"""Explicit manual walk-forward evaluation; no parameter search or publication."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..backtest.execution import DEFAULT_FILL_PRICE_POLICY, ExecutionPriceSlice
from ..markets import _detect_fine_group
from ..strategy import Params, StrategyMarketData, TradingStrategy
from .config import (
    StrategyConstraints,
    WindowStats,
)

logger = logging.getLogger(__name__)


def majority_benchmark_excess(stat: object, control_benchmarks) -> float:
    """Return excess over the median of three declared control benchmarks."""
    benchmarks = dict(getattr(stat, "benchmark_returns", {}) or {})
    controls = tuple(dict.fromkeys(str(name) for name in control_benchmarks))
    if len(controls) != 3 or not all(name in benchmarks for name in controls):
        return float("nan")
    hurdle = float(np.median([float(benchmarks[name]) for name in controls]))
    return float(getattr(stat, "strategy_return", 0.0)) - hurdle


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
    candidate_indexes = list(range(len(windows) - held_out))
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
    """Score median-control excess and penalize its cross-window range."""
    if not ranking_stats:
        return None

    returns = [
        majority_benchmark_excess(stat, constraints.benchmark_codes)
        for stat in ranking_stats
    ]
    if not all(np.isfinite(value) for value in returns):
        return None
    weights = constraints.walk_forward.ranking_weights(len(returns))
    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0 / len(returns)] * len(returns)
    else:
        weights = [weight / total_weight for weight in weights]

    weighted_mean = float(
        sum(value * weight for value, weight in zip(returns, weights))
    )
    window_range = float(max(returns) - min(returns)) if len(returns) >= 2 else 0.0
    return weighted_mean - (
        constraints.walk_forward.window_range_penalty * window_range
    )


def _prepare_wf_evaluation_contexts(
    windows: list,
    state_scope: str,
    constraints: StrategyConstraints,
    evaluator,
    wf_manager,
    *,
    strategy: TradingStrategy | None = None,
    context_enricher=None,
) -> dict[str, object]:
    """Cache parameter-independent window inputs and executable baselines."""
    from ..backtest.engine import buy_and_hold_nav

    context_enricher = context_enricher or getattr(
        wf_manager, "market_data_enricher", None
    )
    required_context = tuple(
        getattr(strategy, "fundamental_feature_dependencies", ()) or ()
    )
    if required_context and context_enricher is None:
        raise ValueError(
            f"strategy {getattr(strategy, 'name', '<unknown>')} requires a "
            "historical fundamental context enricher"
        )
    context_hash = str(getattr(context_enricher, "contract_hash", ""))

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
        tuple(sorted(constraints.execution.withholding_rates.items())),
        context_hash,
    )
    cache = getattr(wf_manager, "_evaluation_context_cache", None)
    if cache is None:
        cache = {}
        wf_manager._evaluation_context_cache = cache
    if cache_key in cache:
        return cache[cache_key]

    parsed_dates = pd.to_datetime(wf_manager.dates)
    date_strings = [str(pd.Timestamp(date).date()) for date in parsed_dates]
    date_ordinals = parsed_dates.values.astype("datetime64[D]").astype(np.int64)
    symbols = list(wf_manager.stock_codes)
    price_matrix = wf_manager.price_matrix
    high_matrix = wf_manager.price_high_matrix
    low_matrix = wf_manager.price_low_matrix
    raw_price_matrix = getattr(wf_manager, "raw_price_matrix", price_matrix)
    raw_high_matrix = getattr(wf_manager, "raw_price_high_matrix", high_matrix)
    raw_low_matrix = getattr(wf_manager, "raw_price_low_matrix", low_matrix)
    market_group = str(getattr(wf_manager, "market_group", "a_share"))
    lifetime_observation_counts = np.cumsum(
        np.isfinite(price_matrix) & (price_matrix > 0), axis=0, dtype=np.int32
    )
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
        observation_counts=lifetime_observation_counts,
    )
    if context_enricher is not None:
        full_market_data = context_enricher(full_market_data)

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
            observation_counts=lifetime_observation_counts[state_slice],
        )
        if context_enricher is not None:
            signal_market_data = context_enricher(signal_market_data)
        execution_prices = DEFAULT_FILL_PRICE_POLICY.build(
            raw_price_matrix,
            raw_high_matrix,
            raw_low_matrix,
            corporate_actions=getattr(wf_manager, "action_schedule", None),
            start=window.test_start,
            end=window.test_end,
        )

        risk_free_daily = (1.0 + constraints.risk_free_rate) ** (1.0 / 252) - 1.0
        cash_baseline = evaluator.initial_cash * (1.0 + risk_free_daily) ** np.arange(
            test_indicators.shape[0], dtype=np.float64
        )
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
            benchmark_low_series = getattr(wf_manager, "benchmark_low_series", {})
            raw_low = np.asarray(
                benchmark_low_series.get(benchmark_code, raw_close),
                dtype=np.float64,
            )
            benchmark_execution = DEFAULT_FILL_PRICE_POLICY.build(
                raw_close,
                raw_high,
                raw_low,
                corporate_actions=getattr(
                    wf_manager, "benchmark_action_schedules", {}
                ).get(benchmark_code),
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
            if resolved_benchmark.corporate_actions is not None:
                resolved_benchmark = ExecutionPriceSlice(
                    valuation_prices=resolved_benchmark.valuation_prices,
                    buy_prices=resolved_benchmark.buy_prices,
                    sell_prices=resolved_benchmark.sell_prices,
                    tradable=resolved_benchmark.tradable,
                    corporate_actions=resolved_benchmark.corporate_actions.with_withholding(
                        float(
                            constraints.execution.withholding_rates.get(
                                benchmark_market, 0.0
                            )
                        )
                    ),
                )
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
                corporate_actions=resolved_benchmark.corporate_actions,
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
        windows,
        state_scope,
        constraints,
        evaluator,
        wf_manager,
        strategy=strategy,
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
                resolved.corporate_actions,
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
