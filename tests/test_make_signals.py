"""Contract tests for the only strategy decision interface."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.search_interface import Params, StrategyMarketData, TradePlan
from src.analysis.strategies import get_strategy


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


@pytest.mark.parametrize("strategy_id", ["builder", "percentile", "simplified"])
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


def test_today_scan_reads_last_row_of_same_trade_plan(monkeypatch):
    strategy = get_strategy("percentile")
    periods = strategy.warmup_rows + 1
    market_data = _market_data(periods=periods, symbols=1)
    buy = np.zeros((periods, 1), dtype=bool)
    sell = np.zeros_like(buy)
    buy[-1, 0] = True
    expected = TradePlan(
        buy_signals=buy,
        sell_signals=sell,
        buy_priority=np.where(buy, 7.0, -np.inf),
        sell_priority=np.where(sell, 7.0, -np.inf),
        buy_cash_limit=20_000.0,
        sell_cash_limit=10_000.0,
        warmup_rows=strategy.warmup_rows,
        dates=market_data.dates,
        symbols=market_data.symbols,
    )
    monkeypatch.setattr(
        "src.data.technical_indicators.compute_all",
        lambda stocks: {"scan": pd.DataFrame()},
    )
    monkeypatch.setattr(
        "src.analysis.backtester._build_indicator_matrix",
        lambda computed, codes: (
            market_data.indicator_matrix,
            market_data.prices,
            market_data.dates,
            market_data.tradable,
        ),
    )
    monkeypatch.setattr(strategy, "make_signals", lambda params, data: expected)

    results = strategy.scan_today(
        _params(strategy), {}, pd.DataFrame({"close": np.arange(periods)})
    )

    assert [item["side"] for item in results] == ["buy"]
    assert results[0]["priority"] == 7.0
    assert "20000" in results[0]["detail"]
