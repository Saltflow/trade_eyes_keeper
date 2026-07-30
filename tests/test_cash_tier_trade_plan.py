"""Regression coverage for the cash-tier execution contract."""

from __future__ import annotations

import numpy as np
import pandas as pd
import yaml

from src.analysis.backtester import _build_indicator_matrix, simulate_portfolio
from src.analysis.search_interface import StrategyMarketData, TradePlan
from src.analysis.strategies import get_strategy
from src.analysis.strategy_artifacts import load_latest_strategy_run


def _trace(
    buy: np.ndarray,
    sell: np.ndarray,
    price: np.ndarray,
    *,
    buy_limit: float,
    sell_limit: float,
    buy_priority: np.ndarray | None = None,
    min_holding_days: int = 30,
    initial_cash: float = 100_000.0,
):
    dates = pd.bdate_range("2026-01-02", periods=len(price)).strftime(
        "%Y-%m-%d"
    ).tolist()
    symbols = ["ZZZ", "AAA"][: price.shape[1]]
    plan = TradePlan(
        buy_signals=buy.astype(bool),
        sell_signals=sell.astype(bool),
        buy_priority=(
            buy_priority
            if buy_priority is not None
            else np.where(buy, 1.0, -np.inf)
        ),
        sell_priority=np.where(sell, 1.0, -np.inf),
        buy_cash_limit=buy_limit,
        sell_cash_limit=sell_limit,
        warmup_rows=0,
        dates=dates,
        symbols=symbols,
    )
    market_data = StrategyMarketData(
        indicator_matrix=np.empty((*price.shape, 0), dtype=np.float32),
        dates=dates,
        symbols=symbols,
        prices=price.astype(np.float32),
        tradable=np.isfinite(price) & (price > 0),
    )
    return simulate_portfolio(
        plan,
        market_data,
        initial_cash=initial_cash,
        lot_size=100,
        commission_rate=0.0,
        min_holding_days=min_holding_days,
    )


def test_cash_cap_obeys_priority_not_input_stock_order():
    buy = np.array([[True, True]])
    sell = np.zeros_like(buy)
    priority = np.array([[0.1, 0.9]], dtype=np.float32)
    trace = _trace(
        buy,
        sell,
        np.array([[10.0, 10.0]]),
        buy_limit=20_000.0,
        sell_limit=20_000.0,
        buy_priority=priority,
        initial_cash=20_000.0,
    )

    assert trace.final_shares.tolist() == [0.0, 2000.0]
    assert trace.final_cash == 0.0


def test_buy_and_sell_use_independent_cash_caps_and_holding_period():
    price = np.full((32, 1), 10.0)
    buy = np.zeros((32, 1), dtype=bool)
    sell = np.zeros((32, 1), dtype=bool)
    buy[0, 0] = True
    sell[31, 0] = True
    trace = _trace(
        buy,
        sell,
        price,
        buy_limit=20_000.0,
        sell_limit=5_000.0,
        min_holding_days=30,
    )

    assert trace.total_trades == 2
    assert trace.final_shares.tolist() == [1500.0]
    assert trace.final_cash == 85_000.0


def test_registered_strategies_have_only_shared_cash_tier_execution_dims():
    for name in ("percentile", "builder", "simplified"):
        strategy = get_strategy(name)
        names = [dim.name for dim in strategy.param_space.dims]
        assert "buy_cash_tier" in names
        assert "sell_cash_tier" in names
        assert not any("frac" in field or "position" in field for field in names)


def test_short_history_symbol_does_not_truncate_daily_union_calendar():
    long_dates = pd.date_range("2025-01-01", periods=300, freq="B")
    short_dates = long_dates[-80:]
    computed = {
        "LONG": pd.DataFrame({"date": long_dates, "close": np.arange(300) + 10}),
        "NEW": pd.DataFrame({"date": short_dates, "close": np.arange(80) + 20}),
    }

    indicators, prices, dates, tradable = _build_indicator_matrix(
        computed, ["LONG", "NEW"]
    )

    assert indicators.shape[:2] == (300, 2)
    assert len(dates) == 300
    assert not tradable[:220, 1].any()
    assert tradable[-80:, 1].all()
    assert np.isnan(prices[:220, 1]).all()


def test_legacy_artifact_maps_percentage_to_immutable_cash_snapshot(tmp_path):
    timestamp = "2026-07-27T12:00:00"
    for group in ("a_share", "hk", "us"):
        (tmp_path / f"{group}_best_params.yaml").write_text(
            yaml.safe_dump(
                {
                    "timestamp": timestamp,
                    "engine": "percentile",
                    "params": {"_engine": "percentile", "position_frac": 2},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    active = load_latest_strategy_run(root=tmp_path)

    assert active is not None
    snapshot = active.params_by_group["a_share"].execution_snapshot
    assert snapshot["model"] == "cash_cap"
    assert snapshot["migration"] == "legacy_execution_mapped"
    assert snapshot["buy_cash_limit"] == snapshot["sell_cash_limit"]
