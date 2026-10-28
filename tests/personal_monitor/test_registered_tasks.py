"""Shared-host migration contract; no network or production data."""

import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.core.execution import (
    ExecutionRuntime,
    ExecutionStore,
    JobSpec,
    ModuleSpec,
    RunResult,
)
from src.personal_monitor import MonitorStore
from src.personal_monitor.tasks import market_deadlines
from src.personal_monitor_runtime import PersonalMonitorRuntime


def clock(text="2026-10-21T14:31:00+08:00"):
    return datetime.fromisoformat(text)


def make_monitor(tmp_path, *, config=None, execution=None, engine=None, send=None):
    return PersonalMonitorRuntime(
        MonitorStore(tmp_path / "personal.sqlite3"),
        config or {"scheduler": {"run_time": "19:00"}},
        send or Mock(return_value={"outcome": "sent"}),
        engine=engine,
        observation_provider=Mock(),
        execution_runtime=execution,
    )


def test_catchup_within_existing_grace_and_future_deadline():
    config = {
        "scheduler": {
            "run_time": "19:00",
            "daily_enabled": False,
            "brief_reports": [
                {"run_time": "14:30", "enabled": False, "skip_weekends": True}
            ],
        }
    }
    catchup, future = market_deadlines(config, clock())
    assert catchup == clock("2026-10-21T14:30:00+08:00")
    assert future == clock("2026-10-21T19:00:00+08:00")
    assert market_deadlines(config, clock("2026-10-21T14:46:00+08:00"))[0] is None
    assert market_deadlines(config, clock("2026-10-24T14:31:00+08:00"))[0] is None
    assert market_deadlines(config, clock("2026-10-24T19:01:00+08:00"))[0]


