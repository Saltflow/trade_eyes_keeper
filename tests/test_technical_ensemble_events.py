from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import FastEvaluator, simulate_portfolio
from src.backtest.execution import DEFAULT_FILL_PRICE_POLICY
from src.strategy import Params, StrategyMarketData, TradePlan, get_strategy


def _params(strategy, **overrides) -> Params:
    values = {dim.name: 0 for dim in strategy.param_space.dims}
    values.update(
        {
            "weight_close": 1,
            "sell_threshold": 8,
            "per_symbol_cap": 1,
            "total_exposure_cap": 1,
        }
    )
    values.update(overrides)
    return Params(values=values, _engine=strategy.name)


def _market(rows: int = 270, columns: int = 1) -> StrategyMarketData:
    close = np.full((rows, columns), 10.0, dtype=np.float32)
    indicators = np.zeros((rows, columns, 22), dtype=np.float32)
    indicators[:, :, 0] = close
    dates = pd.date_range("2025-01-01", periods=rows).strftime("%Y-%m-%d").tolist()
    return StrategyMarketData(
        indicator_matrix=indicators,
        dates=dates,
        symbols=[f"S{index}" for index in range(columns)],
        prices=close,
        highs=close,
        lows=close,
        tradable=np.ones_like(close, dtype=bool),
        date_ordinals=pd.to_datetime(dates).values.astype("datetime64[D]").astype(int),
    )


def test_three_day_confirmation_is_one_shot_and_rearms_after_invalidation():
    strategy = get_strategy("technical_ensemble")
    market = _market()
    score = np.ones((270, 1), dtype=np.float32)
    score[260, 0] = -1.0

    plan = strategy._plan_from_score(_params(strategy), market, score)

    assert np.flatnonzero(plan.entry_events[:, 0]).tolist() == [253, 263]
    assert plan.execution["model"] == "target_weight"
    assert plan.execution["per_symbol_cap"] == pytest.approx(0.20)
    assert plan.target_weights is None
    assert not plan.entry_events[254:260, 0].any()


def test_training_entry_event_is_not_replayed_into_flat_test_account():
    strategy = get_strategy("technical_ensemble")
    market = _market()
    score = np.ones((270, 1), dtype=np.float32)
    full_plan = strategy._plan_from_score(_params(strategy), market, score)
    test_plan = full_plan.sliced(254, 270)
    test_market = StrategyMarketData(
        indicator_matrix=market.indicator_matrix[254:270],
        dates=market.dates[254:270],
        symbols=market.symbols,
        prices=market.prices[254:270],
        highs=market.highs[254:270],
        lows=market.lows[254:270],
        tradable=market.tradable[254:270],
        date_ordinals=market.date_ordinals[254:270],
    )

    trace = simulate_portfolio(
        test_plan,
        test_market,
        initial_cash=100_000.0,
        lot_size=100,
        commission_rate=0.005,
        min_holding_days=30,
        execution_prices=DEFAULT_FILL_PRICE_POLICY.build(
            test_market.prices, test_market.highs, test_market.lows
        ),
    )

    assert not test_plan.entry_events.any()
    assert trace.total_trades == 0
    assert trace.final_shares[0] == 0


def test_lifetime_eligibility_is_not_reset_by_state_lookback_slice():
    strategy = get_strategy("technical_ensemble")
    market = _market(rows=300)
    market.observation_counts = np.cumsum(market.tradable, axis=0, dtype=np.int32)
    score = np.ones((300, 1), dtype=np.float32)
    full_plan = strategy._plan_from_score(_params(strategy), market, score)

    state_start = 60
    state_market = StrategyMarketData(
        indicator_matrix=market.indicator_matrix[state_start:],
        dates=market.dates[state_start:],
        symbols=market.symbols,
        prices=market.prices[state_start:],
        highs=market.highs[state_start:],
        lows=market.lows[state_start:],
        tradable=market.tradable[state_start:],
        date_ordinals=market.date_ordinals[state_start:],
        observation_counts=market.observation_counts[state_start:],
    )
    state_plan = strategy._plan_from_score(
        _params(strategy), state_market, score[state_start:]
    )

    np.testing.assert_array_equal(
        state_plan.entry_events, full_plan.entry_events[state_start:]
    )
    assert np.flatnonzero(state_plan.entry_events[:, 0]).tolist() == [193]


