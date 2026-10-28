"""Bounded delivery batches keep the original per-outbox durable boundary."""

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from src.personal_monitor_runtime import PersonalMonitorRuntime


class Context:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.mark_side_effect = Mock(return_value=True)


def prepared(tmp_path, count=9, callback=None):
    config = {"scheduler": {"run_time": "23:59", "daily_misfire_grace_seconds": 1}}
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "execution.sqlite3"))
    store = MonitorStore(tmp_path / "facts.sqlite3")
    callback = callback or Mock(return_value={"outcome": "sent"})
    monitor = PersonalMonitorRuntime(
        store,
        config,
        callback,
        execution_runtime=execution,
        observation_provider=Mock(),
    )
    store.register_route("owner", "local-capture")
    now = datetime.now(timezone.utc)
    for number in range(count):
        store.create_reminder(
            "owner",
            str(number),
            now - timedelta(seconds=1),
            f"item-{number}",
            "local-capture",
            now=now - timedelta(seconds=2),
        )
    monitor.engine.enqueue_due(now)
    task = monitor._registered_tasks()
    task.apply_config(config)
    task.started = True
    task._finished = Mock()
    return monitor, task, execution, callback


def rows(monitor):
    with monitor.store.connection() as db:
        return [
            dict(r) for r in db.execute("SELECT * FROM outbox ORDER BY created_at,id")
        ]


def receipt_items(result):
    if not result.deliveries:
        return []
    return (
        result.deliveries
        if isinstance(result.deliveries, list)
        else [result.deliveries]
    )


def test_ready_batch_is_bounded_and_each_outbox_has_one_actual_receipt(
    tmp_path, monkeypatch
):
    from src.personal_monitor import tasks

    # Exercise the count bound independently of real disk/worker scheduling;
    # the elapsed budget has its own deterministic test below.
    monkeypatch.setattr(tasks, "monotonic", lambda: 0.0)
    monitor, task, _, send = prepared(tmp_path)
    result = task._delivery({}, Context())
    assert send.call_count == 8
    assert len(receipt_items(result)) == 8
    assert [r["state"] for r in rows(monitor)].count("sent") == 8
    assert [r["state"] for r in rows(monitor)].count("queued") == 1
    result = task._delivery({}, Context())
    assert len(receipt_items(result)) == 1
    assert send.call_count == 9
    assert all(r["state"] == "sent" and r["attempts"] == 1 for r in rows(monitor))
    assert task._finished.call_count == 2


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_non_success_stops_batch_and_preserves_earlier_sent_receipt(tmp_path, outcome):
    send = Mock(side_effect=[{"outcome": "sent"}, {"outcome": outcome}])
    monitor, task, _, _ = prepared(tmp_path, callback=send)
    result = task._delivery({}, Context())
    assert send.call_count == 2
    assert result.status == outcome
    assert [r["outcome"] for r in receipt_items(result)] == ["sent", outcome]
    states = [r["state"] for r in rows(monitor)]
    assert states.count("sent") == 1
    assert states.count(outcome) == 1
    assert states.count("queued") == 7
    assert sum(r["attempts"] for r in rows(monitor)) == 2


def test_cancelled_before_batch_does_not_claim_or_send(tmp_path):
    monitor, task, _, send = prepared(tmp_path)
    context = Context()
    context.cancel_event.set()
    task._delivery({}, context)
    send.assert_not_called()
    assert all(r["state"] == "queued" and r["attempts"] == 0 for r in rows(monitor))


def test_second_side_effect_authorization_rejected_before_submission(tmp_path):
    monitor, task, _, send = prepared(tmp_path)
    context = Context()
    context.mark_side_effect.side_effect = [True, False]
    submitted = monitor.engine.mark_submitted
    monitor.engine.mark_submitted = Mock(side_effect=submitted)
    result = task._delivery({}, context)
    assert send.call_count == 1
    assert monitor.engine.mark_submitted.call_count == 1
    assert receipt_items(result)[-1]["outcome"] == "not_sent"
    assert [r["state"] for r in rows(monitor)].count("sent") == 1
    assert [r["state"] for r in rows(monitor)].count("queued") == 8
    assert not any(
        r["state"] in {"claimed", "submitted", "unknown"} for r in rows(monitor)
    )
    released = next(
        r for r in rows(monitor) if r["state"] == "queued" and r["attempts"] == 1
    )
    assert released["token"] is None and released["claimed_at"] is None
    assert json.loads(released["metadata"])["send_attempted"] is False


