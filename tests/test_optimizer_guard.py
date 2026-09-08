from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

from src import optimizer_guard
from src.notification.email_notifier import (
    build_optimizer_summary,
    optimizer_notification_title,
)
from src.optimizer_guard import _discover_run, _failure_reason, _group_summary
from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary


def test_guard_discovers_new_run_and_counts_interrupted_archive(tmp_path):
    runs = tmp_path / "runs"
    old = runs / "old_run"
    new = runs / "new_run"
    old.mkdir(parents=True)
    new.mkdir()
    archive = new / "a_share_search_archive.jsonl"
    archive.write_text("one\ntwo\nthree\n", encoding="utf-8")

    discovered = _discover_run(runs, {old.name}, 0.0)
    application = SimpleNamespace(OptimizerGroupSummary=OptimizerGroupSummary)
    summary = _group_summary(application, discovered, "a_share")

    assert discovered == new
    assert summary.status == "interrupted"
    assert summary.evaluated_count == 3


def test_sigkill_failure_summary_uses_failure_title_and_progress():
    report = OptimizerRunSummary(
        strategy_name="technical_ensemble",
        strategy_label="22 factors",
        timestamp="2026-08-03T12:00:00",
        elapsed_seconds=900,
        groups={
            "a_share": OptimizerGroupSummary(
                group="a_share",
                evaluated_count=20480,
                status="interrupted",
            ),
            "hk": OptimizerGroupSummary(group="hk"),
            "us": OptimizerGroupSummary(group="us"),
        },
        status="failed",
        failure_reason=_failure_reason(-9),
    )

    title = optimizer_notification_title(report, report.strategy_label)
    body = build_optimizer_summary(report)

    assert title.startswith("\u7b56\u7565\u4f18\u5316\u5931\u8d25")
    assert "SIGKILL" in body
    assert "20,480" in body
    assert "\u5f02\u5e38\u4e2d\u6b62" in body


def test_started_run_without_evaluations_is_a_retained_terminal_failure(tmp_path):
    application = SimpleNamespace(OptimizerGroupSummary=OptimizerGroupSummary)
    summary = _group_summary(application, tmp_path, "hk")
    assert summary.status == "failed"
    assert summary.evaluated_count == 0


def test_guard_parent_reports_sigkill_after_child_releases_memory(monkeypatch):
    observed = {}

    def fake_run(command, cwd, env, check):
        observed["command"] = command
        observed["marker"] = env[optimizer_guard.CHILD_MARKER]
        assert check is False
        return SimpleNamespace(returncode=-9)

    def fake_notify(*args, target_groups):
        observed["returncode"] = args[-1]
        observed["target_groups"] = target_groups

    monkeypatch.setattr(optimizer_guard.subprocess, "run", fake_run)
    monkeypatch.setattr(optimizer_guard, "_notify_failure", fake_notify)

    assert optimizer_guard.main() == 137
    assert observed["marker"] == "1"
    assert observed["returncode"] == -9
    assert observed["command"][-1] == "--optimize"
    assert observed["target_groups"] == ("a_share", "hk", "us")


