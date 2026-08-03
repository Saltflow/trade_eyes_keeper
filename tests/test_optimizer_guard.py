from __future__ import annotations

from types import SimpleNamespace

import src.optimizer_guard as optimizer_guard
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


def test_guard_parent_reports_sigkill_after_child_releases_memory(monkeypatch):
    observed = {}

    def fake_run(command, cwd, env, check):
        observed["command"] = command
        observed["marker"] = env[optimizer_guard.CHILD_MARKER]
        assert check is False
        return SimpleNamespace(returncode=-9)

    def fake_notify(*args):
        observed["returncode"] = args[-1]

    monkeypatch.setattr(optimizer_guard.subprocess, "run", fake_run)
    monkeypatch.setattr(optimizer_guard, "_notify_failure", fake_notify)

    assert optimizer_guard.main() == 137
    assert observed["marker"] == "1"
    assert observed["returncode"] == -9
    assert observed["command"][-1] == "--optimize"
