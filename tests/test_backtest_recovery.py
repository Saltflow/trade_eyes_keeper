"""Regression coverage for restored optimizer and interactive backtest paths."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import main
from src.backtest.engine import evaluate_all_groups
from src.search.config import get_execution_config
from src.strategy import Params
from src.strategy import get_strategy
from src.search.artifacts import (
    OptimizerGroupSummary,
    load_latest_strategy_run,
    publish_complete_run,
)
from src.interactive.commands import handlers
from src.interactive.command_parser import ErrorCommand, parse_command


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


def test_optimizer_runs_each_market_group(monkeypatch):
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

    completed = main.run_optimization(
        {
            "stocks": ["600000", "00700", "AAPL", "600001"],
            "skip_search": ["600001"],
            "optimizer": {"engine": "percentile"},
        },
        target_groups=main.OPTIMIZER_GROUPS,
    )

    assert completed == {"a_share": 1, "hk": 1, "us": 1}
    assert calls == [
        ("percentile", "a_share", ["600000"]),
        ("percentile", "hk", ["00700"]),
        ("percentile", "us", ["AAPL"]),
    ]
    assert len(prune_calls) == 2
    assert prune_calls[0]["keep_completed"] == 3
    assert len(prune_calls[0]["protected_run_ids"]) == 1
    assert prune_calls[1] == {"keep_completed": 3}


def test_optimizer_excludes_short_history_before_date_alignment(monkeypatch):
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

    completed = main.run_optimization(
        {
            "stocks": ["600000", "600001"],
            "optimizer": {"engine": "percentile"},
        }
    )

    assert completed == {"a_share": 1}
    assert calls == [("a_share", ["600000"])]


def test_optimize_cli_runs_configured_strategy_for_all_markets(monkeypatch):
    calls = []

    monkeypatch.setattr(main, "load_config", lambda: {})
    monkeypatch.setattr(
        main, "setup_logging", lambda _config: SimpleNamespace(info=lambda *_args: None)
    )
    monkeypatch.setattr(
        main,
        "run_optimization",
        lambda _config, target_groups=(): calls.append(target_groups)
        or {"a_share": 1},
    )

    main.main(["--optimize"])
    assert calls == [("a_share", "hk", "us")]
    with pytest.raises(SystemExit):
        main.main(["--optimize-v2"])
    with pytest.raises(SystemExit):
        main.main(["--optimize", "--all-markets"])


def test_ref_portfolio_rebalance_accepts_configured_commission(tmp_path):
    from src.core.ref_portfolio import RefPortfolio, RefPortfolioManager

    manager = RefPortfolioManager(file_path=tmp_path / "ref_portfolio.yaml")
    alert = SimpleNamespace(
        stock_code="600000",
        rule_id="buy_signal",
        rule_label="buy",
        type="strategy_buy",
    )
    _, trades = manager.rebalance(
        RefPortfolio(inception_date="2026-07-01"),
        [alert],
        {"600000": 10.0},
        "2026-07-15",
        monthly_buy_limit=50_000,
        commission_rate=0.001,
    )

    assert len(trades) == 1
    assert trades[0].commission > 0


def test_evaluation_honors_requested_backtest_dates():
    history = _price_history()
    strategy = get_strategy("percentile")
    params = Params(
        values={
            dim.name: max((dim.levels - 1) // 2, 0)
            for dim in strategy.param_space.dims
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
        report.total_return
        - report.benchmark_returns[report.primary_benchmark],
        2,
    )
    assert report.primary_benchmark == max(
        ("risk_free", "510300", "universe_equal_weight"),
        key=lambda code: report.benchmark_returns[code],
    )


def test_optimizer_command_rejects_strategy_and_preset_arguments():
    command = parse_command("/optimize builder fast")

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


def test_latest_complete_manifest_selects_newest_timestamp(tmp_path):
    groups = ("a_share", "hk", "us")

    def make_run(run_id, timestamp, engine):
        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        summaries = {}
        for group in groups:
            artifact = run_dir / f"{group}_best_params.yaml"
            artifact.write_text(
                "\n".join(
                    [
                        f"timestamp: {timestamp}",
                        f"group: {group}",
                        f"engine: {engine}",
                        "params:",
                        f"  _engine: {engine}",
                        "  signal: 1",
                        "wf_score: 1.0",
                        "search:",
                        "  selection_score: -0.5",
                        "  ranking_diagnostics:",
                        "    weighted_strategy_return: 2.5",
                        "    positive_return_windows: 9",
                        "    ranking_window_count: 13",
                        "sensitivity:",
                        "  base_score: 1.0",
                        "  worst_score: -1.5",
                        "  drop: 2.5",
                        "  selection_score: -0.5",
                    ]
                ),
                encoding="utf-8",
            )
            summaries[group] = OptimizerGroupSummary(
                group=group,
                candidate_count=1,
                wf_score=1.0,
                params={"signal": 1},
                status="completed",
                artifact=artifact.name,
            )
        assert publish_complete_run(
            run_id, engine, timestamp, summaries, required_groups=groups, root=tmp_path
        )

    make_run("old", "2026-07-26T01:00:00", "percentile")
    make_run("new", "2026-07-26T02:00:00", "builder")

    active = load_latest_strategy_run(groups=groups, root=tmp_path)
    assert active is not None
    assert active.strategy_name == "builder"
    assert active.run_id == "new"
    assert active.params_by_group["hk"].values == {"signal": 1}
    assert active.selection_by_group["a_share"] == {
        "wf_score": 1.0,
        "ranking_diagnostics": {
            "weighted_strategy_return": 2.5,
            "positive_return_windows": 9,
            "ranking_window_count": 13,
        },
        "sensitivity": {
            "base_score": 1.0,
            "worst_score": -1.5,
            "drop": 2.5,
            "selection_score": -0.5,
        },
        "selection_score": -0.5,
    }


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


def test_partial_a_share_publish_retains_previous_hk_and_us_artifacts(tmp_path):
    groups = ("a_share", "hk", "us")

    def write_artifact(run_id, group, signal):
        path = tmp_path / "runs" / run_id / f"{group}_best_params.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    "engine: percentile",
                    "params:",
                    "  _engine: percentile",
                    f"  signal: {signal}",
                ]
            ),
            encoding="utf-8",
        )
        return path

    old_summaries = {}
    for group in groups:
        artifact = write_artifact("old", group, 1)
        old_summaries[group] = OptimizerGroupSummary(
            group=group, status="completed", artifact=artifact.name
        )
    assert publish_complete_run(
        "old",
        "percentile",
        "2026-07-26T01:00:00",
        old_summaries,
        required_groups=groups,
        root=tmp_path,
    )

    a_share = write_artifact("new", "a_share", 2)
    assert publish_complete_run(
        "new",
        "percentile",
        "2026-07-26T02:00:00",
        {"a_share": OptimizerGroupSummary(
            group="a_share", status="completed", artifact=a_share.name
        )},
        required_groups=("a_share",),
        all_groups=groups,
        root=tmp_path,
    )

    active = load_latest_strategy_run(groups=groups, root=tmp_path)
    assert active is not None
    assert active.run_id == "new"
    assert active.params_by_group["a_share"].values == {"signal": 2}
    assert active.params_by_group["hk"].values == {"signal": 1}
    assert active.params_by_group["us"].values == {"signal": 1}


def test_interactive_backtest_uses_unified_evaluator(monkeypatch):
    history = _price_history()

    class FakeDataSource:
        def __init__(self, config):
            self.config = config

        def fetch_stock_data(self, code, days):
            return history.copy()

    monkeypatch.setattr("src.data.data_source.DataSource", FakeDataSource)
    monkeypatch.setattr(
        handlers,
        "_load_config",
        lambda: {"optimizer": {"engine": "percentile"}},
    )

    start = history["date"].iloc[300].strftime("%Y-%m-%d")
    end = history["date"].iloc[420].strftime("%Y-%m-%d")
    result = handlers.handle_backtest("600000", start, end)

    assert "回测报告" in result
    assert "策略收益" in result
