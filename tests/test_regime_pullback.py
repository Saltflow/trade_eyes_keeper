"""Contracts for the regime_pullback state machine and target execution."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.backtester import (
    IDX_ADX,
    IDX_ATR,
    IDX_BOLL_PCT_B,
    IDX_CLOSE,
    IDX_DEVIATION,
    IDX_MACD_HIST,
    IDX_MA200,
    IDX_MA200_SLOPE,
    IDX_MINUS_DI,
    IDX_PLUS_DI,
    IDX_RSI,
    IDX_RSI_PCT,
    INDICATOR_NAMES,
    simulate_portfolio,
)
from src.analysis.execution import DEFAULT_FILL_PRICE_POLICY
from src.analysis.search_interface import (
    Params,
    StrategyMarketData,
    TradePlan,
    allocate_target_weights,
)
from src.analysis.strategies import get_strategy


def _params():
    strategy = get_strategy("regime_pullback")
    values = {dim.name: 0 for dim in strategy.param_space.dims}
    values.update(
        {
            "rsi_setup_weight": 2,
            "deviation_setup_weight": 0,
            "boll_setup_weight": 0,
            "rsi_recovery_weight": 0,
            "price_recovery_weight": 2,
            "macd_recovery_weight": 0,
            "exit_rsi": 3,
            "exit_deviation": 3,
            "per_symbol_cap": 1,
            "total_exposure_cap": 1,
        }
    )
    return Params(values=values, _engine=strategy.name)


def _market(rows=340, columns=1):
    matrix = np.zeros((rows, columns, len(INDICATOR_NAMES)), dtype=np.float32)
    matrix[:, :, IDX_CLOSE] = 110.0
    matrix[:, :, IDX_MA200] = 100.0
    matrix[:, :, IDX_MA200_SLOPE] = 0.01
    matrix[:, :, IDX_ADX] = 25.0
    matrix[:, :, IDX_PLUS_DI] = 30.0
    matrix[:, :, IDX_MINUS_DI] = 10.0
    matrix[:, :, IDX_RSI_PCT] = 0.10
    matrix[:, :, IDX_DEVIATION] = -0.05
    matrix[:, :, IDX_BOLL_PCT_B] = 0.10
    matrix[:, :, IDX_RSI] = 40.0
    matrix[:, :, IDX_MACD_HIST] = np.arange(rows).reshape(-1, 1)
    matrix[:, :, IDX_ATR] = 2.0
    dates = pd.bdate_range("2024-01-02", periods=rows).strftime(
        "%Y-%m-%d"
    ).tolist()
    prices = matrix[:, :, IDX_CLOSE].copy()
    return StrategyMarketData(
        indicator_matrix=matrix,
        dates=dates,
        symbols=[f"S{index}" for index in range(columns)],
        prices=prices,
        highs=prices + 1.0,
        lows=prices - 1.0,
        tradable=np.ones_like(prices, dtype=bool),
    )


def test_registered_strategy_emits_one_shot_three_day_confirmation():
    strategy = get_strategy("regime_pullback")
    market = _market()
    plan = strategy.make_signals(_params(), market)
    entries = np.flatnonzero(plan.entry_events[:, 0])

    assert entries.tolist() == [253]
    assert plan.execution["model"] == "target_weight"
    assert plan.target_weights[253, 0] == pytest.approx(0.20)
    assert not plan.entry_events[254:, 0].any()
    assert plan.strategy_metadata["parameter_schema"] == "regime_pullback/1"


def test_invalid_condition_rearms_and_catastrophe_can_exit_before_30_days():
    strategy = get_strategy("regime_pullback")
    market = _market()
    # The first confirmed entry is row 253.  A 10-point drawdown with ATR=2
    # while below MA200 is greater than the fixed 3ATR catastrophe threshold.
    market.indicator_matrix[257, 0, IDX_CLOSE] = 99.0
    market.prices[257, 0] = 99.0
    market.highs[257, 0] = 100.0
    market.lows[257, 0] = 98.0
    plan = strategy.make_signals(_params(), market)

    assert plan.entry_events[253, 0]
    assert plan.force_exit_signals[257, 0]
    assert plan.entry_events[260, 0]


def test_training_event_is_not_replayed_at_test_start():
    strategy = get_strategy("regime_pullback")
    plan = strategy.make_signals(_params(), _market())
    sliced = plan.sliced(260, 320)

    assert not sliced.entry_events[0, 0]
    assert not sliced.entry_events.any()


def test_target_allocator_is_permutation_invariant_and_respects_caps():
    scores = np.array([1.0, 1.0, 2.0, 4.0])
    active = np.ones(4, dtype=bool)
    weights = allocate_target_weights(scores, active, 0.25, 0.80)
    order = np.array([3, 1, 0, 2])
    permuted = allocate_target_weights(
        scores[order], active[order], 0.25, 0.80
    )

    assert weights.sum() == pytest.approx(0.80)
    assert weights.max() <= 0.25
    assert np.array_equal(weights[order], permuted)


def test_target_execution_uses_stress_high_low_fee_and_pending_final_buy():
    dates = pd.date_range("2026-01-01", periods=40).strftime("%Y-%m-%d").tolist()
    close = np.full((40, 1), 10.0, dtype=np.float32)
    highs = close.copy()
    highs[0, 0] = 11.0
    highs[1, 0] = 12.0
    highs[2, 0] = 13.0
    lows = np.full_like(close, 9.0)
    entry = np.zeros_like(close, dtype=bool)
    normal_exit = np.zeros_like(close, dtype=bool)
    entry[1, 0] = True
    normal_exit[32, 0] = True
    entry[-1, 0] = True
    conviction = np.where(entry, 1.0, 0.0).astype(np.float32)
    plan = TradePlan(
        buy_signals=entry,
        sell_signals=normal_exit,
        buy_priority=np.where(entry, 1.0, -np.inf),
        sell_priority=np.where(normal_exit, 1.0, -np.inf),
        buy_cash_limit=0.0,
        sell_cash_limit=0.0,
        warmup_rows=0,
        dates=dates,
        symbols=["000001"],
        execution={
            "model": "target_weight",
            "per_symbol_cap": 0.20,
            "total_exposure_cap": 0.80,
            "min_holding_calendar_days": 30,
        },
        entry_events=entry,
        exit_events=normal_exit,
        force_exit_signals=np.zeros_like(entry),
        conviction=conviction,
    )
    trace = simulate_portfolio(
        plan,
        StrategyMarketData(
            indicator_matrix=np.zeros((40, 1, 0), dtype=np.float32),
            dates=dates,
            symbols=["000001"],
            prices=close,
            highs=highs,
            lows=lows,
            tradable=np.ones_like(close, dtype=bool),
        ),
        initial_cash=100_000.0,
        lot_size=100,
        commission_rate=0.005,
        min_holding_days=30,
        execution_prices=DEFAULT_FILL_PRICE_POLICY.build(close, highs, lows),
    )

    # Target 20k at close=10 means 2,000 desired shares, scaled only if cash
    # requires it; the actual cost basis is the t-1/t/t+1 max high = 13.
    assert trace.total_trades == 2
    assert trace.final_shares[0] == 0
    assert trace.pending_order_count == 1
    assert trace.final_cash < 100_000.0


def test_target_execution_consumes_plan_declared_target_weight():
    dates = pd.date_range("2026-01-01", periods=3).strftime("%Y-%m-%d").tolist()
    close = np.full((3, 1), 10.0, dtype=np.float32)
    entry = np.zeros_like(close, dtype=bool)
    entry[0, 0] = True
    plan = TradePlan(
        buy_signals=entry,
        sell_signals=np.zeros_like(entry),
        buy_priority=np.where(entry, 1.0, -np.inf),
        sell_priority=np.full_like(close, -np.inf),
        buy_cash_limit=0.0,
        sell_cash_limit=0.0,
        warmup_rows=0,
        dates=dates,
        symbols=["000001"],
        execution={
            "model": "target_weight",
            "per_symbol_cap": 0.25,
            "total_exposure_cap": 0.80,
            "min_holding_calendar_days": 30,
        },
        entry_events=entry,
        exit_events=np.zeros_like(entry),
        force_exit_signals=np.zeros_like(entry),
        conviction=np.where(entry, 1.0, 0.0).astype(np.float32),
        target_weights=np.full_like(close, 0.15),
    )
    trace = simulate_portfolio(
        plan,
        StrategyMarketData(
            indicator_matrix=np.zeros((3, 1, 0), dtype=np.float32),
            dates=dates,
            symbols=["000001"],
            prices=close,
            tradable=np.ones_like(close, dtype=bool),
        ),
        initial_cash=100_000.0,
        lot_size=100,
        commission_rate=0.005,
        min_holding_days=30,
        execution_prices=DEFAULT_FILL_PRICE_POLICY.build(close),
    )

    # The execution cap permits 25%, but the plan explicitly asks for 15%.
    assert trace.final_shares[0] == 1_500


def test_trade_plan_slice_contains_decisions_but_never_concrete_fill_prices():
    values = np.arange(1, 8, dtype=np.float32).reshape(-1, 1)
    plan = TradePlan(
        buy_signals=np.ones_like(values, dtype=bool),
        sell_signals=np.zeros_like(values, dtype=bool),
        buy_priority=values,
        sell_priority=np.zeros_like(values),
        buy_cash_limit=0,
        sell_cash_limit=0,
        warmup_rows=0,
    )
    sliced = plan.sliced(1, 5)
    assert sliced.buy_signals.shape == (4, 1)
    assert not hasattr(sliced, "buy_execution_prices")
    assert not hasattr(sliced, "sell_execution_prices")
    assert np.all(sliced.buy_signals)