def test_cancel_after_second_submission_never_reaches_second_http(tmp_path):
    monitor, task, _, send = prepared(tmp_path)
    context = Context()
    original = monitor.engine.mark_submitted
    calls = []

    def submitted(*args, **kwargs):
        accepted = original(*args, **kwargs)
        calls.append(accepted)
        if len(calls) == 2:
            context.cancel_event.set()
        return accepted

    monitor.engine.mark_submitted = submitted
    result = task._delivery({}, context)
    assert calls == [True, True]
    assert send.call_count == 1
    assert result.status == "unknown"
    states = [r["state"] for r in rows(monitor)]
    assert states.count("sent") == 1 and states.count("unknown") == 1
    assert states.count("queued") == 7
    stopped = next(r for r in rows(monitor) if r["state"] == "unknown")
    assert json.loads(stopped["metadata"])["send_attempted"] is False


def test_batch_yields_on_elapsed_budget(tmp_path, monkeypatch):
    from src.personal_monitor import tasks

    monitor, task, _, send = prepared(tmp_path)
    monkeypatch.setattr(tasks, "monotonic", Mock(side_effect=[10.0, 16.0]))
    result = task._delivery({}, Context())
    assert send.call_count == 1
    assert len(receipt_items(result)) == 1
    assert sum(r["state"] == "queued" for r in rows(monitor)) == 8


def test_hot_disable_drains_current_send_and_does_not_block_another_lane(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def send(*_):
        entered.set()
        assert release.wait(5)
        return {"outcome": "sent"}

    monitor, task, execution, _ = prepared(tmp_path, count=3, callback=send)
    execution.register(
        ModuleSpec(
            "other", (JobSpec("other.probe", lambda *_: RunResult(), lane="other"),)
        )
    )
    execution.start()
    try:
        run = execution.submit(
            "personal.delivery",
            module_id="personal_monitor",
            config_version=task._version,
        )
        assert entered.wait(5)
        probe = execution.wait(execution.submit("other.probe"), timeout=3)
        assert probe["status"] == "succeeded"
        assert execution.disable("personal_monitor")["state"] == "draining"
        release.set()
        receipt = execution.wait(run, timeout=5)
        assert receipt["released_at"]
        assert len(receipt_items(RunResult.from_dict(receipt["result"]))) == 1
        states = [r["state"] for r in rows(monitor)]
        assert states.count("sent") == 1 and states.count("queued") == 2
    finally:
        release.set()
        execution.stop()


def test_process_death_after_second_submission_preserves_sent_and_unstarted(tmp_path):
    script = tmp_path / "die.py"
    script.write_text(
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from tests.personal_monitor.test_delivery_batches import prepared\n"
        "counter=[0]\n"
        "def send(*args):\n"
        " counter[0]+=1\n"
        " if counter[0]==2: os._exit(23)\n"
        " return {'outcome':'sent'}\n"
        "monitor,task,execution,_=prepared(Path(sys.argv[2]),count=5,callback=send)\n"
        "execution.start()\n"
        "execution.submit('personal.delivery',module_id='personal_monitor',config_version=task._version)\n"
        "time.sleep(15)\n"
        "raise RuntimeError('expected injected process death')\n",
        encoding="utf8",
    )
    repo = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), str(repo), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 23, completed.stdout + completed.stderr
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "execution.sqlite3"))
    monitor = PersonalMonitorRuntime(
        MonitorStore(tmp_path / "facts.sqlite3"),
        {"scheduler": {"run_time": "23:59"}},
        Mock(return_value={"outcome": "sent"}),
        execution_runtime=execution,
    )
    old = execution.store.list_runs()[0]
    assert old["status"] == "running" and old["side_effect"] == 1
    states = [r["state"] for r in rows(monitor)]
    assert (
        states.count("sent") == 1
        and states.count("submitted") == 1
        and states.count("queued") == 3
    )
    task = monitor._registered_tasks()
    task.apply_config(monitor.config)
    task.started = True
    task._finished = Mock()
    execution.start()
    try:
        assert execution.store.get(old["id"])["status"] == "unknown"
        task._delivery({}, Context())
        assert monitor.send_callback.call_count == 3
        assert (
            monitor.engine.claim_delivery(
                "recover", datetime.now(timezone.utc) + timedelta(seconds=301)
            )
            is None
        )
        states = [r["state"] for r in rows(monitor)]
        assert states.count("sent") == 4 and states.count("unknown") == 1
        assert all(r["attempts"] == 1 for r in rows(monitor))
    finally:
        execution.stop()


