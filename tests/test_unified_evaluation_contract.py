"""Authoritative contracts for unified strategy evaluation."""

from types import SimpleNamespace

import random

import numpy as np
import pandas as pd

from src.backtest.engine import (
    Backtester,
    INDICATOR_NAMES,
    WalkForwardManager,
    _weekly_nav_ohlc,
    simulate_portfolio,
)
from src.search.config import ExecutionConfig, StrategyConstraints, WalkForwardConfig
from src.search.workflow import _partition_window_indexes
from src.strategy import StrategyMarketData, TradePlan
from src.strategy import get_strategy


def test_every_registered_strategy_returns_trade_plan():
    matrix = np.ones((320, 2, len(INDICATOR_NAMES)), dtype=np.float32)
    data = StrategyMarketData(
        indicator_matrix=matrix,
        dates=pd.bdate_range("2025-01-01", periods=320).strftime("%Y-%m-%d").tolist(),
        symbols=["000001", "000002"],
        prices=np.full((320, 2), 10.0, dtype=np.float32),
        tradable=np.ones((320, 2), dtype=bool),
    )
    for strategy_id in (
        "percentile",
        "builder",
        "simplified",
        "regime_pullback",
    ):
        strategy = get_strategy(strategy_id)
        params = strategy.sample_params(random.Random(7))
        plan = strategy.make_signals(params, data)
        assert isinstance(plan, TradePlan)
        assert plan.buy_signals.shape == (320, 2)
        assert plan.sell_signals.shape == (320, 2)
        assert plan.dates == data.dates
        assert plan.symbols == data.symbols
        assert plan.strategy_metadata["strategy_id"] == strategy_id
        assert not np.any(plan.buy_signals & plan.sell_signals)


def test_calendar_walk_forward_is_exactly_14_windows_and_60_months():
    manager = WalkForwardManager.__new__(WalkForwardManager)
    manager.dates = pd.bdate_range("2019-01-01", "2026-01-30")
    manager.T = len(manager.dates)
    manager.wf_config = WalkForwardConfig(
        {
            "train_months": 12,
            "test_months": 9,
            "step_months": 3,
            "num_windows": 14,
            "validation_windows": 1,
            "purge_overlapping_windows": False,
            "data_years": 5,
        }
    )
    windows = manager.iter_windows()
    assert len(windows) == 14
    assert pd.Timestamp(windows[0].train_start_date) + pd.DateOffset(months=12) == pd.Timestamp(windows[0].test_start_date)
    assert pd.Timestamp(windows[0].test_start_date) + pd.DateOffset(months=9) - pd.Timedelta(days=1) == pd.Timestamp(windows[0].test_end_date)
    assert pd.Timestamp(windows[-1].test_end_date) - pd.Timestamp(windows[0].train_start_date) < pd.Timedelta(days=1830)

    constraints = StrategyConstraints(
        {
            "walk_forward": {
                "num_windows": 14,
                "validation_windows": 1,
                "purge_overlapping_windows": False,
            }
        }
    )
    ranking, purged, holdout = _partition_window_indexes(windows, constraints)
    assert ranking == list(range(13))
    assert purged == []
    assert holdout == [13]


def test_weekly_ohlc_keeps_short_and_single_day_weeks():
    dates = ["2026-01-09", "2026-01-12", "2026-01-13", "2026-01-19"]
    bars = _weekly_nav_ohlc(dates, [100.0, 101.0, 99.0, 102.0])
    assert bars["open"] == [100.0, 101.0, 102.0]
    assert bars["high"] == [100.0, 101.0, 102.0]
    assert bars["low"] == [100.0, 99.0, 102.0]
    assert bars["close"] == [100.0, 99.0, 102.0]


def test_quarterly_holdings_are_natural_quarter_end_snapshots():
    dates = pd.bdate_range("2025-01-02", "2025-07-04")
    price = np.full((len(dates), 1), 10.0)
    buy = np.zeros_like(price)
    sell = np.zeros_like(price)
    buy[0, 0] = 1.0
    date_values = dates.strftime("%Y-%m-%d").tolist()
    plan = TradePlan(
        buy_signals=buy.astype(bool),
        sell_signals=sell.astype(bool),
        buy_priority=np.where(buy > 0, buy, -np.inf),
        sell_priority=np.where(sell > 0, sell, -np.inf),
        buy_cash_limit=10_000.0,
        sell_cash_limit=10_000.0,
        warmup_rows=0,
        dates=date_values,
        symbols=["000001"],
    )
    trace = simulate_portfolio(
        plan,
        StrategyMarketData(
            indicator_matrix=np.empty((*price.shape, 0), dtype=np.float32),
            dates=date_values,
            symbols=["000001"],
            prices=price,
            tradable=np.ones_like(price, dtype=bool),
        ),
        100000.0,
        100,
        0.005,
        min_holding_days=0,
    )
    assert [item["quarter"] for item in trace.quarterly_holdings] == [
        "2025Q1",
        "2025Q2",
    ]
    assert trace.quarterly_holdings[0]["date"] == "2025-03-31"
    assert trace.quarterly_holdings[1]["date"] == "2025-06-30"


def test_backtester_returns_complete_evaluation_report():
    dates = pd.bdate_range("2025-01-02", periods=70).strftime("%Y-%m-%d").tolist()
    prices = np.linspace(10.0, 14.0, len(dates), dtype=np.float32).reshape(-1, 1)
    buy = np.zeros_like(prices, dtype=bool)
    sell = np.zeros_like(prices, dtype=bool)
    buy[0, 0] = True
    plan = TradePlan(
        buy_signals=buy,
        sell_signals=sell,
        buy_priority=np.where(buy, 1.0, -np.inf),
        sell_priority=np.where(sell, 1.0, -np.inf),
        buy_cash_limit=20_000.0,
        sell_cash_limit=20_000.0,
        warmup_rows=0,
        dates=dates,
        symbols=["000001"],
    )
    market_data = StrategyMarketData(
        indicator_matrix=np.zeros((len(dates), 1, 16), dtype=np.float32),
        dates=dates,
        symbols=["000001"],
        prices=prices,
        tradable=np.ones_like(prices, dtype=bool),
    )
    benchmark = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "close": np.linspace(100.0, 105.0, len(dates)),
        }
    )
    execution = ExecutionConfig(
        initial_capital=100_000.0,
        commission_rate=0.001,
        min_holding_days=0,
        lot_sizes={"a_share": 100},
    )

    report = Backtester(execution, "a_share").run(
        plan,
        market_data,
        benchmark_data={"510880": benchmark},
        benchmark_codes=["risk_free", "510880"],
        primary_benchmark="510880",
        risk_free_rate=0.02,
        strategy_id="test_strategy",
    )

    assert report.initial_asset == 100_000.0
    assert report.final_asset == report.final_cash + report.final_holdings_value
    assert report.trade_count == 1
    assert report.final_holdings[0]["code"] == "000001"
    assert report.final_holdings[0]["shares"] > 0
    assert report.primary_benchmark == "510880"
    assert set(report.benchmark_details) == {
        "risk_free",
        "510880",
        "universe_equal_weight",
    }
    assert report.benchmark_details["510880"]["comparison_days"] > 0
    assert report.weekly_nav_ohlc["close"]
    assert report.nav_dates == dates
