"""共用遗传搜索 + 编排器 — 不关心具体策略，只操作 SearchStrategy 接口。"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

import numpy as np
import yaml
from datetime import datetime

from .config import (
    StrategyConstraints, DiscreteSearchConfig, WindowStats, get_constraints,
)
from .search_interface import SearchStrategy, Params, TradePlan

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


def _mutate(enc: StrategyEncoding, strategy: SearchStrategy, rate: float = 0.15) -> StrategyEncoding:
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
    return {
        "weighted_strategy_return": float(
            sum(value * weight for value, weight in zip(returns, weights))
        ),
        "positive_return_windows": sum(value > 0.0 for value in returns),
        "ranking_window_count": len(returns),
    }


def _passes_ranking_return_gate(
    diagnostics: dict[str, float | int], constraints: StrategyConstraints
) -> bool:
    """Require the configured absolute-return quality without holdout leakage."""
    return (
        float(diagnostics["weighted_strategy_return"])
        > constraints.genetic_search.min_weighted_strategy_return
        and int(diagnostics["positive_return_windows"])
        >= constraints.genetic_search.min_positive_return_windows
    )


# ═══════════════════════════════════════════════════════════════
# WF 窗口评估
# ═══════════════════════════════════════════════════════════════

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
    full_plan = strategy.make_trade_plan(params, wf_manager.indicator_matrix)

    for w in windows:
        test_ind = wf_manager.indicator_matrix[w.test_start:w.test_end]
        test_price = wf_manager.price_matrix[w.test_start:w.test_end]

        # Slice the canonical full-history plan; no validation data is used to
        # rank candidates, only the signal state has its normal prior history.
        trade_plan = TradePlan(
            buy_signals=full_plan.buy_signals[w.test_start:w.test_end],
            sell_signals=full_plan.sell_signals[w.test_start:w.test_end],
            buy_priority=full_plan.buy_priority[w.test_start:w.test_end],
            sell_priority=full_plan.sell_priority[w.test_start:w.test_end],
            buy_cash_limit=full_plan.buy_cash_limit,
            sell_cash_limit=full_plan.sell_cash_limit,
            warmup_rows=full_plan.warmup_rows,
        )

        cash_bs = evaluator.initial_cash * (
            1 + constraints.risk_free_rate / 252
        ) ** np.arange(test_ind.shape[0], dtype=np.float64)
        benchmark_series = {}
        available_benchmarks = getattr(wf_manager, "benchmark_series", {})
        for code in constraints.benchmark_codes:
            if code == "risk_free":
                benchmark_series[code] = cash_bs
            elif code in available_benchmarks:
                benchmark_series[code] = available_benchmarks[code][
                    w.test_start:w.test_end
                ]
        stats = evaluator.evaluate(
            indicator_matrix=test_ind,
            price_matrix=test_price,
            cash_baseline=cash_bs,
            trade_plan=trade_plan,
            benchmark_series=benchmark_series or None,
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
    selection_score: float | None = None


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
        logger.info("[Phase 1] Random sampling %d strategies", self.ga_cfg.phase1_random_samples)
        scored = []
        absolute_return_rejections = 0
        encodings = [
            _random_encoding(self.strategy)
            for _ in range(self.ga_cfg.phase1_random_samples)
        ]
        # 简单串行（可并行化）
        for enc in encodings:
            result = _evaluate_encoding_wf(
                enc, self.strategy, windows, self.ds_cfg,
                self.constraints, self.evaluator, self.wf_manager,
            )
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
            len(scored), absolute_return_rejections,
        )

        # Phase 2: 遗传迭代
        for gen in range(self.ga_cfg.num_generations):
            pop = scored[: self.ga_cfg.population_size]
            if len(pop) < 2:
                break

            offspring: list[ScoredEncoding] = []
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

                result = _evaluate_encoding_wf(
                    child, self.strategy, windows, self.ds_cfg,
                    self.constraints, self.evaluator, self.wf_manager,
                )
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
            logger.info(
                "Robust selection: evaluated top %d by sensitivity, selected wf_score=%.2f, "
                "selection_score=%.2f",
                min(self.ga_cfg.sensitivity_top_candidates, len(scored)),
                scored[0].wf_score,
                scored[0].selection_score if scored[0].selection_score is not None else scored[0].wf_score,
            )

        return scored

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
                candidate.wf_score
                - self.ga_cfg.sensitivity_penalty_weight * drop
            )
            candidate.sensitivity["selection_score"] = float(
                candidate.selection_score
            )
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
        """Perturb every parameter by up to three levels on ranking history only."""
        ranking_windows = windows[: len(selected.ranking_stats)]
        if not ranking_windows:
            return {}

        params = selected.encoding.to_params(self.strategy)
        scores: list[float] = []
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
            result = _evaluate_encoding_wf(
                StrategyEncoding(
                    genome=genome,
                    engine_name=self.strategy.name,
                    params=perturbed,
                ),
                self.strategy,
                ranking_windows,
                self.ds_cfg,
                self.constraints,
                self.evaluator,
                self.wf_manager,
                validation_window_count=0,
            )
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
    """运行完整搜参流程：数据 → 窗口 → GA → 排序结果。"""
    from .backtester import WalkForwardManager, FastEvaluator
    from .config import get_constraints

    if _constraints is not None:
        constraints = _constraints
    else:
        constraints = get_constraints()
        constraints.set_group(group)
    exec_cfg = constraints.execution

    wf_manager = WalkForwardManager(
        stocks_data, constraints, stock_codes, benchmark_data=benchmark_data
    )
    evaluator = FastEvaluator(exec_cfg, group)

    opt = GeneticOptimizer(strategy, constraints, wf_manager, evaluator)
    results = opt.run()
    if results:
        windows = wf_manager.iter_windows()
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
        )
    return results, constraints


def _save_optimizer_result(
    results,
    strategy,
    group: str,
    output_dir=None,
    validation_period: dict[str, str] | None = None,
    constraints=None,
) -> None:
    """保存最优参数到 data/optimizer/{group}_best_params.yaml"""
    top = results[0]
    constraints = constraints or get_constraints()
    params = top.encoding.to_params(strategy)
    data = {
        "timestamp": datetime.now().isoformat(),
        "group": group,
        "engine": strategy.name,
        "schema_version": 2,
        "params": {**params.values, "_engine": strategy.name},
        "execution": strategy.execution_params(params),
        "validation_period": validation_period or {},
        "wf_score": top.wf_score,
        "search": {
            "total_window_count": len(top.wf_stats),
            "ranking_window_count": len(top.ranking_stats),
            "validation_window_count": len(top.validation_stats),
            "purged_overlap_window_count": top.purged_window_count,
            "score_formula": (
                "weighted_primary_benchmark_excess - stability_penalty * std "
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
        },
        "sensitivity": dict(top.sensitivity),
    }
    from pathlib import Path

    out_dir = Path(output_dir) if output_dir is not None else Path("data/optimizer")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{group}_best_params.yaml"
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    logger.info(f"最优参数已保存: {path}")
