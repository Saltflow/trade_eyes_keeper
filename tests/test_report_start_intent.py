"""Durable report-start intent shares the claim without weakening ownership."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src.core import report_execution as reports
from src.core.execution import (
    ExecutionRuntime,
    ExecutionStore,
    JobSpec,
    ModuleSpec,
    RunContext,
    RunResult,
)


def declared_job(name="report"):
    return JobSpec(
        name,
        lambda *_: RunResult(),
        starting_side_effect="report_starting",
    )


def claimed_store(tmp_path):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    store.apply(ModuleSpec("test", (declared_job(),)))
    run_id = store.submit("report")
    row = store.claim(
        "owner",
        "default",
        ["report"],
        starting_side_effects={"report": "report_starting"},
    )
    assert row["id"] == run_id
    return store, row


def test_start_intent_is_committed_with_claim(tmp_path):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    store.apply(ModuleSpec("test", (declared_job(),)))
    run_id = store.submit("report")
    statements = []
    original = store.connection

    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as db:
            db.set_trace_callback(statements.append)
            try:
                yield db
            finally:
                # Context exit commits; keep its trace installed for this test.
                pass

    store.connection = traced
    row = store.claim(
        "owner",
        "default",
        ["report"],
        starting_side_effects={"report": "report_starting"},
    )
    assert sum(sql.strip().upper() == "COMMIT" for sql in statements) == 1
    with sqlite3.connect(store.path) as independent:
        persisted = independent.execute(
            "SELECT status,side_effect,stage FROM execution_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    assert persisted == ("running", 1, "report_starting")
    context = RunContext(store, row)
    progress = Mock(wraps=store.progress)
    store.progress = progress
    assert context.mark_side_effect("report_starting") is True
    progress.assert_not_called()


@pytest.mark.parametrize("stage", ["", True, 1, "a" * 161])
def test_start_intent_stage_rejects_invalid_declarations(stage):
    with pytest.raises((TypeError, ValueError)):
        JobSpec("bad", lambda *_: None, starting_side_effect=stage)


def test_plain_job_preserves_original_fingerprint_and_start_state(tmp_path):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    job = JobSpec("plain", lambda *_: RunResult())
    store.apply(ModuleSpec("plain_module", (job,)))
    old_fields = [
        [job.id, job.lane, job.concurrency_key, job.timeout_seconds, job.recovery]
    ]
    old_fingerprint = json.dumps(
        old_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    with store.connection() as db:
        fingerprint = db.execute(
            "SELECT fingerprint FROM execution_modules WHERE id='plain_module'"
        ).fetchone()[0]
    assert fingerprint == old_fingerprint
    run_id = store.submit("plain")
    row = store.claim(
        "owner",
        "default",
        ["plain"],
        starting_side_effects={"other": "report_starting"},
    )
    assert row["id"] == run_id
    assert row["stage"] == "starting" and row["side_effect"] == 0


def test_claim_rollback_cannot_leave_intent_or_dispatch(tmp_path):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    store.apply(ModuleSpec("test", (declared_job(),)))
    run_id = store.submit("report")
    original = store.connection

    @contextmanager
    def fail_commit(**kwargs):
        with original(**kwargs) as db:
            yield db
            raise sqlite3.OperationalError("controlled precommit failure")

    store.connection = fail_commit
    with pytest.raises(sqlite3.OperationalError):
        store.claim(
            "owner",
            "default",
            ["report"],
            starting_side_effects={"report": "report_starting"},
        )
    store.connection = original
    row = store.get(run_id)
    assert row["status"] == "queued" and row["side_effect"] == 0
    assert row["claim_token"] is None and row["started"] is None


def test_claimed_report_request_failure_is_conservatively_unknown(
    tmp_path, monkeypatch
):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    runtime = ExecutionRuntime(store)
    config = {"storage": {"data_dir": str(tmp_path)}}
    reports.register_report_jobs(runtime, config=config)
    launch = Mock()
    monkeypatch.setattr(reports.subprocess, "Popen", launch)
    monkeypatch.setattr(reports, "_write_json", Mock(side_effect=OSError("controlled")))
    runtime.start()
    try:
        run_id = runtime.submit(
            "reports.daily", {"kind": "daily", "artifact_root": str(tmp_path)}
        )
        row = runtime.wait(run_id, 5)
        assert row["status"] == "unknown" and row["side_effect"] == 1
        assert row["reason"] == "handler_raised:OSError"
    finally:
        runtime.stop(drain=True)
    launch.assert_not_called()


@pytest.mark.parametrize("change", ["cancel", "token", "status", "flag"])
def test_idempotent_mark_rechecks_current_ownership(tmp_path, change):
    store, row = claimed_store(tmp_path)
    context = RunContext(store, row)
    if change == "cancel":
        context.cancel_event.set()
    else:
        columns = {
            "token": "claim_token='other'",
            "status": "status='unknown'",
            "flag": "side_effect=0",
        }
        with store.connection() as db:
            db.execute(
                f"UPDATE execution_runs SET {columns[change]} WHERE id=?", (row["id"],)
            )
    assert context.mark_side_effect("report_starting") is False


def test_runtime_observes_durable_intent_before_handler(tmp_path):
    store = ExecutionStore(tmp_path / "runs.sqlite3")
    runtime = ExecutionRuntime(store)
    observed = []

    def handle(_payload, context):
        with sqlite3.connect(store.path) as db:
            observed.append(
                db.execute(
                    "SELECT side_effect,stage FROM execution_runs WHERE id=?",
                    (context.run_id,),
                ).fetchone()
            )
        assert context.mark_side_effect("report_starting")
        return RunResult()

    runtime.register(
        ModuleSpec(
            "reports",
            (JobSpec("report", handle, starting_side_effect="report_starting"),),
        )
    )
    runtime.start()
    try:
        run_id = runtime.submit("report")
        assert runtime.wait(run_id, 5)["status"] == "succeeded"
    finally:
        runtime.stop(drain=True)
    assert observed == [(1, "report_starting")]


def test_unacknowledged_start_never_launches_child(tmp_path, monkeypatch):
    context = SimpleNamespace(run_id="rejected", mark_side_effect=lambda *_: False)
    launch = Mock()
    monkeypatch.setattr(reports.subprocess, "Popen", launch)
    result = reports._report_handler(
        {"kind": "daily", "artifact_root": str(tmp_path)}, context
    )
    assert result.status == "unknown"
    assert result.reason == "report_start_not_authorized"
    launch.assert_not_called()


def test_diagnostic_phases_coalesce_but_receipt_still_decides_result(
    tmp_path, monkeypatch
):
    clock = [0.0]
    progress = []
    directory = tmp_path / "phases"
    directory.mkdir()
    phase_path = directory / "phase.json"
    phase_path.write_text(json.dumps({"stage": "phase-0"}), encoding="utf8")
    (directory / "result.json").write_text(
        json.dumps({"status": "partial", "reason": "one_channel_unknown"}),
        encoding="utf8",
    )
    context = SimpleNamespace(
        run_id="phases",
        cancel_event=threading.Event(),
        mark_side_effect=lambda *_: True,
        progress=lambda stage: progress.append((clock[0], stage)),
    )

    class Child:
        returncode = 1

        def poll(self):
            return None if clock[0] < 26 else 1

        def wait(self, timeout=None):
            if timeout is not None:
                clock[0] += 2
                phase_path.write_text(
                    json.dumps({"stage": f"phase-{clock[0]:g}"}), encoding="utf8"
                )
                raise reports.subprocess.TimeoutExpired("controlled", timeout)
            return 1

    monkeypatch.setattr(reports, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(reports.subprocess, "Popen", lambda *_args, **_kw: Child())
    result = reports._report_handler(
        {"kind": "daily", "artifact_root": str(tmp_path)}, context
    )
    assert progress == [(10.0, "phase-10"), (20.0, "phase-20")]
    assert result.status == "partial" and result.reason == "one_channel_unknown"
    assert json.loads(phase_path.read_text())["stage"] == "phase-26"


def test_cli_worker_reuses_its_already_loaded_application(monkeypatch, tmp_path):
    import main

    call = Mock(return_value=0)
    monkeypatch.setattr(reports, "run_report_worker", call)
    assert (
        main.main(
            [
                "--report-worker",
                "daily",
                "--report-payload",
                str(tmp_path / "payload"),
                "--report-result",
                str(tmp_path / "result"),
            ]
        )
        == 0
    )
    assert call.call_args.kwargs["application"] is main


@pytest.mark.parametrize("kind", ["daily", "brief"])
def test_injected_application_preserves_pinned_config_and_receipt(tmp_path, kind):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"stocks": ["NEW"]}), encoding="utf8")
    payload = tmp_path / "request.json"
    payload.write_text(
        json.dumps(
            {
                "kind": kind,
                "config_path": str(config_path),
                "config_snapshot": {"stocks": ["PINNED"]},
                "report_id": "afternoon_snapshot",
                "force": True,
            }
        ),
        encoding="utf8",
    )
    result_path = tmp_path / "result.json"
    business = Mock(return_value=RunResult("partial", "preserved_partial"))
    application = SimpleNamespace(
        setup_logging=Mock(), run_daily_task=business, run_brief_report=business
    )
    assert (
        reports.run_report_worker(kind, payload, result_path, application=application)
        == 1
    )
    assert business.call_args.kwargs["config"]["stocks"] == ["PINNED"]
    assert business.call_args.kwargs["force"] is True
    assert json.loads(result_path.read_text())["status"] == "partial"
