"""Regression coverage for retained explicit and interactive backtests."""


import numpy as np
import pandas as pd

from src.backtest.engine import evaluate_all_groups
from src.interactive.commands import handlers
from src.search.config import get_execution_config
from src.strategy import Params, get_strategy


def _price_history(periods: int = 2_000) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=periods, freq="B")
    close = 20 + np.linspace(0, 12, periods) + np.sin(np.arange(periods) / 9)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
            "volume": 100_000 + (np.arange(periods) % 11) * 1_000,
        }
    )


def test_evaluation_honors_requested_backtest_dates():
    history = _price_history()
    strategy = get_strategy("percentile")
    params = Params(
        values={
            dim.name: max((dim.levels - 1) // 2, 0) for dim in strategy.param_space.dims
        },
        _engine=strategy.name,
    )
    start = history["date"].iloc[300].strftime("%Y-%m-%d")
    end = history["date"].iloc[420].strftime("%Y-%m-%d")

    reports = evaluate_all_groups(
        {"600000": history},
        ["600000"],
        strategy,
        params,
        get_execution_config(),
        benchmark_data={
            "510880": history.copy(),
            "510300": history.copy(),
        },
        target_groups=["a_share"],
        start_date=start,
        end_date=end,
    )

    report = reports["a_share"]
    assert report.nav_dates[0] == start
    assert report.nav_dates[-1] == end
    assert len(report.nav_dates) == len(report.nav_series) == 121
    assert set(report.benchmark_returns) == {
        "510880",
        "510300",
        "risk_free",
        "universe_equal_weight",
    }
    assert set(report.benchmark_win_rates) == {
        "510880",
        "510300",
        "risk_free",
        "universe_equal_weight",
    }
    assert report.excess_return == round(
        report.total_return - report.benchmark_returns[report.primary_benchmark],
        2,
    )
    assert report.primary_benchmark == max(
        ("risk_free", "510300", "universe_equal_weight"),
        key=lambda code: report.benchmark_returns[code],
    )


def test_interactive_backtest_uses_unified_evaluator(monkeypatch):
    monkeypatch.setattr(
        handlers,
        "_load_config",
        lambda: {"optimizer": {"markets": {}}},
    )
    # This case is about an unactivated market, so it must not depend on
    # whatever optimizer pointer happens to exist on the machine.
    monkeypatch.setattr(
        "src.search.artifacts.load_latest_strategy_run",
        lambda *args, **kwargs: None,
    )
    result = handlers.handle_backtest("600000", "2024-01-01", "2024-04-30")
    assert result.startswith("❌")
    assert "没有可用的历史策略快照" in result