def test_guard_passes_single_market_to_child_and_failure_report(monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return SimpleNamespace(returncode=1)

    def fake_notify(*args, target_groups):
        observed["groups"] = target_groups

    monkeypatch.setattr(optimizer_guard.subprocess, "run", fake_run)
    monkeypatch.setattr(optimizer_guard, "_notify_failure", fake_notify)

    assert optimizer_guard.main(["--group", "hk"]) == 1
    assert observed["command"][-3:] == ["--optimize", "--group", "hk"]
    assert observed["groups"] == ("hk",)


@pytest.mark.parametrize("groups", [("a_share", "hk", "us"), ("hk",)])
@pytest.mark.parametrize("load_failure", [False, True, "exit"])
def test_startup_failure_keeps_config_error_and_independent_receipts(
    tmp_path, monkeypatch, groups, load_failure
):
    reason = (
        "unable to load config/config.yaml (loader exited with code 1)"
        if load_failure == "exit"
        else "global optimizer fallback fields are forbidden: engine"
    )
    observed = {}

    def fail_config(*args, **kwargs):
        if load_failure == "exit":
            raise SystemExit(1)
        raise ValueError(reason)

    def notify(config, report):
        observed["report"] = report

    application = SimpleNamespace(
        load_config=fail_config if load_failure else lambda: {"optimizer": {}},
        get_market_optimizer_configs=fail_config,
        OptimizerGroupSummary=OptimizerGroupSummary,
        OptimizerRunSummary=OptimizerRunSummary,
        yaml=yaml,
        _notify_optimizer_run=notify,
    )
    monkeypatch.setitem(sys.modules, "main", application)
    monkeypatch.setattr(sys, "path", list(sys.path))

    optimizer_guard._notify_failure(
        tmp_path,
        set(),
        datetime(2026, 9, 7, 2, tzinfo=timezone.utc),
        0,
        1.5,
        1,
        target_groups=groups,
    )

    report = observed["report"]
    assert reason in report.failure_reason
    assert reason in build_optimizer_summary(report)
    assert set(report.groups) == set(groups)
    assert all(summary.status == "failed" for summary in report.groups.values())
    assert report.strategy_by_group == {}
    assert report.run_ids_by_group == {}
    assert not report.activated and not report.candidate
    receipts = list(
        (tmp_path / "data/optimizer/failures").glob("*/optimizer_failure.yaml")
    )
    assert len(receipts) == len(groups)
    for path in receipts:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert reason in payload["failure_reason"]
        assert payload["returncode"] == 1
        assert len(payload["groups"]) == 1
        group = next(iter(payload["groups"]))
        assert path.parent.name.endswith(f"_{group}")
        assert payload["groups"][group]["status"] == "failed"
    assert not (tmp_path / "data/optimizer/runs").exists()
    assert not (tmp_path / "data/optimizer/latest_strategy.yaml").exists()


def test_failure_receipt_does_not_mix_or_overwrite_other_market_runs(
    tmp_path, monkeypatch
):
    runs = tmp_path / "data/optimizer/runs"
    hk_run = runs / "20260907T020000_regime_pullback_hk"
    us_run = runs / "20260907T020001_percentile_us"
    hk_run.mkdir(parents=True)
    us_run.mkdir()
    (hk_run / "hk_search_archive.jsonl").write_text("one\ntwo\n", encoding="utf-8")
    market_config = SimpleNamespace(
        strategy=SimpleNamespace(name="regime_pullback", label="regime pullback"),
        solver_id="simulated_annealing",
        gate_profile="standard",
        walk_forward_profile="hk_84m",
        execution_profile="hk_hkd",
        benchmark_profile="hk",
        config_hash="hk-config-hash",
        search=SimpleNamespace(run_retention_count=3),
    )
    observed = {}

    def configs(config, *, groups):
        assert groups == ("hk",)
        return {"hk": market_config}

    def notify(config, report):
        observed["report"] = report

    application = SimpleNamespace(
        load_config=dict,
        get_market_optimizer_configs=configs,
        OptimizerGroupSummary=OptimizerGroupSummary,
        OptimizerRunSummary=OptimizerRunSummary,
        yaml=yaml,
        _notify_optimizer_run=notify,
        prune_optimizer_runs=lambda **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "main", application)
    monkeypatch.setattr(sys, "path", list(sys.path))

    optimizer_guard._notify_failure(
        tmp_path,
        set(),
        datetime(2026, 9, 7, 2, tzinfo=timezone.utc),
        0,
        1.5,
        -9,
        target_groups=("hk",),
    )

    report = observed["report"]
    assert set(report.groups) == {"hk"}
    assert report.groups["hk"].evaluated_count == 2
    assert report.groups["hk"].solver_id == "simulated_annealing"
    assert report.run_ids_by_group == {"hk": hk_run.name}
    payload = yaml.safe_load((hk_run / "optimizer_failure.yaml").read_text("utf-8"))
    assert set(payload["groups"]) == {"hk"}
    assert "SIGKILL" in payload["failure_reason"]
    assert not (us_run / "optimizer_failure.yaml").exists()
