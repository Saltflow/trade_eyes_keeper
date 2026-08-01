"""Regression tests for the strategy-independent execution-price contract."""

from __future__ import annotations

import numpy as np
import pytest

from src.analysis.config import ExecutionConfig
from src.analysis.execution import DEFAULT_FILL_PRICE_POLICY
from src.analysis.search_interface import StrategyMarketData, TradePlan
from src.analysis.backtester import FastEvaluator, simulate_portfolio


def test_fill_policy_uses_window_edges_without_crossing_window_end():
    close = np.full((6, 1), 10.0)
    high = np.array([[11.0], [12.0], [13.0], [14.0], [15.0], [99.0]])
    low = np.arange(9.0, 3.0, -1.0).reshape(-1, 1)

    prices = DEFAULT_FILL_PRICE_POLICY.build(close, high, low, start=1, end=5)

    assert prices.buy_prices[:, 0].tolist()[:3] == [13.0, 14.0, 15.0]
    assert np.isnan(prices.buy_prices[-1, 0])
    assert prices.sell_prices[:, 0].tolist() == [8.0, 7.0, 6.0, 5.0]
    assert prices.valuation_prices[:, 0].tolist() == [10.0] * 4


def test_cash_cap_optimizer_and_daily_share_stress_fills_and_close_nav():
    rows = 33
    close = np.full((rows, 1), 10.0, dtype=np.float32)
    high = np.full((rows, 1), 12.0, dtype=np.float32)
    low = np.full((rows, 1), 8.0, dtype=np.float32)
    execution_prices = DEFAULT_FILL_PRICE_POLICY.build(close, high, low)
    buy = np.zeros_like(close, dtype=bool)
    sell = np.zeros_like(close, dtype=bool)
    buy[0, 0] = True
    sell[31, 0] = True
    buy[-1, 0] = True
    plan = TradePlan(
        buy_signals=buy,
        sell_signals=sell,
        buy_priority=np.where(buy, 1.0, -np.inf),
        sell_priority=np.where(sell, 1.0, -np.inf),
        buy_cash_limit=20_000.0,
        sell_cash_limit=20_000.0,
        warmup_rows=0,
        execution={"model": "cash_cap"},
    )
    config = ExecutionConfig(
        initial_capital=100_000.0,
        commission_rate=0.0,
        min_holding_days=30,
        lot_sizes={"a_share": 100},
    )
    evaluator = FastEvaluator(config, "a_share")

    fast = evaluator.evaluate(
        indicator_matrix=np.zeros((rows, 1, 22), dtype=np.float32),
        price_matrix=close,
        cash_baseline=np.full(rows, 100_000.0),
        trade_plan=plan,
        execution_prices=execution_prices,
    )
    daily = simulate_portfolio(
        plan,
        StrategyMarketData(
            indicator_matrix=np.zeros((rows, 1, 22), dtype=np.float32),
            dates=[f"2026-01-{index + 1:02d}" for index in range(rows)],
            symbols=["000001"],
            prices=close,
            highs=high,
            lows=low,
            tradable=np.ones_like(close, dtype=bool),
        ),
        initial_cash=100_000.0,
        lot_size=100,
        commission_rate=0.0,
        min_holding_days=30,
        execution_prices=execution_prices,
    )

    assert fast.strategy_return == pytest.approx(-6.4)
    assert daily.total_return_pct == pytest.approx(-6.4)
    assert fast.final_asset == pytest.approx(daily.daily_values[-1])
    assert fast.total_trades == daily.total_trades == 2
    assert fast.pending_order_count == daily.pending_order_count == 1


def test_policy_never_changes_past_prices_when_future_rows_are_appended():
    prefix = np.array([[100.0], [90.0], [80.0], [70.0]])
    extended = np.vstack([prefix, [[500.0]]])

    prefix_prices = DEFAULT_FILL_PRICE_POLICY.build(prefix, prefix, prefix)
    extended_prices = DEFAULT_FILL_PRICE_POLICY.build(
        extended, extended, extended, end=len(prefix)
    )

    assert np.array_equal(
        prefix_prices.valuation_prices,
        extended_prices.valuation_prices,
    )
    assert np.array_equal(
        prefix_prices.sell_prices,
        extended_prices.sell_prices,
    )
    # The last fill is pending in both slices; earlier fills are identical.
    assert np.array_equal(
        prefix_prices.buy_prices[:-1],
        extended_prices.buy_prices[:-1],
    )
    assert np.isnan(prefix_prices.buy_prices[-1, 0])
    assert np.isnan(extended_prices.buy_prices[-1, 0])