def test_score_allocation_respects_shared_caps_and_symbol_permutation():
    strategy = get_strategy("technical_ensemble")
    market = _market(columns=5)
    score = np.tile(np.arange(1, 6, dtype=np.float32), (270, 1))
    params = _params(strategy)
    plan = strategy._plan_from_score(params, market, score)
    order = np.array([4, 1, 3, 0, 2])
    permuted_market = StrategyMarketData(
        indicator_matrix=market.indicator_matrix[:, order],
        dates=market.dates,
        symbols=[market.symbols[index] for index in order],
        prices=market.prices[:, order],
        highs=market.highs[:, order],
        lows=market.lows[:, order],
        tradable=market.tradable[:, order],
        date_ordinals=market.date_ordinals,
    )
    permuted = strategy._plan_from_score(params, permuted_market, score[:, order])

    assert plan.target_weights is None
    assert permuted.target_weights is None
    np.testing.assert_allclose(plan.conviction[253, order], permuted.conviction[253])
    assert plan.execution["per_symbol_cap"] == pytest.approx(0.20)
    assert plan.execution["total_exposure_cap"] == pytest.approx(0.80)


def test_unified_diagnostics_count_events_cash_rejections_and_concentration():
    rows = 5
    close = np.full((rows, 1), 10.0, dtype=np.float32)
    buy = np.ones_like(close, dtype=bool)
    plan = TradePlan(
        buy_signals=buy,
        sell_signals=np.zeros_like(buy),
        buy_priority=np.ones_like(close),
        sell_priority=np.full_like(close, -np.inf),
        buy_cash_limit=100_000.0,
        sell_cash_limit=100_000.0,
        warmup_rows=0,
        execution={"model": "cash_cap"},
    )
    market = StrategyMarketData(
        indicator_matrix=np.zeros((rows, 1, 22), dtype=np.float32),
        dates=pd.date_range("2026-01-01", periods=rows).strftime("%Y-%m-%d").tolist(),
        symbols=["S0"],
        prices=close,
        highs=close,
        lows=close,
        tradable=np.ones_like(close, dtype=bool),
    )
    trace = simulate_portfolio(
        plan,
        market,
        initial_cash=100_000.0,
        lot_size=100,
        commission_rate=0.005,
        execution_prices=DEFAULT_FILL_PRICE_POLICY.build(close),
    )

    assert trace.signal_event_count == 1
    assert trace.cash_rejected_order_count > 0
    assert trace.concentration_hhi == pytest.approx(1.0)
    assert trace.selected_basket_hold_return is not None
    assert trace.timing_value_add is not None


def test_fast_and_batch_diagnostics_are_identical():
    rows = 10
    close = np.full((rows, 1), 10.0, dtype=np.float32)
    buy = np.zeros_like(close, dtype=bool)
    buy[0:3, 0] = True
    plan = TradePlan(
        buy_signals=buy,
        sell_signals=np.zeros_like(buy),
        buy_priority=np.where(buy, 1.0, -np.inf),
        sell_priority=np.full_like(close, -np.inf),
        buy_cash_limit=20_000.0,
        sell_cash_limit=20_000.0,
        warmup_rows=0,
        execution={"model": "cash_cap"},
    )
    evaluator = FastEvaluator(
        SimpleNamespace(
            initial_capital=100_000.0,
            lot_sizes={"a_share": 100},
            commission_rate=0.005,
            min_holding_days=0,
            fx_rates={"a_share": 1.0},
        ),
        "a_share",
    )
    inputs = {
        "indicator_matrix": np.zeros((rows, 1, 22), dtype=np.float32),
        "price_matrix": close,
        "cash_baseline": np.full(rows, 100_000.0),
    }
    scalar = evaluator.evaluate(trade_plan=plan, **inputs)
    batched = evaluator.evaluate_batch([plan], **inputs)[0]

    assert (
        scalar.signal_event_count,
        scalar.cash_rejected_order_count,
        scalar.concentration_hhi,
    ) == (
        batched.signal_event_count,
        batched.cash_rejected_order_count,
        batched.concentration_hhi,
    )
