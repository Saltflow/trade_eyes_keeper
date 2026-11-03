"""Regression coverage for restored optimizer and interactive backtest paths."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import main
from src.backtest.engine import evaluate_all_groups
from src.interactive.command_parser import ErrorCommand, parse_command
from src.interactive.commands import handlers
from src.search.artifacts import OptimizerGroupSummary
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


def test_optimizer_runs_each_market_group(monkeypatch, tmp_path):
    config = main.load_config()
    config["stocks"] = ["600000", "00700", "AAPL", "600001"]
    config["skip_search"] = ["600001"]
    config.pop("point_in_time_data", None)
    monkeypatch.chdir(tmp_path)
    history = _price_history()
    calls = []
    prune_calls = []

    class FakeDataSource:
        def __init__(self, config):
            self.config = config

        def fetch_stock_data(self, code, days):
            assert days >= 1_900
            return history.copy()

    def fake_run_optimizer(
        strategy, stocks_data, stock_codes, group, _constraints, output_dir, **_kwargs
    ):
        calls.append((strategy.name, group, sorted(stock_codes)))
        return [
            SimpleNamespace(
                parameters={"signal": 1},
                objective_score=1.25,
                ranking_stats=[],
                validation_stats=[],
                purged_window_count=0,
                ranking_metrics={},
                sensitivity={},
            )
        ], _constraints

    monkeypatch.setattr("src.data.data_source.DataSource", FakeDataSource)
    monkeypatch.setattr(main, "run_optimizer", fake_run_optimizer)
    monkeypatch.setattr(main, "publish_complete_run", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        main,
        "prune_optimizer_runs",
        lambda **kwargs: prune_calls.append(kwargs),
    )
    monkeypatch.setattr(main, "_notify_optimizer_run", lambda *_args, **_kwargs: None)

    completed = main.run_optimization(config, target_groups=main.OPTIMIZER_GROUPS)

    assert completed == {"a_share": 1, "hk": 1, "us": 1}
    assert calls == [
        ("technical_ensemble", "a_share", ["600000"]),
        ("regime_pullback", "hk", ["00700"]),
        ("percentile", "us", ["AAPL"]),
    ]
    assert len(prune_calls) == 6
    assert all(call["keep_completed"] == 3 for call in prune_calls)


def test_optimizer_excludes_short_history_before_date_alignment(monkeypatch, tmp_path):
    config = main.load_config()
    config["stocks"] = ["600000", "600001"]
    config["skip_search"] = []
    config.pop("point_in_time_data", None)
    monkeypatch.chdir(tmp_path)
    history = _price_history()
    short_history = _price_history(120)
    calls = []

    class FakeDataSource:
        def __init__(self, config):
            self.config = config

        def fetch_stock_data(self, code, days):
            return short_history.copy() if code == "600001" else history.copy()

    def fake_run_optimizer(
        strategy, stocks_data, stock_codes, group, _constraints, output_dir, **_kwargs
    ):
        calls.append((group, sorted(stock_codes)))
        return [
            SimpleNamespace(
                parameters={"signal": 1},
                objective_score=1.25,
                ranking_stats=[],
                validation_stats=[],
                purged_window_count=0,
                ranking_metrics={},
                sensitivity={},
            )
        ], _constraints

    monkeypatch.setattr("src.data.data_source.DataSource", FakeDataSource)
    monkeypatch.setattr(main, "run_optimizer", fake_run_optimizer)
    monkeypatch.setattr(main, "publish_complete_run", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "prune_optimizer_runs", lambda **_kwargs: None)
    monkeypatch.setattr(main, "_notify_optimizer_run", lambda *_args, **_kwargs: None)

    completed = main.run_optimization(config, target_groups=("a_share",))

    assert completed == {"a_share": 1}
    assert calls == [("a_share", ["600000"])]


def test_optimize_cli_runs_configured_strategy_for_all_markets(monkeypatch):
    calls = []

    monkeypatch.setattr(main, "load_config", dict)
    monkeypatch.setattr(
        main, "setup_logging", lambda _config: SimpleNamespace(info=lambda *_args: None)
    )
    monkeypatch.setattr(
        main,
        "run_optimization",
        lambda _config, target_groups=(): calls.append(target_groups) or {"a_share": 1},
    )

    main.main(["--optimize"])
    assert calls == [("a_share", "hk", "us")]
    with pytest.raises(SystemExit):
        main.main(["--optimize-v2"])
    with pytest.raises(SystemExit):
        main.main(["--optimize", "--all-markets"])


def test_ref_portfolio_trade_plan_accepts_configured_commission(tmp_path):
    from src.core.ref_portfolio import RefPortfolio, RefPortfolioManager
    from src.strategy import StrategyMarketData, TradePlan

    manager = RefPortfolioManager(file_path=tmp_path / "ref_portfolio.yaml")
    plan = TradePlan(
        buy_signals=np.array([[True]]),
        sell_signals=np.array([[False]]),
        buy_priority=np.array([[1.0]]),
        sell_priority=np.array([[0.0]]),
        buy_cash_limit=5000,
        sell_cash_limit=5000,
        dates=["2026-07-15"],
        symbols=["600000"],
        warmup_rows=0,
    )
    portfolio = RefPortfolio(
        inception_date="2026-07-01",
        market_group="a_share",
        strategy_run_id="run-1",
        strategy_id="percentile",
        params_hash="params",
        execution_hash="execution",
    )
    _, trades = manager.rebalance_plan(
        portfolio,
        plan,
        StrategyMarketData(
            indicator_matrix=np.zeros((1, 1, 1)), prices=np.array([[10.0]])
        ),
        "2026-07-15",
        run_id="run-1",
        strategy_id="percentile",
        commission_rate=0.001,
    )

    assert len(trades) == 1
    assert trades[0].commission == pytest.approx(trades[0].cost * 0.001)


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


def test_optimizer_command_rejects_strategy_and_preset_arguments():
    command = parse_command("/optimize percentile fast")

    assert isinstance(command, ErrorCommand)
    assert "不接受参数" in command.message


def test_configured_optimizer_groups_only_include_eligible_markets(monkeypatch):
    monkeypatch.setattr(main, "get_skip_search", lambda config: {"VOO"})
    config = {
        "stocks": [
            {"code": "510880"},
            {"code": "00700"},
            {"code": "VOO"},
        ]
    }

    assert main._configured_optimizer_groups(config) == ("a_share", "hk")


def test_optimizer_notification_uses_one_three_market_payload(monkeypatch):
    sent = []

    class FakeNotifierManager:
        def __init__(self, config):
            assert config == {"notification": {}}

        def send_optimizer_notification(self, report, group_name=""):
            sent.append((report, group_name))

    monkeypatch.setattr(main, "NotifierManager", FakeNotifierManager)
    report = main.OptimizerRunSummary(
        "percentile",
        "Percentile",
        "2026-07-26T03:00:00",
        3.0,
        {
            group: OptimizerGroupSummary(group=group, status="no_data")
            for group in ("a_share", "hk", "us")
        },
    )

    main._notify_optimizer_run({"notification": {}}, report)

    assert len(sent) == 1
    assert sent[0][0].groups.keys() == {"a_share", "hk", "us"}
    assert sent[0][1] == "Percentile"


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
    assert "没有已激活的回测策略" in result
