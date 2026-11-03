"""Persisted recurring occurrences do not create empty clock jobs all day."""

from datetime import datetime, timezone

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.report_execution import runtime_path
from src.triggers.runtime import TriggerRuntime


def test_today_recurring_slot_already_admitted_does_not_create_empty_runs(tmp_path):
    now = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)
    config = {"storage": {"data_dir": str(tmp_path)}}
    execution = ExecutionRuntime(ExecutionStore(runtime_path(config)))
    trigger = TriggerRuntime(config, execution, clock=lambda: now)
    trigger.store.create(
        "alice",
        {
            "label": "每日日报",
            "schedule": {"kind": "daily", "at": "09:50"},
            "actions": [{"kind": "daily", "payload": {"chat_id": "chat"}}],
        },
        "recurring",
    )
    trigger.evaluator.evaluate(None, now=now)
    action = trigger.store.actions("alice")[0]
    trigger.store.mark_failed(action["id"], "synthetic_rejection", now=now)
    assert trigger.store.get("alice", 1)["state"] == "active"
    trigger.started = True
    for _ in range(3):
        trigger._timer()
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM execution_runs").fetchone()[0] == 0


def test_idle_timer_retires_expired_window_without_a_report_job(tmp_path):
    now = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
    config = {"storage": {"data_dir": str(tmp_path)}}
    execution = ExecutionRuntime(ExecutionStore(runtime_path(config)))
    trigger = TriggerRuntime(config, execution, clock=lambda: now)
    trigger.store.create(
        "alice",
        {
            "schedule": {"at": "2026-11-02T09:50+08:00", "window_seconds": 60},
            "actions": [{"kind": "daily", "payload": {"chat_id": "chat"}}],
        },
        "missed",
    )
    trigger.started = True
    trigger._timer()
    assert trigger.store.get("alice", 1)["state"] == "done"
    assert trigger.store.list("alice") == []
    assert trigger.store.actions("alice") == []
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM execution_runs").fetchone()[0] == 0
