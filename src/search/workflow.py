"""Unified strategy-search orchestration and walk-forward evaluation helpers."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .config import (
    StrategyConstraints,
    WindowStats,
    get_constraints,
)
from ..backtest.execution import DEFAULT_FILL_PRICE_POLICY
from ..markets import _detect_fine_group
from .gates import majority_benchmark_excess
from ..strategy import TradingStrategy, Params, StrategyMarketData
from .artifacts import as_yaml_primitives

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 策略编码
# ═══════════════════════════════════════════════════════════════


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


def _prepare_wf_evaluation_contexts(
    windows: list,
    state_scope: str,
    constraints: StrategyConstraints,
    evaluator,
    wf_manager,
) -> dict[str, object]:
    """Cache parameter-independent window inputs and executable baselines."""
    from ..backtest.engine import buy_and_hold_nav

    geometry = tuple((w.train_start, w.test_start, w.test_end) for w in windows)
    cache_key = (
        state_scope,
        geometry,
        float(evaluator.initial_cash),
        float(evaluator.commission_rate),
        int(evaluator.lot_size),
        float(evaluator.fx_rate),
        float(constraints.risk_free_rate),
        tuple(constraints.benchmark_codes),
        tuple(sorted(constraints.execution.lot_sizes.items())),
        tuple(sorted(constraints.execution.fx_rates.items())),
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

        risk_free_daily = (1.0 + constraints.risk_free_rate) ** (1.0 / 252) - 1.0
        cash_baseline = evaluator.initial_cash * (
            1.0 + risk_free_daily
        ) ** np.arange(test_indicators.shape[0], dtype=np.float64)
        benchmark_series = {"risk_free": cash_baseline}
        benchmark_initial_values = {"risk_free": evaluator.initial_cash}
        benchmark_raw_returns = {
            "risk_free": float((cash_baseline[-1] / cash_baseline[0] - 1.0) * 100.0)
        }
        for benchmark_code in constraints.benchmark_codes:
            if benchmark_code == "risk_free":
                continue
            if benchmark_code not in available_benchmarks:
                logger.warning(
                    "Configured benchmark %s is unavailable for %s",
                    benchmark_code,
                    market_group,
                )
                continue
            raw_close = np.asarray(
                available_benchmarks[benchmark_code], dtype=np.float64
            )
            raw_high = np.asarray(
                benchmark_high_series.get(benchmark_code, raw_close),
                dtype=np.float64,
            )
            benchmark_execution = DEFAULT_FILL_PRICE_POLICY.build(
                raw_close,
                raw_high,
                raw_close,
                start=window.test_start,
                end=window.test_end,
            )
            benchmark_market = _detect_fine_group(benchmark_code)
            benchmark_fx = float(
                constraints.execution.fx_rates.get(benchmark_market, 1.0)
            )
            benchmark_lot = int(
                constraints.execution.lot_sizes.get(benchmark_market, 1)
            )
            resolved_benchmark = benchmark_execution.scaled(benchmark_fx)
            benchmark_close = resolved_benchmark.valuation_prices[:, 0]
            benchmark_buy = resolved_benchmark.buy_prices[:, 0]
            if (
                len(benchmark_close) < 2
                or not np.isfinite(benchmark_buy[0])
                or not np.isfinite(benchmark_close[[0, -1]]).all()
                or np.any(benchmark_close[[0, -1]] <= 0)
            ):
                logger.warning(
                    "Configured benchmark %s has no executable window entry",
                    benchmark_code,
                )
                continue
            benchmark_series[benchmark_code] = buy_and_hold_nav(
                benchmark_close,
                benchmark_buy,
                evaluator.initial_cash,
                benchmark_lot,
                evaluator.commission_rate,
                weights=np.array([1.0]),
            )
            benchmark_initial_values[benchmark_code] = evaluator.initial_cash
            raw_window = benchmark_execution.valuation_prices[:, 0]
            benchmark_raw_returns[benchmark_code] = float(
                (raw_window[-1] / raw_window[0] - 1.0) * 100.0
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


def _evaluate_params_wf(
    params: Params,
    strategy: TradingStrategy,
    windows: list,
    constraints: StrategyConstraints,
    evaluator,
    wf_manager,
    validation_window_count: int | None = None,
    include_candidate_diagnostics: bool = False,
):
    """Evaluate one typed parameter set across the requested WF windows."""
    all_stats = []
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
        if include_candidate_diagnostics:
            from ..backtest.engine import selected_basket_hold_return

            resolved = context["execution_prices"].scaled(evaluator.fx_rate)
            basket_return = selected_basket_hold_return(
                trade_plan,
                resolved.valuation_prices,
                resolved.buy_prices,
                resolved.tradable,
                float(evaluator.initial_cash),
                int(evaluator.lot_size),
                float(evaluator.commission_rate),
            )
            stats.selected_basket_hold_return = basket_return
            stats.timing_value_add = round(
                float(stats.strategy_return) - basket_return, 2
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


def run_optimizer(
    strategy: TradingStrategy,
    stocks_data: dict,
    stock_codes: list[str],
    group: str = "a_share",
    _constraints=None,
    output_dir=None,
    benchmark_data: dict | None = None,
) -> tuple[list, StrategyConstraints]:
    """Run the configured Solver through the stable SearchController."""
    from ..backtest.engine import WalkForwardManager, FastEvaluator
    from .gates import CandidateGatePipeline
    from .config import get_constraints
    from .evaluator import EvaluationService
    from ..strategy.features import TECHNICAL_FEATURES
    from .resources import ResourcePlanner
    from .archive import SearchArchive
    from .contracts import SearchProblem, SolverCapabilities, stable_hash
    from .controller import SearchController
    from .registry import create_solver
    from .validation import ValidationController

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

    schema = strategy.parameter_schema
    gate_pipeline = CandidateGatePipeline.from_config(
        constraints._raw_config, constraints.search.gate_profile
    )
    solver_config = constraints.search.solver_config()
    fallback_budget = (
        constraints.genetic_search.phase1_random_samples
        + constraints.genetic_search.num_generations
        * constraints.genetic_search.offspring_size
    )
    budget = max(1, int(solver_config.get("budget", fallback_budget)))

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
    data_hasher.update(
        ("controls:" + "|".join(constraints.benchmark_codes)).encode("utf-8")
    )
    for benchmark_code in constraints.benchmark_codes:
        for series_name, source in (
            ("close", wf_manager.benchmark_series),
            ("high", wf_manager.benchmark_high_series),
        ):
            values = source.get(benchmark_code)
            data_hasher.update(f"{benchmark_code}:{series_name}:".encode("utf-8"))
            if values is None:
                data_hasher.update(b"missing")
                continue
            contiguous = np.ascontiguousarray(values)
            data_hasher.update(str(contiguous.shape).encode("ascii"))
            data_hasher.update(str(contiguous.dtype).encode("ascii"))
            data_hasher.update(contiguous.view(np.uint8))
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
            "risk_free_rate": constraints.risk_free_rate,
            "control_benchmarks": constraints.benchmark_codes,
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
        objective_id="weighted-strongest-configured-excess-stability-sharpe/2",
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
        metadata={
            "strategy_id": strategy.name,
            "market": group,
            "control_benchmarks": tuple(constraints.benchmark_codes),
        },
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
        evaluation_backend=constraints.search.evaluation_backend,
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
    try:
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
    finally:
        service.close()

    performance = service.performance_snapshot()
    search_metadata = {
        "solver_id": solver.solver_id,
        "solver_config": dict(
            getattr(solver, "effective_config", solver_config)
        ),
        "solver_stop_reason": getattr(solver, "stop_reason", None),
        "solver_total_issued": int(
            getattr(solver, "total_issued", getattr(solver, "issued", budget))
        ),
        "solver_unique_parameters": len(
            getattr(solver, "seen_parameter_keys", ())
        )
        or int(performance.get("evaluated", 0)),
        "gate_profile": gate_pipeline.profile_id,
        "gate_profile_hash": gate_pipeline.hash,
        "gate_activation_eligible": gate_pipeline.activation_eligible,
        "search_contract_hash": problem.contract_hash,
        "parameter_schema_hash": schema.hash,
        "feature_contract_hash": feature_hash,
        "data_contract_hash": data_hash,
        "execution_contract_hash": execution_hash,
        "window_contract_hash": window_hash,
        "control_benchmarks": list(constraints.benchmark_codes),
        "ranking_window_indexes": ranking_indexes,
        "purged_window_count": len(purged_indexes),
        "validation_window_count": len(validation_indexes),
        "resource_plan": resource_plan.__dict__,
        "performance": performance,
        "budget": budget,
    }
    for item in validated:
        item.search_metadata = dict(search_metadata)
    results = validated
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
    params = Params(values=dict(top.parameters), _engine=strategy.name)
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
            "majority_benchmark_excess": float(
                majority_benchmark_excess(stat, constraints.benchmark_codes)
            ),
            "benchmark_raw_returns": dict(
                getattr(stat, "benchmark_raw_returns", {}) or {}
            ),
            "strongest_benchmark": str(getattr(stat, "strongest_benchmark", "") or ""),
            "pending_order_count": int(getattr(stat, "pending_order_count", 0)),
            "signal_event_count": int(getattr(stat, "signal_event_count", 0)),
            "cash_rejected_order_count": int(
                getattr(stat, "cash_rejected_order_count", 0)
            ),
            "concentration_hhi": float(getattr(stat, "concentration_hhi", 0.0)),
            "selected_basket_hold_return": getattr(
                stat, "selected_basket_hold_return", None
            ),
            "timing_value_add": getattr(stat, "timing_value_add", None),
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
            top.all_stats[index],
            windows[index] if index < len(windows) else None,
        )
        for index in range(
            len(top.ranking_stats),
            min(validation_offset, len(top.all_stats)),
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
        np.isfinite(majority_benchmark_excess(stat, constraints.benchmark_codes))
        and majority_benchmark_excess(stat, constraints.benchmark_codes) > 0
        for stat in top.validation_stats
    )
    required_benchmarks = set(constraints.benchmark_codes)
    benchmarks_complete = all(
        len(required_benchmarks) == 3
        and
        required_benchmarks.issubset(set(stat.benchmark_returns))
        for stat in top.all_stats
    )
    local_robustness_passed = bool(top.sensitivity) and (
        float(top.sensitivity.get("worst_score", float("-inf"))) > 0
        and float(
            top.selection_score if top.selection_score is not None else float("-inf")
        )
        > 0
    )
    universe_robustness = dict(getattr(top, "universe_robustness", {}) or {})
    universe_config = constraints.universe_robustness
    universe_robustness_passed = (
        not universe_config.activation_required
        or (
            bool(universe_robustness)
            and bool(universe_robustness.get("passed"))
        )
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
        "parameter_schema": getattr(
            strategy,
            "parameter_schema_id",
            "parameter-space/1",
        ),
        "solver_id": search_metadata.get("solver_id", "genetic"),
        "solver_config": dict(search_metadata.get("solver_config", {})),
        "gate_profile": search_metadata.get("gate_profile", "standard"),
        "control_benchmarks": list(constraints.benchmark_codes),
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
        "wf_score": top.objective_score,
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
            "total_window_count": len(top.all_stats),
            "ranking_window_count": len(top.ranking_stats),
            "validation_window_count": len(top.validation_stats),
            "purged_overlap_window_count": top.purged_window_count,
            "budget": int(search_metadata.get("budget", 0)),
            "solver_id": search_metadata.get("solver_id", "genetic"),
            "gate_profile": search_metadata.get("gate_profile", "standard"),
            "score_formula": (
                "weighted_strongest_configured_benchmark_excess "
                "- stability_penalty * std "
                "- sharpe_penalty"
            ),
            "selection_formula": (
                f"wf_score - {constraints.genetic_search.sensitivity_penalty_weight:g} "
                "* parameter_sensitivity_drop - "
                f"{universe_config.penalty_weight:g} * universe_sensitivity_drop"
            ),
            "universe_robustness_config": universe_config.to_contract(),
            "selection_score": top.selection_score,
            "ranking_diagnostics": dict(top.ranking_metrics),
            "gate_results": [
                dict(item) for item in getattr(top, "gate_results", ())
            ],
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
    logger.info("Selected optimizer parameters saved: %s", path)
