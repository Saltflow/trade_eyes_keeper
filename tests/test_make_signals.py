"""Contract tests for the only strategy decision interface."""

from __future__ import annotations

import numpy as np
import pytest

from src.strategy import Params, StrategyMarketData, TradePlan, get_strategy


def _market_data(periods: int = 100, symbols: int = 2) -> StrategyMarketData:
    rng = np.random.RandomState(42)
    indicators = rng.randn(periods, symbols, 16).astype(np.float32)
    prices = 100 + np.cumsum(rng.randn(periods, symbols), axis=0)
    indicators[:, :, 0] = prices
    return StrategyMarketData(
        indicator_matrix=indicators,
        dates=[f"2026-01-{index + 1:03d}" for index in range(periods)],
        symbols=[f"S{index}" for index in range(symbols)],
        prices=prices.astype(np.float32),
        tradable=np.ones((periods, symbols), dtype=bool),
    )


def _params(strategy) -> Params:
    return Params(
        values={
            dim.name: min(1, max(dim.levels - 1, 0))
            for dim in strategy.param_space.dims
        }
    )


@pytest.mark.parametrize("strategy_id", ["percentile", "simplified"])
def test_registered_strategy_returns_canonical_trade_plan(strategy_id: str):
    strategy = get_strategy(strategy_id)
    market_data = _market_data()

    plan = strategy.make_signals(_params(strategy), market_data)

    assert isinstance(plan, TradePlan)
    assert plan.buy_signals.shape == plan.sell_signals.shape == (100, 2)
    assert plan.buy_priority.shape == plan.sell_priority.shape == (100, 2)
    assert plan.dates == market_data.dates
    assert plan.symbols == market_data.symbols
    assert plan.execution["model"] == "cash_cap"
    assert plan.strategy_metadata["strategy_id"] == strategy_id
    assert not np.any(plan.buy_signals & plan.sell_signals)


def test_make_signals_rejects_raw_indicator_array():
    strategy = get_strategy("percentile")
    market_data = _market_data()

    with pytest.raises(TypeError, match="StrategyMarketData"):
        strategy.make_signals(_params(strategy), market_data.indicator_matrix)
