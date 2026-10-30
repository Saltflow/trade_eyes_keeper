import numpy as np
import pytest

from src.backtest import engine
from src.search.config import ExecutionConfig
from src.strategy import StrategyMarketData, TradePlan


@pytest.mark.parametrize("factory", [engine.FastEvaluator, engine.Backtester])
def test_execution_entry_points_reject_missing_numba(monkeypatch, factory):
    monkeypatch.setattr(engine, "HAS_NUMBA", False)

    with pytest.raises(RuntimeError, match="requires numba"):
        factory(ExecutionConfig(initial_capital=100_000), "a_share")


def test_direct_simulation_cannot_return_zero_nav_for_missing_backend(monkeypatch):
    prices = np.array([[10.0], [11.0], [12.0]])
    buys = np.array([[True], [False], [False]])
    plan = TradePlan(
        buy_signals=buys,
        sell_signals=np.zeros_like(buys),
        buy_priority=buys.astype(float),
        sell_priority=np.zeros_like(prices),
        buy_cash_limit=5000,
        sell_cash_limit=5000,
        warmup_rows=0,
        execution={"model": "cash_cap"},
    )
    market = StrategyMarketData(
        indicator_matrix=np.zeros((3, 1, 1)),
        prices=prices,
        highs=prices,
        lows=prices,
    )
    monkeypatch.setattr(engine, "HAS_NUMBA", False)

    with pytest.raises(RuntimeError, match="requires numba"):
        engine.simulate_portfolio(
            plan, market, initial_cash=100_000, lot_size=100, commission_rate=0.005
        )