def test_cancel_after_claim_releases_only_unsubmitted_item(tmp_path):
    monitor, _, _, send = prepared(tmp_path, count=2)
    context = Context()
    original = monitor.engine.claim_delivery

    def claimed(*args, **kwargs):
        result = original(*args, **kwargs)
        context.cancel_event.set()
        return result

    monitor.engine.claim_delivery = claimed
    result = monitor.deliver_one(datetime.now(timezone.utc), context=context)
    assert result["outcome"] == "not_sent"
    send.assert_not_called()
    context.mark_side_effect.assert_not_called()
    assert all(r["state"] == "queued" for r in rows(monitor))
    assert sum(r["attempts"] for r in rows(monitor)) == 1


def test_release_claim_is_token_bound_once_and_cannot_release_submitted(tmp_path):
    monitor, _, _, _ = prepared(tmp_path, count=2)
    now = datetime.now(timezone.utc)
    item = monitor.engine.claim_delivery("local", now)
    assert not monitor.engine.release_unsubmitted(item["id"], "wrong")
    assert monitor.engine.release_unsubmitted(item["id"], item["token"])
    assert not monitor.engine.release_unsubmitted(item["id"], item["token"])
    again = monitor.engine.claim_delivery("local", now)
    assert monitor.engine.mark_submitted(again["id"], again["token"], now=now)
    assert not monitor.engine.release_unsubmitted(again["id"], again["token"])
    assert (
        next(r for r in rows(monitor) if r["id"] == again["id"])["state"] == "submitted"
    )


def test_concurrent_user_cancel_is_never_revived_by_release(tmp_path):
    monitor, _, _, send = prepared(tmp_path, count=2)
    context = Context()
    original = monitor.engine.claim_delivery

    def claimed(*args, **kwargs):
        result = original(*args, **kwargs)
        monitor.store.cancel_reminder("owner", result["payload"]["reminder_id"], 1)
        context.cancel_event.set()
        return result

    monitor.engine.claim_delivery = claimed
    result = monitor.deliver_one(datetime.now(timezone.utc), context=context)
    send.assert_not_called()
    assert result["outcome"] == "not_sent"
    assert sum(r["state"] == "cancelled" for r in rows(monitor)) == 1
    assert sum(r["state"] == "queued" for r in rows(monitor)) == 1


def test_disable_reenable_recovers_known_unsubmitted_claim_without_duplicate(tmp_path):
    monitor, task, execution, send = prepared(tmp_path, count=2)
    context = Context()
    original = monitor.engine.claim_delivery
    once = [False]

    def claimed(*args, **kwargs):
        result = original(*args, **kwargs)
        if not once[0]:
            once[0] = True
            execution.disable("personal_monitor")
        return result

    monitor.engine.claim_delivery = claimed
    monitor.deliver_one(datetime.now(timezone.utc), context=context)
    assert not task.started
    send.assert_not_called()
    assert all(r["state"] == "queued" for r in rows(monitor))
    execution.enable("personal_monitor")
    assert task.started
    task._delivery({}, Context())
    assert send.call_count == 2
    assert all(r["state"] == "sent" for r in rows(monitor))
    assert sorted(r["attempts"] for r in rows(monitor)) == [1, 2]


def test_http_exception_on_second_item_is_unknown_and_stops_remaining(tmp_path):
    send = Mock(side_effect=[{"outcome": "sent"}, TimeoutError("http response lost")])
    monitor, task, _, _ = prepared(tmp_path, callback=send)
    result = task._delivery({}, Context())
    assert result.status == "unknown"
    assert send.call_count == 2
    assert [r["outcome"] for r in receipt_items(result)] == ["sent", "unknown"]
    states = [r["state"] for r in rows(monitor)]
    assert states.count("sent") == 1 and states.count("unknown") == 1
    assert states.count("queued") == 7


def test_executor_total_timeout_keeps_hold_and_stops_before_next_batch_item(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def send(*_):
        entered.set()
        assert release.wait(15)
        return {"outcome": "sent"}

    monitor, task, execution, _ = prepared(tmp_path, count=3, callback=send)
    execution.register(
        ModuleSpec(
            "batch-budget",
            (
                JobSpec(
                    "batch.delivery",
                    task._delivery,
                    lane="personal_delivery",
                    timeout_seconds=5,
                ),
            ),
        )
    )
    execution.start()
    try:
        run = execution.submit("batch.delivery")
        assert entered.wait(10)
        result = execution.wait(run, timeout=8)
        assert result["status"] == "unknown" and result["released_at"] is None
        release.set()
        deadline = time.monotonic() + 5
        while (
            execution.store.get(run)["released_at"] is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        result = execution.store.get(run)
        assert result["status"] == "unknown" and result["released_at"]
        states = [r["state"] for r in rows(monitor)]
        assert states.count("sent") == 1 and states.count("queued") == 2
        assert sum(r["attempts"] for r in rows(monitor)) == 1
    finally:
        release.set()
        execution.stop()
