"""共用遗传搜索 + 编排器 — 不关心具体策略，只操作 SearchStrategy 接口。"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from joblib import Parallel, delayed

from .config import (
    StrategyConstraints,
    DiscreteSearchConfig,
    WindowStats,
    get_constraints,
)
from .execution import DEFAULT_FILL_PRICE_POLICY
from .search_interface import SearchStrategy, Params, StrategyMarketData
from .strategy_artifacts import as_yaml_primitives

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 策略编码
# ═══════════════════════════════════════════════════════════════


@dataclass
class StrategyEncoding:
    """策略的扁平编码 — 纯整数数组，适配 GA。"""

    genome: list[int]  # 扁平编码
    engine_name: str = ""
    params: Params | None = None  # 解码后的 Params（懒求值）

    def to_params(self, strategy: SearchStrategy) -> Params:
        if self.params is not None:
            return self.params
        vals = {}
        for i, d in enumerate(strategy.param_space.dims):
            vals[d.name] = self.genome[i] if i < len(self.genome) else 0
        self.params = Params(values=vals, _engine=strategy.name)
        return self.params


# ═══════════════════════════════════════════════════════════════
# GA 核心
# ═══════════════════════════════════════════════════════════════


def _random_encoding(strategy: SearchStrategy) -> StrategyEncoding:
    genome = []
    r = random
    for d in strategy.param_space.dims:
        genome.append(r.randint(0, max(d.levels - 1, 0)))
    return StrategyEncoding(genome=genome, engine_name=strategy.name)


def _crossover(p1: StrategyEncoding, p2: StrategyEncoding) -> StrategyEncoding:
    child = []
    for a, b in zip(p1.genome, p2.genome):
        child.append(a if random.random() < 0.5 else b)
    return StrategyEncoding(genome=child, engine_name=p1.engine_name)


def _mutate(
    enc: StrategyEncoding, strategy: SearchStrategy, rate: float = 0.15
) -> StrategyEncoding:
    new_genome = list(enc.genome)
    for i, d in enumerate(strategy.param_space.dims):
        if i < len(new_genome) and random.random() < rate:
            new_genome[i] = random.randint(0, max(d.levels - 1, 0))
    return StrategyEncoding(genome=new_genome, engine_name=strategy.name)


def _split_ranking_and_validation_stats(
    all_stats: list[WindowStats],
    constraints: StrategyConstraints,
    validation_window_count: int | None = None,
) -> tuple[list[WindowStats], list[WindowStats]]:
    """Keep the newest configured windows completely outside selection."""
    requested = (
        constraints.walk_forward.held_out_window_count
        if validation_window_count is None
        else max(0, int(validation_window_count))
    )
    held_out = min(requested, len(all_stats))
    if held_out == 0:
        return list(all_stats), []
    return list(all_stats[:-held_out]), list(all_stats[-held_out:])


def _partition_window_indexes(
    windows: list,
    constraints: StrategyConstraints,
    validation_window_count: int | None = None,
) -> tuple[list[int], list[int], list[int]]:
    """Split windows into ranking, purged-overlap and validation sets.

    A held-out test period is not truly unseen when an earlier rolling window
    extends into it.  Keep only windows ending on or before the first held-out
    test day; the intervening windows are an explicit embargo rather than
    silently leaking validation dates into candidate selection.
    """
    requested = (
        constraints.walk_forward.held_out_window_count
        if validation_window_count is None
        else max(0, int(validation_window_count))
    )
    held_out = min(requested, len(windows))
    if held_out == 0:
        return list(range(len(windows))), [], []

    validation_indexes = list(range(len(windows) - held_out, len(windows)))
    candidate_indexes = list(range(0, len(windows) - held_out))
    if not constraints.walk_forward.purge_overlapping_windows:
        return candidate_indexes, [], validation_indexes

    first_validation = windows[validation_indexes[0]]
    validation_start = getattr(first_validation, "test_start", None)
    if validation_start is None:
        return candidate_indexes, [], validation_indexes

    ranking_indexes = []
    purged_indexes = []
    for index in candidate_indexes:
        test_end = getattr(windows[index], "test_end", None)
        if test_end is not None and test_end <= validation_start:
            ranking_indexes.append(index)
        else:
            purged_indexes.append(index)
    return ranking_indexes, purged_indexes, validation_indexes


def _compute_ranking_wf_score(
    ranking_stats: list[WindowStats], constraints: StrategyConstraints
) -> float | None:
    """Original WF formula: weighted excess return minus stability penalty."""
    if not ranking_stats:
        return None

    returns = [float(stat.excess_return) for stat in ranking_stats]
    weights = constraints.walk_forward.ranking_weights(len(returns))
    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0 / len(returns)] * len(returns)
    else:
        weights = [weight / total_weight for weight in weights]

    score = float(sum(value * weight for value, weight in zip(returns, weights)))
    if len(returns) >= 2 and constraints.walk_forward.stability_penalty > 0:
        score -= float(np.std(returns)) * constraints.walk_forward.stability_penalty
    mean_sharpe = float(np.mean([stat.sharpe_ratio for stat in ranking_stats]))
    score -= constraints.compute_soft_penalty(mean_sharpe)
    return score


def _ranking_return_diagnostics(
    ranking_stats: list[WindowStats], constraints: StrategyConstraints
) -> dict[str, float | int]:
    """Summarize absolute strategy returns using ranking windows only."""
    returns = [float(stat.strategy_return) for stat in ranking_stats]
    weights = constraints.walk_forward.ranking_weights(len(returns))
    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0 / len(returns)] * len(returns) if returns else []
    else:
        weights = [weight / total_weight for weight in weights]
    diagnostics = {
        "weighted_strategy_return": float(
            sum(value * weight for value, weight in zip(returns, weights))
        ),
        "positive_return_windows": sum(value > 0.0 for value in returns),
        "ranking_window_count": len(returns),
    }
    has_benchmark_contract = any(
        bool(getattr(stat, "strongest_benchmark", ""))
        or bool(getattr(stat, "benchmark_returns", {}))
        for stat in ranking_stats
    )
    has_benchmark_contract = has_benchmark_contract or (
        len(ranking_stats) >= constraints.genetic_search.min_winning_benchmark_windows
        and any(float(stat.test_excess_return) != 0.0 for stat in ranking_stats)
    )
    if has_benchmark_contract:
        excess = [float(stat.test_excess_return) for stat in ranking_stats]
        diagnostics.update(
            {
                "mean_strongest_benchmark_excess": float(np.mean(excess)),
                "strongest_benchmark_win_windows": sum(value > 0.0 for value in excess),
            }
        )
    return diagnostics


def _passes_ranking_return_gate(
    diagnostics: dict[str, float | int], constraints: StrategyConstraints
) -> bool:
    """Require the configured absolute-return quality without holdout leakage."""
    absolute_pass = (
        float(diagnostics["weighted_strategy_return"])
        > constraints.genetic_search.min_weighted_strategy_return
        and int(diagnostics["positive_return_windows"])
        >= constraints.genetic_search.min_positive_return_windows
    )
    if "mean_strongest_benchmark_excess" not in diagnostics:
        return absolute_pass
    return absolute_pass and (
        float(diagnostics["mean_strongest_benchmark_excess"]) > 0.0
        and int(diagnostics["strongest_benchmark_win_windows"])
        >= constraints.genetic_search.min_winning_benchmark_windows
    )


# ═══════════════════════════════════════════════════════════════
# WF 窗口评估
# ═══════════════════════════════════════════════════════════════


def _prepare_wf_evaluation_contexts(
    windows: list,
    state_scope: str,
    constraints: StrategyConstraints,
    evaluator,
    wf_manager,
) -> dict[str, object]:
    """Cache parameter-independent window inputs and executable baselines."""
    from .backtester import buy_and_hold_nav

    geometry = tuple((w.train_start, w.test_start, w.test_end) for w in windows)
    cache_key = (
        state_scope,
        geometry,
        float(evaluator.initial_cash),
        float(evaluator.commission_rate),
        int(evaluator.lot_size),
        float(evaluator.fx_rate),
        float(constraints.risk_free_rate),
        int(constraints.execution.lot_sizes.get("a_share", 100)),
    )
    cache = getattr(wf_manager, "_evaluation_context_cache", None)
    if cache is None:
        cache = {}
        setattr(wf_manager, "_evaluation_context_cache", cache)
    if cache_key in cache:
        return cache[cache_key]

    parsed_dates = pd.to_datetime(wf_manager.dates)
    date_strings = [str(pd.Timestamp(date).date()) for date in parsed_dates]
    date_ordinals = parsed_dates.values.astype("datetime64[D]").astype(np.int64)
    symbols = list(wf_manager.stock_codes)
    price_matrix = wf_manager.price_matrix
    high_matrix = wf_manager.price_high_matrix
    low_matrix = wf_manager.price_low_matrix
    market_group = str(getattr(wf_manager, "market_group", "a_share"))
    full_market_data = StrategyMarketData(
        indicator_matrix=wf_manager.indicator_matrix,
        dates=date_strings,
        symbols=symbols,
        prices=price_matrix,
        highs=high_matrix,
        lows=low_matrix,
        tradable=np.isfinite(price_matrix) & (price_matrix > 0),
        date_ordinals=date_ordinals,
        market=market_group,
    )

    available_benchmarks = getattr(wf_manager, "benchmark_series", {})
    benchmark_high_series = getattr(wf_manager, "benchmark_high_series", {})
    contexts: list[dict[str, object]] = []
    for window in windows:
        test_slice = slice(window.test_start, window.test_end)
        test_indicators = wf_manager.indicator_matrix[test_slice]
        test_prices = price_matrix[test_slice]
        state_start = window.train_start if state_scope == "train" else 0
        state_end = window.test_end if state_scope == "train" else len(price_matrix)
        state_slice = slice(state_start, state_end)
        state_prices = price_matrix[state_slice]
        signal_market_data = StrategyMarketData(
            indicator_matrix=wf_manager.indicator_matrix[state_slice],
            dates=date_strings[state_slice],
            symbols=symbols,
            prices=state_prices,
            highs=high_matrix[state_slice],
            lows=low_matrix[state_slice],
            tradable=np.isfinite(state_prices) & (state_prices > 0),
            date_ordinals=date_ordinals[state_slice],
            market=market_group,
        )
        execution_prices = DEFAULT_FILL_PRICE_POLICY.build(
            price_matrix,
            high_matrix,
            low_matrix,
            start=window.test_start,
            end=window.test_end,
        )

        cash_baseline = evaluator.initial_cash * (
            1 + constraints.risk_free_rate / 252
        ) ** np.arange(test_indicators.shape[0], dtype=np.float64)
        benchmark_series = {"risk_free": cash_baseline}
        benchmark_initial_values = {"risk_free": evaluator.initial_cash}
        benchmark_raw_returns = {
            "risk_free": float((cash_baseline[-1] / cash_baseline[0] - 1.0) * 100.0)
        }
        if "510300" in available_benchmarks:
            raw_close = np.asarray(available_benchmarks["510300"], dtype=np.float64)
            raw_high = np.asarray(
                benchmark_high_series.get("510300", raw_close),
                dtype=np.float64,
            )
            benchmark_execution = DEFAULT_FILL_PRICE_POLICY.build(
                raw_close,
                raw_high,
                raw_close,
                start=window.test_start,
                end=window.test_end,
            )
            benchmark_close = benchmark_execution.valuation_prices[:, 0]
            benchmark_buy = benchmark_execution.buy_prices[:, 0]
            benchmark_series["510300"] = buy_and_hold_nav(
                benchmark_close,
                benchmark_buy,
                evaluator.initial_cash,
                int(constraints.execution.lot_sizes.get("a_share", 100)),
                evaluator.commission_rate,
                weights=np.array([1.0]),
            )
            benchmark_initial_values["510300"] = evaluator.initial_cash
            raw_510300 = benchmark_close
            if len(raw_510300) > 1 and raw_510300[0] > 0:
                benchmark_raw_returns["510300"] = float(
                    (raw_510300[-1] / raw_510300[0] - 1.0) * 100.0
                )
        resolved_execution = execution_prices.scaled(evaluator.fx_rate)
        benchmark_series["universe_equal_weight"] = buy_and_hold_nav(
            resolved_execution.valuation_prices,
            resolved_execution.buy_prices,
            evaluator.initial_cash,
            evaluator.lot_size,
            evaluator.commission_rate,
        )
        benchmark_initial_values["universe_equal_weight"] = evaluator.initial_cash
        raw_components = []
        for column in range(test_prices.shape[1]):
            values = test_prices[:, column]
            valid = values[np.isfinite(values) & (values > 0)]
            if len(valid) > 1:
                raw_components.append(valid[-1] / valid[0] - 1.0)
        benchmark_raw_returns["universe_equal_weight"] = float(
            np.mean(raw_components) * 100.0 if raw_components else 0.0
        )
        contexts.append(
            {
                "signal_market_data": signal_market_data,
                "relative_test_start": window.test_start - state_start,
                "relative_test_end": window.test_end - state_start,
                "test_indicators": test_indicators,
                "execution_prices": execution_prices,
                "test_prices": test_prices,
                "cash_baseline": cash_baseline,
                "benchmark_series": benchmark_series,
                "benchmark_initial_values": benchmark_initial_values,
                "benchmark_raw_returns": benchmark_raw_returns,
            }
        )
    result = {
        "full_market_data": full_market_data,
        "windows": contexts,
    }
    cache[cache_key] = result
    return result


def _evaluate_encoding_wf(
    encoding: StrategyEncoding,
    strategy: SearchStrategy,
    windows: list,
    ds_cfg: DiscreteSearchConfig,
    constraints: StrategyConstraints,
    evaluator,
    wf_manager,
    validation_window_count: int | None = None,
):
    """在多个 WF 窗口上评估一组参数（统一路径，零策略分支）。"""
    all_stats = []
    params = encoding.to_params(strategy)
    # Build against the complete aligned history so a test window inherits its
    # legitimate train-period warmup instead of being muted for 252 days.
    state_scope = str(getattr(strategy, "window_state_scope", "continuous"))
    prepared = _prepare_wf_evaluation_contexts(
        windows, state_scope, constraints, evaluator, wf_manager
    )
    full_plan = None
    if state_scope != "train":
        full_plan = strategy.make_signals(
            params,
            prepared["full_market_data"],
        )

    for w, context in zip(windows, prepared["windows"]):
        if state_scope == "train":
            state_plan = strategy.make_signals(params, context["signal_market_data"])
            trade_plan = state_plan.sliced(
                context["relative_test_start"],
                context["relative_test_end"],
            )
        else:
            # Existing strategies retain their historical continuous-state
            # behavior; new train-scoped strategies declare the reset above.
            trade_plan = full_plan.sliced(w.test_start, w.test_end)

        stats = evaluator.evaluate(
            indicator_matrix=context["test_indicators"],
            price_matrix=context["test_prices"],
            cash_baseline=context["cash_baseline"],
            execution_prices=context["execution_prices"],
            trade_plan=trade_plan,
            benchmark_series=context["benchmark_series"],
            benchmark_initial_values=context["benchmark_initial_values"],
            benchmark_raw_returns=context["benchmark_raw_returns"],
        )
        all_stats.append(stats)

    if not all_stats:
        return None

    ranking_indexes, _, validation_indexes = _partition_window_indexes(
        windows, constraints, validation_window_count
    )
    ranking_stats = [all_stats[index] for index in ranking_indexes]
    validation_stats = [all_stats[index] for index in validation_indexes]
    wf_score = _compute_ranking_wf_score(ranking_stats, constraints)
    if wf_score is None:
        return None

    return all_stats, ranking_stats, validation_stats, wf_score


# ═══════════════════════════════════════════════════════════════
# 遗传搜索
# ═══════════════════════════════════════════════════════════════


@dataclass
class ScoredEncoding:
    encoding: StrategyEncoding
    wf_stats: list[WindowStats]
    wf_score: float
    # ``wf_stats`` is retained for compatibility and contains all windows.
    # Every search decision must use ``ranking_stats`` only.
    ranking_stats: list[WindowStats] = field(default_factory=list)
    validation_stats: list[WindowStats] = field(default_factory=list)
    purged_window_count: int = 0
    ranking_diagnostics: dict[str, float | int] = field(default_factory=dict)
    sensitivity: dict[str, float | int] = field(default_factory=dict)
    universe_robustness: dict[str, object] = field(default_factory=dict)
    selection_score: float | None = None
    search_metadata: dict[str, object] = field(default_factory=dict)


class GeneticOptimizer:
    """通用遗传搜索 — 三阶段：随机采样 → 交叉变异 → 精确验证。"""

    def __init__(
        self,
        strategy: SearchStrategy,
        constraints: StrategyConstraints,
        wf_manager,
        evaluator,
    ):
        self.strategy = strategy
        self.constraints = constraints
        self.ds_cfg = constraints.discrete_search
        self.ga_cfg = constraints.genetic_search
        self.wf_manager = wf_manager
        self.evaluator = evaluator
        self._wf_result_cache: dict[tuple[int, ...], object] = {}

    def _evaluate_cached(self, encoding: StrategyEncoding, windows: list):
        """Reuse identical genomes produced by crossover and no-op mutation."""
        key = tuple(int(value) for value in encoding.genome)
        if key in self._wf_result_cache:
            return self._wf_result_cache[key]
        result = _evaluate_encoding_wf(
            encoding,
            self.strategy,
            windows,
            self.ds_cfg,
            self.constraints,
            self.evaluator,
            self.wf_manager,
            validation_window_count=0,
        )
        self._wf_result_cache[key] = result
        return result

    def _evaluate_many(
        self, encodings: list[StrategyEncoding], windows: list
    ) -> list[object]:
        """Deduplicate genomes and evaluate them in stable process order."""
        if not encodings:
            return []
        ordered_keys = [
            tuple(int(value) for value in encoding.genome) for encoding in encodings
        ]
        missing: dict[tuple[int, ...], StrategyEncoding] = {}
        for key, encoding in zip(ordered_keys, encodings):
            if key not in self._wf_result_cache and key not in missing:
                missing[key] = encoding
        missing_items = list(missing.items())
        if self.ga_cfg.evaluation_workers == 1 or len(missing_items) <= 1:
            for key, encoding in missing_items:
                self._wf_result_cache[key] = _evaluate_encoding_wf(
                    encoding,
                    self.strategy,
                    windows,
                    self.ds_cfg,
                    self.constraints,
                    self.evaluator,
                    self.wf_manager,
                    validation_window_count=0,
                )
        elif missing_items:
            results = Parallel(
                n_jobs=self.ga_cfg.evaluation_workers,
                backend="loky",
                batch_size=5,
                max_nbytes="1M",
            )(
                delayed(_evaluate_encoding_wf)(
                    encoding,
                    self.strategy,
                    windows,
                    self.ds_cfg,
                    self.constraints,
                    self.evaluator,
                    self.wf_manager,
                    validation_window_count=0,
                )
                for _, encoding in missing_items
            )
            for (key, _encoding), result in zip(missing_items, results):
                self._wf_result_cache[key] = result
        return [self._wf_result_cache[key] for key in ordered_keys]

    def run(self) -> list[ScoredEncoding]:
        if self.ga_cfg.random_seed is not None:
            random.seed(self.ga_cfg.random_seed)
        windows = self.wf_manager.iter_windows()
        if not windows:
            wf = self.constraints.walk_forward
            required_rows = (
                wf.train_months * 21
                + wf.test_months * 21
                + (wf.num_windows - 1) * wf.step_months * 21
            )
            logger.warning(
                "No complete WF windows: rows=%d, required=%d",
                self.wf_manager.T,
                required_rows,
            )
            return []

        first_bounds = (
            getattr(windows[0], "test_start", None),
            getattr(windows[0], "test_end", None),
        )
        held_out_bounds = (
            getattr(windows[-1], "test_start", None),
            getattr(windows[-1], "test_end", None),
        )
        logger.info(
            "WF windows: count=%d, first_test=%s, held_out=%s",
            len(windows),
            first_bounds,
            held_out_bounds,
        )
        ranking_indexes, purged_indexes, validation_indexes = _partition_window_indexes(
            windows, self.constraints
        )
        ranking_windows = [windows[index] for index in ranking_indexes]
        logger.info(
            "WF selection partition: ranking=%d, purged_overlap=%d, validation=%d",
            len(ranking_indexes),
            len(purged_indexes),
            len(validation_indexes),
        )
        if not ranking_windows:
            logger.warning("No strictly independent ranking windows are available")
            return []

        # Phase 1: 随机采样
        logger.info(
            "[Phase 1] Random sampling %d strategies", self.ga_cfg.phase1_random_samples
        )
        scored = []
        absolute_return_rejections = 0
        encodings = [
            _random_encoding(self.strategy)
            for _ in range(self.ga_cfg.phase1_random_samples)
        ]
        evaluated = self._evaluate_many(encodings, ranking_windows)
        for enc, result in zip(encodings, evaluated):
            if result:
                stats, ranking_stats, validation_stats, score = result
                ranking_diagnostics = _ranking_return_diagnostics(
                    ranking_stats, self.constraints
                )
                if not _passes_ranking_return_gate(
                    ranking_diagnostics, self.constraints
                ):
                    absolute_return_rejections += 1
                    continue
                passes, _ = self.constraints.check_hard_constraints(
                    ranking_stats, score
                )
                if passes:
                    scored.append(
                        ScoredEncoding(
                            enc,
                            stats,
                            score,
                            ranking_stats=ranking_stats,
                            validation_stats=validation_stats,
                            purged_window_count=len(purged_indexes),
                            ranking_diagnostics=ranking_diagnostics,
                        )
                    )

        scored.sort(key=lambda x: x.wf_score, reverse=True)
        scored = scored[: self.ga_cfg.phase1_top_keep]
        logger.info(
            "[Phase 1] Top after filter: %d (absolute-return rejections=%d)",
            len(scored),
            absolute_return_rejections,
        )

        # Phase 2: 遗传迭代
        for gen in range(self.ga_cfg.num_generations):
            pop = scored[: self.ga_cfg.population_size]
            if len(pop) < 2:
                break

            children: list[StrategyEncoding] = []
            absolute_return_rejections = 0
            for _ in range(self.ga_cfg.offspring_size):
                if random.random() < self.ga_cfg.crossover_rate and len(pop) >= 2:
                    p1, p2 = random.sample(pop, 2)
                    child = _crossover(p1.encoding, p2.encoding)
                else:
                    parent = random.choice(pop)
                    child = StrategyEncoding(
                        genome=list(parent.encoding.genome),
                        engine_name=parent.encoding.engine_name,
                    )

                if random.random() < self.ga_cfg.mutation_rate:
                    child = _mutate(child, self.strategy)
                children.append(child)

            offspring: list[ScoredEncoding] = []
            evaluated = self._evaluate_many(children, ranking_windows)
            for child, result in zip(children, evaluated):
                if result:
                    stats, ranking_stats, validation_stats, score = result
                    ranking_diagnostics = _ranking_return_diagnostics(
                        ranking_stats, self.constraints
                    )
                    if not _passes_ranking_return_gate(
                        ranking_diagnostics, self.constraints
                    ):
                        absolute_return_rejections += 1
                        continue
                    passes, _ = self.constraints.check_hard_constraints(
                        ranking_stats, score
                    )
                    if passes:
                        offspring.append(
                            ScoredEncoding(
                                child,
                                stats,
                                score,
                                ranking_stats=ranking_stats,
                                validation_stats=validation_stats,
                                purged_window_count=len(purged_indexes),
                                ranking_diagnostics=ranking_diagnostics,
                            )
                        )

            combined = scored + offspring
            combined.sort(key=lambda x: x.wf_score, reverse=True)
            scored = combined[: self.ga_cfg.population_size]
            logger.info(
                "[Phase 2] Gen %d: best wf_score=%.2f, survivors=%d, "
                "absolute-return rejections=%d",
                gen + 1,
                scored[0].wf_score if scored else 0.0,
                len(scored),
                absolute_return_rejections,
            )

        if scored:
            scored = self._apply_robust_selection(scored, ranking_windows)
            scored[0].universe_robustness = self._evaluate_universe_robustness(
                scored[0], ranking_windows
            )
            worst_drop = float(scored[0].universe_robustness.get("worst_drop", 0.0))
            if scored[0].selection_score is None:
                scored[0].selection_score = scored[0].wf_score
            scored[0].selection_score -= self.ga_cfg.sensitivity_penalty_weight * max(
                worst_drop, 0.0
            )
            logger.info(
                "Robust selection: evaluated top %d by sensitivity, selected wf_score=%.2f, "
                "selection_score=%.2f",
                min(self.ga_cfg.sensitivity_top_candidates, len(scored)),
                scored[0].wf_score,
                (
                    scored[0].selection_score
                    if scored[0].selection_score is not None
                    else scored[0].wf_score
                ),
            )

            # Only the final selected parameter set may see the two isolated
            # windows and the independent holdout.  This is both faster and a
            # stricter guard against accidental validation feedback.
            final_result = _evaluate_encoding_wf(
                scored[0].encoding,
                self.strategy,
                windows,
                self.ds_cfg,
                self.constraints,
                self.evaluator,
                self.wf_manager,
            )
            if final_result is None:
                logger.warning("Final full-window evaluation failed")
                return []
            (
                scored[0].wf_stats,
                scored[0].ranking_stats,
                scored[0].validation_stats,
                _final_wf_score,
            ) = final_result
            scored[0].purged_window_count = len(purged_indexes)

        return scored

    def _evaluate_universe_robustness(
        self, selected: ScoredEncoding, ranking_windows: list
    ) -> dict[str, object]:
        """Check code-order invariance and every leave-one-symbol-out basket."""
        codes = list(getattr(self.wf_manager, "stock_codes", []) or [])
        if not ranking_windows or not codes:
            return {}

        def subset_manager(indexes):
            manager = type("SubsetWalkForwardData", (), {})()
            manager.indicator_matrix = self.wf_manager.indicator_matrix[:, indexes]
            manager.price_matrix = self.wf_manager.price_matrix[:, indexes]
            manager.price_high_matrix = self.wf_manager.price_high_matrix[:, indexes]
            manager.price_low_matrix = self.wf_manager.price_low_matrix[:, indexes]
            manager.stock_codes = [codes[index] for index in indexes]
            manager.dates = self.wf_manager.dates
            manager.benchmark_series = self.wf_manager.benchmark_series
            manager.benchmark_high_series = self.wf_manager.benchmark_high_series
            return manager

        params = selected.encoding.to_params(self.strategy)
        encoding = StrategyEncoding(
            genome=list(selected.encoding.genome),
            engine_name=self.strategy.name,
            params=params,
        )
        reverse_result = _evaluate_encoding_wf(
            encoding,
            self.strategy,
            ranking_windows,
            self.ds_cfg,
            self.constraints,
            self.evaluator,
            subset_manager(list(reversed(range(len(codes))))),
            validation_window_count=0,
        )
        order_invariant = False
        if reverse_result is not None:
            reverse_stats = reverse_result[1]
            order_invariant = len(reverse_stats) == len(selected.ranking_stats)
            if order_invariant:
                for left, right in zip(selected.ranking_stats, reverse_stats):
                    left_values = (
                        left.strategy_return,
                        left.test_excess_return,
                        left.max_drawdown_pct,
                        left.sharpe_ratio,
                        left.total_trades,
                        left.final_asset,
                    )
                    right_values = (
                        right.strategy_return,
                        right.test_excess_return,
                        right.max_drawdown_pct,
                        right.sharpe_ratio,
                        right.total_trades,
                        right.final_asset,
                    )
                    if left_values != right_values:
                        order_invariant = False
                        break

        base_mean = float(
            np.mean([stat.test_excess_return for stat in selected.ranking_stats])
        )
        variants = []
        positive_count = 0
        drops = []
        if len(codes) == 1:
            positive_count = 1
            variants.append(
                {
                    "removed": codes[0],
                    "mean_excess": base_mean,
                    "not_applicable": True,
                }
            )
        else:
            for removed_index, removed_code in enumerate(codes):
                indexes = [
                    index for index in range(len(codes)) if index != removed_index
                ]
                result = _evaluate_encoding_wf(
                    encoding,
                    self.strategy,
                    ranking_windows,
                    self.ds_cfg,
                    self.constraints,
                    self.evaluator,
                    subset_manager(indexes),
                    validation_window_count=0,
                )
                mean_excess = (
                    float(np.mean([stat.test_excess_return for stat in result[1]]))
                    if result is not None and result[1]
                    else float("-inf")
                )
                if mean_excess > 0:
                    positive_count += 1
                drop = (
                    base_mean - mean_excess
                    if np.isfinite(mean_excess)
                    else float("inf")
                )
                drops.append(drop)
                variants.append(
                    {
                        "removed": removed_code,
                        "mean_excess": mean_excess,
                        "drop": drop,
                    }
                )
        required = max(1, int(np.ceil(len(codes) * 0.80)))
        return {
            "symbol_order_invariant": order_invariant,
            "variant_count": len(codes),
            "positive_variant_count": positive_count,
            "required_positive_variant_count": required,
            "leave_one_out_passed": positive_count >= required,
            "base_mean_excess": base_mean,
            "worst_drop": max(drops) if drops else 0.0,
            "variants": variants,
        }

    def _apply_robust_selection(
        self, scored: list[ScoredEncoding], ranking_windows: list
    ) -> list[ScoredEncoding]:
        """Apply full sensitivity selection to the configured WF finalist pool."""
        finalist_count = min(self.ga_cfg.sensitivity_top_candidates, len(scored))
        finalists = list(scored[:finalist_count])
        for candidate in finalists:
            candidate.sensitivity = self._evaluate_sensitivity(
                candidate, ranking_windows
            )
            drop = float(candidate.sensitivity.get("drop", 0.0))
            candidate.selection_score = (
                candidate.wf_score - self.ga_cfg.sensitivity_penalty_weight * drop
            )
            candidate.sensitivity["selection_score"] = float(candidate.selection_score)
        finalists.sort(
            key=lambda item: (
                item.selection_score
                if item.selection_score is not None
                else item.wf_score
            ),
            reverse=True,
        )
        return finalists + scored[finalist_count:]

    def _evaluate_sensitivity(
        self, selected: ScoredEncoding, windows: list
    ) -> dict[str, float | int]:
        """Perturb one parameter at a time by one adjacent level."""
        ranking_windows = windows[: len(selected.ranking_stats)]
        if not ranking_windows:
            return {}

        params = selected.encoding.to_params(self.strategy)
        scores: list[float] = []
        perturbation_encodings: list[StrategyEncoding] = []
        for perturbed in self.strategy.random_perturbations(
            params, n=self.ga_cfg.sensitivity_samples
        ):
            if isinstance(perturbed, dict):
                perturbed = Params(values=perturbed, _engine=self.strategy.name)
            if not isinstance(perturbed, Params):
                logger.warning(
                    "Ignoring invalid sensitivity perturbation from %s",
                    self.strategy.name,
                )
                continue
            genome = [
                int(perturbed.values.get(dim.name, 0))
                for dim in self.strategy.param_space.dims
            ]
            perturbation_encodings.append(
                StrategyEncoding(
                    genome=genome,
                    engine_name=self.strategy.name,
                    params=perturbed,
                )
            )
        for result in self._evaluate_many(perturbation_encodings, ranking_windows):
            if result is not None:
                scores.append(float(result[3]))

        if not scores:
            return {}
        worst = min(scores)
        return {
            "sample_count": len(scores),
            "base_score": float(round(float(selected.wf_score), 6)),
            "worst_score": float(round(float(worst), 6)),
            "drop": float(round(float(selected.wf_score) - float(worst), 6)),
            "min_score": float(round(float(min(scores)), 6)),
            "max_score": float(round(float(max(scores)), 6)),
        }


# ═══════════════════════════════════════════════════════════════
# 编排器
# ═══════════════════════════════════════════════════════════════


def run_optimizer(
    strategy: SearchStrategy,
    stocks_data: dict,
    stock_codes: list[str],
    group: str = "a_share",
    _constraints=None,
    output_dir=None,
    benchmark_data: dict | None = None,
) -> tuple[list[ScoredEncoding], StrategyConstraints]:
    """Run the configured Solver through the stable SearchController."""
    from .backtester import WalkForwardManager, FastEvaluator
    from .candidate_gates import CandidateGatePipeline
    from .config import get_constraints
    from .evaluation_service import EvaluationService
    from .feature_registry import TECHNICAL_FEATURES
    from .resource_planner import ResourcePlanner
    from .search_archive import SearchArchive
    from .search_contracts import SearchProblem, SolverCapabilities, stable_hash
    from .search_controller import SearchController
    from .solvers import create_solver
    from .validation_controller import ValidationController

    if _constraints is not None:
        constraints = _constraints
    else:
        constraints = get_constraints()
    constraints.set_group(group)
    exec_cfg = constraints.execution

    wf_manager = WalkForwardManager(
        stocks_data, constraints, stock_codes, benchmark_data=benchmark_data
    )
    wf_manager.market_group = group
    evaluator = FastEvaluator(exec_cfg, group)
    windows = wf_manager.iter_windows()
    ranking_indexes, purged_indexes, validation_indexes = _partition_window_indexes(
        windows, constraints
    )
    ranking_windows = [windows[index] for index in ranking_indexes]
    if not ranking_windows:
        logger.warning("No independent ranking windows are available")
        return [], constraints

    schema = strategy.search_parameter_schema
    gate_pipeline = CandidateGatePipeline.from_config(
        constraints._raw_config, constraints.search.gate_profile
    )
    solver_config = constraints.search.solver_config()
    legacy_budget = (
        constraints.genetic_search.phase1_random_samples
        + constraints.genetic_search.num_generations
        * constraints.genetic_search.offspring_size
    )
    budget = max(1, int(solver_config.get("budget", legacy_budget)))

    data_hasher = hashlib.sha256()
    for array in (
        wf_manager.indicator_matrix,
        wf_manager.price_matrix,
        wf_manager.price_high_matrix,
        wf_manager.price_low_matrix,
    ):
        contiguous = np.ascontiguousarray(array)
        data_hasher.update(str(contiguous.shape).encode("ascii"))
        data_hasher.update(str(contiguous.dtype).encode("ascii"))
        data_hasher.update(contiguous.view(np.uint8))
    data_hasher.update("|".join(wf_manager.stock_codes).encode("utf-8"))
    data_hasher.update("|".join(map(str, wf_manager.dates)).encode("utf-8"))
    data_hash = data_hasher.hexdigest()
    window_hash = stable_hash(
        [
            {
                "train_start": window.train_start,
                "test_start": window.test_start,
                "test_end": window.test_end,
            }
            for window in windows
        ]
    )
    execution_hash = stable_hash(
        {
            "initial_capital": exec_cfg.initial_capital,
            "commission_rate": exec_cfg.commission_rate,
            "min_holding_days": exec_cfg.min_holding_days,
            "lot_size": exec_cfg.lot_sizes.get(group, 100),
            "fx_rate": exec_cfg.fx_rates.get(group, 1.0),
            "fill_policy": DEFAULT_FILL_PRICE_POLICY.name,
        }
    )
    dependencies = tuple(getattr(strategy, "feature_dependencies", ()) or ())
    feature_hash = (
        TECHNICAL_FEATURES.hash
        if dependencies == TECHNICAL_FEATURES.names
        else stable_hash({"strategy": strategy.name, "features": dependencies})
    )
    problem = SearchProblem(
        schema=schema,
        objective_id="weighted-strongest-excess-stability-sharpe/1",
        gate_profile_id=gate_pipeline.hash,
        budget=budget,
        data_hash=data_hash,
        execution_hash=execution_hash,
        window_hash=window_hash,
        feature_hash=feature_hash,
        requirements=SolverCapabilities(
            batched=True,
            conditional_parameters=any(item.active_if for item in schema.parameters),
        ),
        metadata={"strategy_id": strategy.name, "market": group},
    )
    planner = ResourcePlanner()
    resource_plan = planner.plan(
        constraints.search.parallel_axis,
        constraints.search.workers,
        constraints.search.batch_size,
    )
    base_output = Path(output_dir) if output_dir is not None else Path("data/optimizer")
    archive = SearchArchive(base_output / f"{group}_search_archive.jsonl", problem)
    checkpoint_path = (
        base_output / f"{group}_search_checkpoint.yaml"
        if constraints.search.checkpoint
        else None
    )
    service = EvaluationService(
        strategy,
        constraints,
        wf_manager,
        evaluator,
        ranking_windows,
        workers=resource_plan.outer_workers,
    )
    solver = create_solver(constraints.search.solver_id)
    controller = SearchController(
        problem,
        solver,
        service,
        gate_pipeline,
        solver_config=solver_config,
        batch_size=resource_plan.batch_size,
        archive=archive,
        checkpoint_path=checkpoint_path,
    )
    with planner.apply(resource_plan):
        searched = controller.run(
            finalist_limit=constraints.genetic_search.sensitivity_top_candidates
        )
        validated = ValidationController(
            strategy,
            constraints,
            wf_manager,
            evaluator,
            schema,
            service,
            gate_pipeline,
            windows,
        ).run(searched)

    search_metadata = {
        "solver_id": solver.solver_id,
        "solver_config": solver_config,
        "gate_profile": gate_pipeline.profile_id,
        "gate_profile_hash": gate_pipeline.hash,
        "gate_activation_eligible": gate_pipeline.activation_eligible,
        "search_contract_hash": problem.contract_hash,
        "parameter_schema_hash": schema.hash,
        "feature_contract_hash": feature_hash,
        "data_contract_hash": data_hash,
        "execution_contract_hash": execution_hash,
        "window_contract_hash": window_hash,
        "ranking_window_indexes": ranking_indexes,
        "purged_window_count": len(purged_indexes),
        "validation_window_count": len(validation_indexes),
        "resource_plan": resource_plan.__dict__,
        "performance": service.performance_snapshot(),
        "budget": budget,
    }
    results = []
    for item in validated:
        params = Params(values=dict(item.parameters), _engine=strategy.name)
        encoding = StrategyEncoding(
            genome=[int(params.values[dim.name]) for dim in strategy.param_space.dims],
            engine_name=strategy.name,
            params=params,
        )
        results.append(
            ScoredEncoding(
                encoding=encoding,
                wf_stats=item.all_stats,
                wf_score=item.objective_score,
                ranking_stats=item.ranking_stats,
                validation_stats=item.validation_stats,
                purged_window_count=item.purged_window_count,
                ranking_diagnostics=item.ranking_metrics,
                sensitivity=item.sensitivity,
                universe_robustness=item.universe_robustness,
                selection_score=item.selection_score,
                search_metadata=dict(search_metadata),
            )
        )
    if results:
        validation_period = {}
        if windows:
            held_out = windows[-1]
            validation_period = {
                "start": str(wf_manager.dates[held_out.test_start].date()),
                "end": str(wf_manager.dates[held_out.test_end - 1].date()),
            }
        _save_optimizer_result(
            results,
            strategy,
            group,
            output_dir=output_dir,
            validation_period=validation_period,
            constraints=constraints,
            windows=windows,
            strategy_codes=wf_manager.stock_codes,
        )
    return results, constraints


def _save_optimizer_result(
    results,
    strategy,
    group: str,
    output_dir=None,
    validation_period: dict[str, str] | None = None,
    constraints=None,
    windows=None,
    strategy_codes=None,
) -> None:
    """保存最优参数到 data/optimizer/{group}_best_params.yaml"""
    top = results[0]
    constraints = constraints or get_constraints()
    params = top.encoding.to_params(strategy)
    windows = list(windows or [])
    strategy_codes = list(strategy_codes or [])

    def serialize_window(stat, window=None):
        final_asset = float(getattr(stat, "final_asset", 0.0) or 0.0)
        shares = np.asarray(getattr(stat, "final_shares", []), dtype=float)
        prices = np.asarray(getattr(stat, "final_prices", []), dtype=float)
        costs = np.asarray(getattr(stat, "cost_basis", []), dtype=float)
        holdings = []
        if len(shares) == len(prices) == len(costs) == len(strategy_codes):
            for code, quantity, price, cost in zip(
                strategy_codes, shares, prices, costs
            ):
                if quantity <= 0 or not np.isfinite(price) or price <= 0:
                    continue
                value = float(quantity * price)
                holdings.append(
                    {
                        "code": code,
                        "shares": round(float(quantity), 4),
                        "cost": round(float(cost), 4),
                        "price": round(float(price), 4),
                        "value": round(value, 2),
                        "weight": (
                            round(value / final_asset * 100, 2) if final_asset else 0.0
                        ),
                        "pnl": round((price - cost) * quantity, 2),
                    }
                )
        result = {
            "return": float(stat.strategy_return),
            "excess_return": float(stat.test_excess_return),
            "max_drawdown": float(stat.max_drawdown_pct),
            "sharpe_ratio": float(stat.sharpe_ratio),
            "trade_count": int(stat.total_trades),
            "initial_asset": float(getattr(stat, "initial_asset", 0.0)),
            "final_asset": final_asset,
            "final_cash": float(getattr(stat, "final_cash", 0.0)),
            "final_position_pct": float(stat.final_position_pct),
            "final_holdings": holdings,
            "benchmark_returns": dict(stat.benchmark_returns),
            "benchmark_raw_returns": dict(
                getattr(stat, "benchmark_raw_returns", {}) or {}
            ),
            "strongest_benchmark": str(getattr(stat, "strongest_benchmark", "") or ""),
            "pending_order_count": int(getattr(stat, "pending_order_count", 0)),
        }
        if window is not None:
            result["period"] = {
                "train_start": window.train_start_date,
                "train_end": window.train_end_date,
                "test_start": window.test_start_date,
                "test_end": window.test_end_date,
            }
        return result

    ranking_reports = [
        serialize_window(stat, windows[index] if index < len(windows) else None)
        for index, stat in enumerate(top.ranking_stats)
    ]
    validation_offset = len(top.ranking_stats) + int(top.purged_window_count)
    isolated_reports = [
        serialize_window(
            top.wf_stats[index],
            windows[index] if index < len(windows) else None,
        )
        for index in range(
            len(top.ranking_stats),
            min(validation_offset, len(top.wf_stats)),
        )
    ]
    holdout_reports = [
        serialize_window(
            stat,
            (
                windows[validation_offset + index]
                if validation_offset + index < len(windows)
                else None
            ),
        )
        for index, stat in enumerate(top.validation_stats)
    ]
    holdout_passed = bool(top.validation_stats) and all(
        stat.test_excess_return > 0 and bool(getattr(stat, "strongest_benchmark", ""))
        for stat in top.validation_stats
    )
    required_benchmarks = {"risk_free", "510300", "universe_equal_weight"}
    benchmarks_complete = all(
        required_benchmarks.issubset(set(stat.benchmark_returns))
        for stat in top.wf_stats
    )
    local_robustness_passed = bool(top.sensitivity) and (
        float(top.sensitivity.get("worst_score", float("-inf"))) > 0
        and float(
            top.selection_score if top.selection_score is not None else float("-inf")
        )
        > 0
    )
    universe_robustness = dict(getattr(top, "universe_robustness", {}) or {})
    universe_robustness_passed = bool(universe_robustness) and (
        bool(universe_robustness.get("symbol_order_invariant"))
        and bool(universe_robustness.get("leave_one_out_passed"))
    )
    search_metadata = dict(getattr(top, "search_metadata", {}) or {})
    activation_eligible = bool(
        holdout_passed
        and benchmarks_complete
        and bool(search_metadata.get("gate_activation_eligible", True))
        and local_robustness_passed
        and universe_robustness_passed
    )
    data = {
        "timestamp": datetime.now().isoformat(),
        "group": group,
        "strategy_id": strategy.name,
        "schema_version": 2,
        "parameter_schema": getattr(strategy, "parameter_schema", "legacy/1"),
        "solver_id": search_metadata.get("solver_id", "genetic"),
        "solver_config": dict(search_metadata.get("solver_config", {})),
        "gate_profile": search_metadata.get("gate_profile", "standard"),
        "contracts": {
            key: value
            for key, value in search_metadata.items()
            if key.endswith("_hash")
        },
        "params": dict(params.values),
        "execution": strategy.execution_params(params),
        "resource_plan": dict(search_metadata.get("resource_plan", {})),
        "performance": dict(search_metadata.get("performance", {})),
        "validation_period": validation_period or {},
        "wf_score": top.wf_score,
        "ranking_windows": ranking_reports,
        "isolated_windows": isolated_reports,
        "holdout_windows": holdout_reports,
        "activation": {
            "eligible": activation_eligible,
            "holdout_passed": holdout_passed,
            "benchmarks_complete": benchmarks_complete,
            "local_robustness_passed": local_robustness_passed,
            "universe_robustness_passed": universe_robustness_passed,
            "requires_manual_activation": bool(
                getattr(strategy, "manual_activation", False)
            ),
        },
        "search": {
            "total_window_count": len(top.wf_stats),
            "ranking_window_count": len(top.ranking_stats),
            "validation_window_count": len(top.validation_stats),
            "purged_overlap_window_count": top.purged_window_count,
            "budget": int(search_metadata.get("budget", 0)),
            "solver_id": search_metadata.get("solver_id", "genetic"),
            "gate_profile": search_metadata.get("gate_profile", "standard"),
            "score_formula": (
                "weighted_strongest_benchmark_excess - stability_penalty * std "
                "- sharpe_penalty"
            ),
            "selection_formula": (
                f"wf_score - {constraints.genetic_search.sensitivity_penalty_weight:g} "
                "* sensitivity_drop"
            ),
            "absolute_return_gate": {
                "min_weighted_strategy_return": (
                    constraints.genetic_search.min_weighted_strategy_return
                ),
                "min_positive_return_windows": (
                    constraints.genetic_search.min_positive_return_windows
                ),
            },
            "selection_score": top.selection_score,
            "ranking_diagnostics": dict(top.ranking_diagnostics),
            "strongest_benchmark_gate": {
                "min_mean_excess": 0.0,
                "min_winning_windows": (
                    constraints.genetic_search.min_winning_benchmark_windows
                ),
            },
        },
        "sensitivity": dict(top.sensitivity),
        "universe_robustness": universe_robustness,
    }
    from pathlib import Path

    out_dir = Path(output_dir) if output_dir is not None else Path("data/optimizer")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{group}_best_params.yaml"
    path.write_text(
        yaml.safe_dump(
            as_yaml_primitives(data),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    logger.info(f"最优参数已保存: {path}")
