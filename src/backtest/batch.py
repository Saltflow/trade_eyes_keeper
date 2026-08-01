"""Columnar CPU batch backend for the unified backtester.

The kernel parallelizes independent candidates while preserving the serial
date loop inside each portfolio.  It deliberately handles only the generic
``cash_cap`` execution contract; unsupported execution models return ``None``
and are evaluated by the scalar-compatible path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .execution import DEFAULT_FILL_PRICE_POLICY

if TYPE_CHECKING:
    from .engine import FastEvaluator
    from ..search.config import WindowStats
    from ..strategy import TradePlan

try:
    from numba import jit, prange

    HAS_NUMBA_BATCH = True
except ImportError:
    HAS_NUMBA_BATCH = False

    def jit(*args, **kwargs):
        def decorator(function):
            return function

        return decorator

    def prange(*args):
        return range(*args)


if HAS_NUMBA_BATCH:
    from .engine import _simulate_cash_plan_numba

    @jit(nopython=True, parallel=True, cache=True, nogil=True)
    def _simulate_cash_batch(
        buy_signals,
        sell_signals,
        buy_priority,
        sell_priority,
        valuation_prices,
        buy_prices,
        sell_prices,
        tradable,
        initial_cash,
        buy_cash_limits,
        sell_cash_limits,
        lot_size,
        commission_rate,
        min_holding_days,
    ):
        candidate_count, row_count, symbol_count = buy_signals.shape
        daily_values = np.zeros((candidate_count, row_count), dtype=np.float64)
        trade_counts = np.zeros(candidate_count, dtype=np.int64)
        avg_position_pct = np.zeros(candidate_count, dtype=np.float64)
        final_position_pct = np.zeros(candidate_count, dtype=np.float64)
        final_shares = np.zeros((candidate_count, symbol_count), dtype=np.float64)
        final_cash = np.zeros(candidate_count, dtype=np.float64)
        cost_basis = np.zeros((candidate_count, symbol_count), dtype=np.float64)
        pending_orders = np.zeros(candidate_count, dtype=np.int64)
        snapshot_mask = np.zeros(row_count, dtype=np.bool_)
        for candidate_index in prange(candidate_count):
            result = _simulate_cash_plan_numba(
                buy_signals[candidate_index],
                sell_signals[candidate_index],
                buy_priority[candidate_index],
                sell_priority[candidate_index],
                valuation_prices,
                buy_prices,
                sell_prices,
                tradable,
                initial_cash,
                buy_cash_limits[candidate_index],
                sell_cash_limits[candidate_index],
                lot_size,
                commission_rate,
                min_holding_days,
                snapshot_mask,
            )
            daily_values[candidate_index] = result[0]
            trade_counts[candidate_index] = result[1]
            avg_position_pct[candidate_index] = result[2]
            final_position_pct[candidate_index] = result[3]
            final_shares[candidate_index] = result[4]
            final_cash[candidate_index] = result[5]
            cost_basis[candidate_index] = result[6]
            pending_orders[candidate_index] = result[12]
        return (
            daily_values,
            trade_counts,
            avg_position_pct,
            final_position_pct,
            final_shares,
            final_cash,
            cost_basis,
            pending_orders,
        )


def evaluate_cash_batch(
    evaluator: "FastEvaluator",
    trade_plans: list["TradePlan"],
    window_inputs: dict[str, object],
) -> list["WindowStats"] | None:
    """Evaluate a shared-window cash-cap batch or request scalar fallback."""
    if not HAS_NUMBA_BATCH or any(
        str(plan.execution.get("model", "cash_cap")) != "cash_cap"
        for plan in trade_plans
    ):
        return None
    if any(
        plan.buy_cash_limit <= 0.0 or plan.sell_cash_limit <= 0.0
        for plan in trade_plans
    ):
        return None

    from .engine import WindowStats, _compute_stats

    indicator_matrix = np.asarray(window_inputs["indicator_matrix"])
    price_matrix = np.asarray(window_inputs["price_matrix"])
    row_count, symbol_count = indicator_matrix.shape[:2]
    if row_count == 0 or symbol_count == 0:
        return [WindowStats() for _plan in trade_plans]
    expected_shape = (row_count, symbol_count)
    if any(plan.buy_signals.shape != expected_shape for plan in trade_plans):
        raise ValueError("TradePlan and evaluator matrix shapes do not match")

    execution_prices = window_inputs.get("execution_prices")
    if execution_prices is None:
        execution_prices = DEFAULT_FILL_PRICE_POLICY.build(price_matrix)
    resolved_prices = execution_prices.scaled(evaluator.fx_rate)
    if resolved_prices.valuation_prices.shape != expected_shape:
        raise ValueError("ExecutionPriceSlice and evaluator matrix shapes do not match")
    tradable = window_inputs.get("tradable")
    if tradable is None:
        tradable = resolved_prices.tradable
    valuation_prices = np.asarray(
        resolved_prices.valuation_prices, dtype=np.float32
    ).copy()
    for symbol_index in range(symbol_count):
        last = 0.0
        for row_index in range(row_count):
            price = valuation_prices[row_index, symbol_index]
            if np.isfinite(price) and price > 0.0:
                last = price
            elif last > 0.0:
                valuation_prices[row_index, symbol_index] = last
            else:
                valuation_prices[row_index, symbol_index] = 0.0

    result = _simulate_cash_batch(
        np.ascontiguousarray(
            np.stack([plan.buy_signals for plan in trade_plans]), dtype=np.bool_
        ),
        np.ascontiguousarray(
            np.stack([plan.sell_signals for plan in trade_plans]), dtype=np.bool_
        ),
        np.ascontiguousarray(
            np.stack([plan.buy_priority for plan in trade_plans]), dtype=np.float32
        ),
        np.ascontiguousarray(
            np.stack([plan.sell_priority for plan in trade_plans]), dtype=np.float32
        ),
        np.ascontiguousarray(valuation_prices, dtype=np.float32),
        np.ascontiguousarray(resolved_prices.buy_prices, dtype=np.float32),
        np.ascontiguousarray(resolved_prices.sell_prices, dtype=np.float32),
        np.ascontiguousarray(tradable, dtype=np.bool_),
        float(evaluator.initial_cash),
        np.asarray([plan.buy_cash_limit for plan in trade_plans]),
        np.asarray([plan.sell_cash_limit for plan in trade_plans]),
        int(evaluator.lot_size),
        float(evaluator.commission_rate),
        int(evaluator.min_holding_days),
    )
    empty_shares = np.zeros((0, symbol_count), dtype=np.float64)
    empty_values = np.zeros(0, dtype=np.float64)
    return [
        _compute_stats(
            result[0][index],
            valuation_prices,
            window_inputs.get("cash_baseline"),
            int(result[1][index]),
            0,
            avg_pos_pct=float(result[2][index]),
            benchmark_series=window_inputs.get("benchmark_series"),
            benchmark_initial_values=window_inputs.get("benchmark_initial_values"),
            benchmark_raw_returns=window_inputs.get("benchmark_raw_returns"),
            initial_asset=evaluator.initial_cash,
            final_pos_pct=float(result[3][index]),
            final_shares=result[4][index].copy(),
            final_cash=float(result[5][index]),
            cost_basis=result[6][index].copy(),
            quarter_shares=empty_shares.copy(),
            quarter_cash=empty_values.copy(),
            quarter_nav=empty_values.copy(),
            quarter_prices=empty_shares.copy(),
            quarter_cost_basis=empty_shares.copy(),
            pending_order_count=int(result[7][index]),
        )
        for index in range(len(trade_plans))
    ]
