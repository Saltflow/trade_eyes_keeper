"""Contracts for the fixed MA60 +/-5% timing strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import IDX_CLOSE, IDX_MA60, INDICATOR_NAMES, simulate_portfolio
from src.backtest.execution import DEFAULT_FILL_PRICE_POLICY
from src.strategy import Params, StrategyMarketData, get_strategy


def _market(rows: int = 110, columns: int = 4) -> StrategyMarketData:
    matrix = np.full(
        (rows, columns, len(INDICATOR_NAMES)), np.nan, dtype=np.float32
    )
    matrix[:, :, IDX_CLOSE] = 100.0
    matrix[:, :, IDX_MA60] = 100.0
    dates = pd.bdate_range("2025-01-02", periods=rows)
    prices = matrix[:, :, IDX_CLOSE].copy()
    return StrategyMarketData(
        indicator_matrix=matrix,
        dates=dates.strftime("%Y-%m-%d").tolist(),
        symbols=[f"S{index}" for index in range(columns)],
        prices=prices,
        highs=prices.copy(),
        lows=prices.copy(),
        tradable=np.ones_like(prices, dtype=bool),
        date_ordinals=dates.values.astype("datetime64[D]").astype(np.int64),
    )


def _params() -> Params:
    return Params(values={}, _engine="ma60_band")


def test_fixed_boundaries_warmup_and_missing_values():
    strategy = get_strategy("ma60_band")
    market = _market()
    matrix = market.indicator_matrix
    matrix[58, 0, IDX_CLOSE] = 94.99
    matrix[59, 0, IDX_CLOSE] = 94.99
    matrix[60, 0, IDX_CLOSE] = 95.0
    matrix[61, 0, IDX_CLOSE] = 105.0
    matrix[62, 0, IDX_CLOSE] = 105.01
    matrix[63, 0, IDX_CLOSE] = np.nan
    market.prices = matrix[:, :, IDX_CLOSE].copy()

    plan = strategy.make_signals(_params(), market)

    assert not plan.entry_events[58, 0]
    assert plan.entry_events[59, 0]
    assert not plan.entry_events[60, 0]
    assert not plan.exit_events[61, 0]
    assert plan.exit_events[62, 0]
    assert not plan.buy_signals[63, 0]
    assert not plan.sell_signals[63, 0]


def test_fixed_target_weights_and_empty_search_schema():
    strategy = get_strategy("ma60_band")
    market = _market()
    plan = strategy.make_signals(_params(), market)

    assert strategy.parameter_schema.names == ()
    assert strategy.sample_params().values == {}
    assert plan.execution["model"] == "target_weight"
    assert plan.execution["per_symbol_cap"] == pytest.approx(0.25)
    assert plan.execution["total_exposure_cap"] == pytest.approx(1.0)
    assert np.allclose(plan.target_weights, 0.25)
    assert plan.strategy_metadata["strategy_id"] == "ma60_band"


def test_level_signal_executes_once_then_exits_after_shared_holding_lock():
    strategy = get_strategy("ma60_band")
    market = _market(columns=1)
    market.indicator_matrix[59:75, 0, IDX_CLOSE] = 90.0
    market.indicator_matrix[75:, 0, IDX_CLOSE] = 110.0
    market.prices = market.indicator_matrix[:, :, IDX_CLOSE].copy()
    market.highs = market.prices.copy()
    market.lows = market.prices.copy()
    plan = strategy.make_signals(_params(), market)

    trace = simulate_portfolio(
        plan,
        market,
        initial_cash=100_000.0,
        lot_size=100,
        commission_rate=0.005,
        min_holding_days=30,
        execution_prices=DEFAULT_FILL_PRICE_POLICY.build(
            market.prices, market.highs, market.lows
        ),
    )

    assert plan.entry_events[59:75, 0].all()
    assert plan.exit_events[75:, 0].all()
    assert trace.total_trades == 2
    assert trace.final_shares[0] == 0


def test_symbol_permutation_only_permutes_decisions():
    strategy = get_strategy("ma60_band")
    market = _market()
    market.indicator_matrix[59:, 0, IDX_CLOSE] = 90.0
    market.indicator_matrix[70:, 1, IDX_CLOSE] = 89.0
    order = np.array([2, 0, 3, 1])
    permuted = StrategyMarketData(
        indicator_matrix=market.indicator_matrix[:, order, :],
        dates=market.dates,
        symbols=[market.symbols[index] for index in order],
        prices=market.prices[:, order],
        highs=market.highs[:, order],
        lows=market.lows[:, order],
        tradable=market.tradable[:, order],
        date_ordinals=market.date_ordinals,
    )

    original_plan = strategy.make_signals(_params(), market)
    permuted_plan = strategy.make_signals(_params(), permuted)

    assert np.array_equal(
        original_plan.entry_events[:, order], permuted_plan.entry_events
    )
    assert np.array_equal(
        original_plan.exit_events[:, order], permuted_plan.exit_events
    )
    assert np.array_equal(
        original_plan.target_weights[:, order], permuted_plan.target_weights
    )
