"""Data preparation failures stay visible in optimizer notification receipts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

import main
from src.data.backtest_data import DataReadinessIssue
from src.notification.report_builders import (
    build_optimizer_summary,
    optimizer_notification_title,
)
from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary
from src.search.config import get_market_optimizer_config


def _report(groups):
    return OptimizerRunSummary(
        "percentile", "test", "2026-09-23T02:00:00", 10, groups
    )


def test_no_symbols_exposes_refresh_failure_and_benchmark_without_duplicates():
    report = _report({
        "hk": OptimizerGroupSummary(
            group="hk",
            status="no_symbols",
            ranking_diagnostics={
                "data_exclusions": ["00883: current data is stale"],
                "data_readiness_issues": [
                    {
                        "code": "00883",
                        "reason": "local=stale; fetch=HTTP 403 <blocked>",
                    },
                    {"code": "VOO", "reason": "benchmark fetch failed"},
                ],
            },
        )
    })

    body = build_optimizer_summary(report)

    assert "策略优化未执行" in optimizer_notification_title(report)
    assert "候选评估 0 次" in body
    assert "00883: local=stale; fetch=HTTP 403 &lt;blocked&gt;" in body
    assert body.count("00883:") == 1
    assert "VOO: benchmark fetch failed" in body
    assert "<blocked>" not in body
    assert "当前生产指针保持不变" in body


def test_existing_receipts_without_readiness_keep_exclusion_reason():
    report = _report({
        "us": OptimizerGroupSummary(
            group="us", status="no_symbols",
            ranking_diagnostics={"data_exclusions": ["VOO: current data is stale"]},
        )
    })
    assert "VOO: current data is stale" in build_optimizer_summary(report)


def test_each_readiness_cause_for_the_same_symbol_survives_deduplication():
    report = _report(
        {
            "hk": OptimizerGroupSummary(
                group="hk",
                status="no_symbols",
                ranking_diagnostics={
                    "data_readiness_issues": [
                        {"code": "00883", "reason": "fetch=HTTP 403"},
                        {"code": "00883", "reason": "fundamental statements missing"},
                        {"code": "00883", "reason": "fetch=HTTP 403"},
                    ],
                    "data_exclusions": ["00883: fetch=HTTP 403"],
                },
            )
        }
    )

    body = build_optimizer_summary(report)

    assert body.count("00883:") == 1
    assert body.count("fetch=HTTP 403") == 1
    assert body.count("fundamental statements missing") == 1


@pytest.mark.parametrize("status", ["completed", "no_candidates"])
def test_actual_search_keeps_completed_title(status):
    report = _report({"hk": OptimizerGroupSummary(group="hk", status=status)})
    assert optimizer_notification_title(report) == "策略优化完成"
    report.status = "failed"
    assert optimizer_notification_title(report) == "策略优化部分失败"


def test_data_readiness_failure_is_persisted_before_no_symbols_return(
    tmp_path, monkeypatch
):
    config = main.load_config()
    market_config = get_market_optimizer_config("hk", config)
    issue = DataReadinessIssue(
        "00883", "yahoo_chart", "2018-03-27", "2026-09-23", "fetch=HTTP 403"
    )
    readiness = type("Readiness", (), {"issues": [issue], "bundles": {}})()
    monkeypatch.setattr(main, "prepare_backtest_data", lambda *a, **k: readiness)
    monkeypatch.setattr(
        main, "_load_optimizer_market_bundles_with_errors",
        lambda *a, **k: ({}, ["00883: current data is stale"]),
    )
    monkeypatch.chdir(tmp_path)
    reports = []

    main._run_optimization_group(config, "hk", market_config, report_sink=reports)

    assert len(reports) == 1
    group = reports[0].groups["hk"]
    assert group.status == "no_data"
    assert group.ranking_diagnostics["data_readiness_issues"] == [issue.as_dict()]
    assert "fetch=HTTP 403" in build_optimizer_summary(reports[0])
    assert not Path("data/optimizer/latest_strategy.yaml").exists()


def _isolated_optimizer_inputs(monkeypatch, tmp_path):
    """Keep the configured full pool, mocking only data/search boundaries."""
    config = main.load_config()
    market_config = get_market_optimizer_config("hk", config)
    codes = [
        main._stock_code(stock)
        for stock in config["stocks"]
        if main._detect_fine_group(main._stock_code(stock)) == "hk"
    ]
    benchmark_codes = [
        code
        for code in market_config.constraints.benchmark_codes_for("hk")
        if code != "risk_free"
    ]
    closed_dates = pd.to_datetime(["2026-09-21", "2026-09-22"])
    bundles = {
        code: SimpleNamespace(
            prices=pd.DataFrame(
                {
                    "date": closed_dates,
                    "qfq_open": [10.0, 11.0],
                    "qfq_high": [10.5, 11.5],
                    "qfq_low": [9.5, 10.5],
                    "qfq_close": [10.0, 11.0],
                    "volume": [100.0, 200.0],
                    "tradable": [True, True],
                }
            )
        )
        for code in [*codes, *benchmark_codes]
    }
    loaders = []
    for name in (
        "_load_optimizer_market_bundles_with_errors",
        "_load_optimizer_market_bundles",
        "_load_optimizer_benchmark_bundles",
    ):
        loader = Mock(side_effect=AssertionError("readiness must remain authoritative"))
        monkeypatch.setattr(main, name, loader)
        loaders.append(loader)
    monkeypatch.setattr(main, "load_latest_strategy_run", lambda **kwargs: None)
    monkeypatch.setattr(main, "prune_optimizer_runs", lambda **kwargs: None)
    monkeypatch.setattr(main, "publish_complete_run", lambda *args, **kwargs: False)
    monkeypatch.setattr(main, "_strategy_context_enricher", lambda *args: None)
    monkeypatch.chdir(tmp_path)
    return config, market_config, codes, benchmark_codes, bundles, loaders


def test_optimizer_fails_closed_on_incomplete_configured_universe(
    tmp_path, monkeypatch
):
    config, market_config, codes, benchmarks, bundles, loaders = (
        _isolated_optimizer_inputs(monkeypatch, tmp_path)
    )
    failed_code = codes[0]
    issue = DataReadinessIssue(
        failed_code, "fundamental", "2018-03-27", "2026-09-23", "statements missing"
    )
    # A later readiness check can fail after the price bundle was accepted.
    # Keeping that bundle in the result must never put the symbol into search.
    readiness = SimpleNamespace(issues=[issue], bundles=bundles)
    monkeypatch.setattr(main, "prepare_backtest_data", lambda *a, **k: readiness)
    search = Mock(side_effect=AssertionError("incomplete universe must block search"))
    monkeypatch.setattr(main, "run_optimizer", search)
    reports = []

    main._run_optimization_group(config, "hk", market_config, report_sink=reports)

    search.assert_not_called()
    for loader in loaders:
        loader.assert_not_called()
    assert reports[0].groups["hk"].ranking_diagnostics["data_readiness_issues"] == [
        issue.as_dict()
    ]
    assert reports[0].groups["hk"].status == "no_data"


@pytest.mark.parametrize("keep_failed_bundle", [False, True])
def test_benchmark_readiness_failure_blocks_search_without_store_fallback(
    tmp_path, monkeypatch, keep_failed_bundle
):
    config, market_config, _codes, benchmarks, bundles, loaders = (
        _isolated_optimizer_inputs(monkeypatch, tmp_path)
    )
    failed_benchmark = benchmarks[0]
    if not keep_failed_bundle:
        bundles.pop(failed_benchmark)
    issue = DataReadinessIssue(
        failed_benchmark, "yahoo_chart", "2018-03-27", "2026-09-23", "HTTP 403"
    )
    readiness = SimpleNamespace(issues=[issue], bundles=bundles)
    monkeypatch.setattr(main, "prepare_backtest_data", lambda *a, **k: readiness)
    search = Mock(side_effect=AssertionError("benchmark failure must block search"))
    monkeypatch.setattr(main, "run_optimizer", search)
    reports = []

    completed = main._run_optimization_group(
        config, "hk", market_config, report_sink=reports
    )

    assert completed == {}
    search.assert_not_called()
    for loader in loaders:
        loader.assert_not_called()
    assert len(reports) == 1
    report = reports[0]
    assert report.groups["hk"].status == "no_data"
    assert report.groups["hk"].evaluated_count == 0
    assert f"{failed_benchmark}: HTTP 403" in report.failure_reason
    assert "HTTP 403" in build_optimizer_summary(report)
    assert not Path("data/optimizer/latest_strategy.yaml").exists()


@pytest.mark.parametrize(
    ("statuses", "expected_title"),
    [
        (("failed", "failed"), "策略优化失败"),
        (("no_symbols", "failed"), "策略优化失败"),
        (("no_candidates", "failed"), "策略优化部分失败"),
        (("no_data", "not_run"), "策略数据未就绪"),
        (("no_symbols", "no_symbols"), "策略优化未执行"),
    ],
)
def test_merged_optimizer_notification_never_calls_unexecuted_failures_complete(
    monkeypatch, statuses, expected_title
):
    config = main.load_config()
    captured = []

    def fake_group_run(config, group, market_config, *, report_sink):
        status = dict(zip(("hk", "us"), statuses))[group]
        report_sink.append(
            OptimizerRunSummary(
                "percentile",
                "test",
                "2026-09-23T02:00:00",
                1,
                {group: OptimizerGroupSummary(group=group, status=status)},
                status="failed" if status == "failed" else "completed",
                failure_reason="benchmark unavailable" if status == "failed" else "",
            )
        )
        return {}

    monkeypatch.setattr(main, "_run_optimization_group", fake_group_run)
    monkeypatch.setattr(
        main, "_notify_optimizer_run", lambda config, report: captured.append(report)
    )

    main.run_optimization(config, target_groups=("hk", "us"))

    assert len(captured) == 1
    assert optimizer_notification_title(captured[0]) == expected_title
    assert build_optimizer_summary(captured[0]).startswith(f"<b>{expected_title}</b>")


def test_empty_configured_universe_is_unexecuted_after_market_receipts_merge(
    tmp_path, monkeypatch
):
    config = main.load_config()
    config["stocks"] = []
    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        main, "_notify_optimizer_run", lambda config, report: captured.append(report)
    )

    assert main.run_optimization(config) == {}

    assert len(captured) == 1
    report = captured[0]
    assert set(report.groups) == {"a_share", "hk", "us"}
    assert all(item.status == "no_symbols" for item in report.groups.values())
    assert optimizer_notification_title(report) == "策略优化未执行"