def test_failed_market_is_not_completed_and_completion_survives_restart(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.store.ensure_account("u", "a")
    evaluate = Mock(side_effect=[RuntimeError("unavailable"), {"checked": 0}])
    monitor.engine.evaluate = evaluate
    at = clock("2026-10-21T19:00:00+08:00")
    with pytest.raises(RuntimeError):
        monitor.check_market(at)
    assert not monitor._tasks.slot_completed("2026-10-21T19:00+0800")
    monitor.check_market(at + timedelta(seconds=2))
    restored = make_monitor(tmp_path)
    restored.engine.evaluate = Mock()
    restored.check_market(at + timedelta(seconds=3))
    restored.engine.evaluate.assert_not_called()
    assert evaluate.call_count == 2


def test_registration_preserves_all_existing_business_rows(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.store.register_route("u", "chat")
    monitor.store.ensure_account("u", "a")
    monitor.store.import_snapshot(
        "u",
        "a",
        [{"instrument": "601088", "quantity": 100}],
        clock(),
        0,
        source="manual",
    )
    monitor.store.create_reminder(
        "u", "r", clock() + timedelta(days=28), "maturity", "chat", now=clock()
    )
    with monitor.store.connection() as db:
        names = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        before = {
            name: [tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')]
            for name in names
        }
    monitor._registered_tasks().apply_config(monitor.config)
    with monitor.store.connection() as db:
        after = {
            name: [tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')]
            for name in names
        }
    assert after == before


def test_market_attempt_limit_and_delay_survive_facade_recreation(tmp_path):
    monitor = make_monitor(tmp_path)
    task = monitor._registered_tasks()
    task._timer = Mock()
    at = clock()
    slot = "2026-10-21T14:30+0800"
    assert task._market_attempt(slot, at) == 1
    assert task._market_attempt(slot, at + timedelta(seconds=59)) is None
    restored = make_monitor(tmp_path)._registered_tasks()
    restored._timer = Mock()
    assert restored._market_attempt(slot, at + timedelta(minutes=1)) == 2
    assert restored._market_attempt(slot, at + timedelta(minutes=2)) == 3
    assert restored._market_attempt(slot, at + timedelta(minutes=3)) is None


def test_queued_market_job_expired_before_claim_uses_no_stale_observation(tmp_path):
    monitor = make_monitor(tmp_path)
    task = monitor._registered_tasks()
    monitor.check_market = Mock(side_effect=AssertionError("must not fetch"))
    result = task._market(
        {
            "slot": "2026-10-21T19:00+0800",
            "scheduled_for": "2026-10-21T19:00:00+08:00",
            "config": monitor.config,
        },
        Mock(),
    )
    assert result.status == "skipped"
    assert result.reason == "market_slot_expired"


def test_deadline_queries_use_indexes_and_do_not_enumerate_accounts(tmp_path):
    monitor = make_monitor(tmp_path)
    task = monitor._registered_tasks()
    monitor.store.list_accounts = Mock(
        side_effect=AssertionError("must not scan facts")
    )
    assert task.next_deadlines(clock()) == (None, None)
    with monitor.store.connection() as db:
        plans = [
            db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            for query in (
                "SELECT MIN(due_at) FROM reminders WHERE state='pending'",
                "SELECT 1 FROM outbox WHERE state='queued' LIMIT 1",
                (
                    "SELECT MIN(COALESCE(next_attempt_at,created_at)) "
                    "FROM outbox WHERE state='failed'"
                ),
                (
                    "SELECT MIN(claimed_at) FROM outbox "
                    "WHERE state IN ('claimed','submitted')"
                ),
            )
        ]
    assert all("USING" in str(tuple(plan[0])) for plan in plans)


def test_next_deadline_includes_backoff_and_expired_claim_without_resend(tmp_path):
    monitor = make_monitor(tmp_path)
    task = monitor._registered_tasks()
    at = clock()
    monitor.store.register_route("u", "chat")
    monitor.store.create_reminder(
        "u", "r", at, "due", "chat", now=at - timedelta(minutes=1)
    )
    assert task.next_deadlines(at) == (at.astimezone(timezone.utc), None)
    monitor.engine.enqueue_due(at)
    item = monitor.engine.claim_delivery("dead-worker", at)
    assert task.next_deadlines(at)[1] == at + timedelta(seconds=300)
    monitor.engine.mark_submitted(item["id"], item["token"], now=at)
    monitor.deliver_one(at + timedelta(seconds=301))
    monitor.send_callback.assert_not_called()
    assert monitor.store.status("u")["reminders"][0]["state"] == "unknown"


def test_injected_host_is_not_started_or_stopped_by_personal_module(tmp_path):
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "host.sqlite3"))
    execution.register(ModuleSpec("other", (JobSpec("probe", lambda *_: RunResult()),)))
    monitor = make_monitor(tmp_path, execution=execution)
    monitor.start()
    assert not execution.running
    execution.start()
    try:
        monitor.stop()
        assert execution.running
        row = execution.wait(execution.submit("probe"), timeout=3)
        assert row["status"] == "succeeded"
        assert not execution.module_status("personal_monitor")["enabled"]
    finally:
        execution.stop()


def test_new_reminder_wake_reschedules_exact_due_and_sends_once(tmp_path):
    sent = threading.Event()
    monitor = make_monitor(
        tmp_path,
        send=lambda *_: sent.set() or {"outcome": "sent", "platform_message_id": "p1"},
    )
    monitor.store.register_route("u", "chat")
    monitor.start()
    try:
        now = datetime.now(timezone.utc)
        monitor.store.create_reminder(
            "u", "r", now + timedelta(seconds=0.3), "due", "chat", now=now
        )
        monitor.wake()
        assert sent.wait(10)
        monitor.wake()
        with monitor.store.connection() as db:
            assert db.execute("SELECT attempts FROM outbox").fetchone()[0] == 1
        assert monitor.threads == [monitor.execution_runtime._thread]
        assert not hasattr(monitor, "_run")
    finally:
        monitor.stop()


def test_public_enable_restores_overdue_reminder_and_preserves_other_modules(tmp_path):
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "host.sqlite3"))
    execution.register(ModuleSpec("other", (JobSpec("probe", lambda *_: RunResult()),)))
    sent = threading.Event()
    monitor = make_monitor(
        tmp_path,
        execution=execution,
        send=lambda *_: sent.set() or {"outcome": "sent"},
    )
    monitor.store.register_route("u", "chat")
    monitor.start()
    execution.start()
    try:
        execution.disable("personal_monitor")
        assert not monitor._tasks.started
        now = datetime.now(timezone.utc)
        monitor.store.create_reminder(
            "u", "r", now + timedelta(seconds=0.05), "due", "chat", now=now
        )
        monitor.wake()  # A disabled module cannot revive its own timers.
        assert not sent.wait(0.15)
        assert (
            execution.wait(execution.submit("probe"), timeout=3)["status"]
            == "succeeded"
        )
        execution.enable("personal_monitor")
        assert monitor._tasks.started
        assert sent.wait(10), "public enable lost the persisted due deadline"
        monitor.wake()
        with monitor.store.connection() as db:
            assert db.execute("SELECT attempts FROM outbox").fetchone()[0] == 1
    finally:
        monitor.stop()
        execution.stop()


def test_idle_reconciliation_has_no_run_records_and_snapshot_omits_credentials(
    tmp_path,
):
    monitor = make_monitor(
        tmp_path,
        config={
            "scheduler": {"run_time": "19:00"},
            "llm": {"api_key": "must-not-persist"},
        },
    )
    task = monitor._registered_tasks()
    task.apply_config(monitor.config)
    task.started = True
    try:
        task.slot_completed = lambda *_: True
        for _ in range(3):
            task.reconcile()
        assert monitor.execution_runtime.store.list() == []
        assert "must-not-persist" not in json.dumps(task._config)
        assert set(monitor.execution_runtime._timers) <= {
            "personal_monitor:market",
            "personal_monitor:reconcile",
        }
    finally:
        task.stop()


def test_invalid_hot_config_retains_active_registration_and_clock(tmp_path):
    monitor = make_monitor(tmp_path)
    task = monitor._registered_tasks()
    task.apply_config(monitor.config)
    original = monitor.execution_runtime.module_status(task.MODULE)
    with pytest.raises(ValueError):
        task.apply_config({"scheduler": {"run_time": "99:99"}})
    current = monitor.execution_runtime.module_status(task.MODULE)
    assert current["version"] == original["version"]
    assert current["generation"] == original["generation"]
    assert task._config["scheduler"]["run_time"] == "19:00"


def test_valid_hot_config_does_not_revive_disabled_module(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.start()
    try:
        monitor.execution_runtime.disable("personal_monitor")
        monitor.apply_config({"scheduler": {"run_time": "20:00"}})
        state = monitor.execution_runtime.module_status("personal_monitor")
        assert not state["enabled"]
        assert not monitor._tasks.started
        assert not monitor.execution_runtime._timers
        assert monitor._tasks._config["scheduler"]["run_time"] == "20:00"
        monitor.execution_runtime.enable("personal_monitor")
        assert monitor._tasks.started
        assert all(
            entry[3] == state["generation"]
            for entry in monitor.execution_runtime._timers.values()
        )
    finally:
        monitor.stop()


def test_only_shared_runtime_threads_exist_and_start_is_idempotent(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.start()
    version = monitor.execution_runtime.module_status("personal_monitor")
    try:
        monitor.start()
        assert monitor.execution_runtime.module_status("personal_monitor") == version
        assert all(
            not thread.name.startswith("personal-monitor-")
            for thread in monitor.threads
        )
    finally:
        monitor.stop()


def test_ordinary_host_restart_preserves_enabled_personal_module(tmp_path):
    first = make_monitor(tmp_path)
    first.start()
    first.stop(disable=False)
    assert first.execution_runtime.module_status("personal_monitor")["enabled"]
    restored = make_monitor(tmp_path)
    restored.start()
    try:
        assert restored._tasks.started
        assert restored.health()["state"] == "running"
        assert "personal_monitor:reconcile" in restored.execution_runtime._timers
    finally:
        restored.stop(disable=False)


def test_explicit_disable_survives_host_restart_until_public_enable(tmp_path):
    first = make_monitor(tmp_path)
    first.start()
    first.execution_runtime.disable("personal_monitor")
    first.stop(disable=False)
    restored = make_monitor(tmp_path)
    restored.start()
    try:
        assert not restored._tasks.started
        assert restored.health()["state"] == "disabled"
        assert not restored.execution_runtime._timers
        restored.execution_runtime.enable("personal_monitor")
        assert restored._tasks.started
        assert "personal_monitor:reconcile" in restored.execution_runtime._timers
    finally:
        restored.stop(disable=False)
