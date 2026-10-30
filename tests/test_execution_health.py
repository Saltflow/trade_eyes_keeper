"""Durable health notices, using local callbacks and isolated SQLite only."""

import json
import sqlite3
import threading
import time

import pytest

from src.core.execution import (
    ExecutionRuntime,
    ExecutionStore,
    JobSpec,
    ModuleSpec,
    RunResult,
)
from src.core.execution_health import JOB, MODULE, HealthReporter


@pytest.fixture
def runtime(tmp_path):
    value = ExecutionRuntime(ExecutionStore(tmp_path / "runtime.sqlite"))
    value.register(
        ModuleSpec("business", (JobSpec("business.run", lambda p, c: RunResult()),))
    )
    yield value
    value.stop(timeout=3)


def completed(runtime, status="failed", reason="test failure"):
    runtime.store.owner_started("fixture")
    run = runtime.submit("business.run", {}, source="test")
    claim = runtime.store.claim("fixture", "default", ["business.run"])
    assert claim["id"] == run
    assert runtime.store.finish(run, claim["claim_token"], RunResult(status, reason))
    return run


def events(runtime):
    with runtime.store.connection() as db:
        result = []
        for row in db.execute(
            "SELECT * FROM execution_health_outbox ORDER BY created,id"
        ):
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            item["result"] = json.loads(item["result"])
            result.append(item)
        return result


def eventually(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("condition did not become true")


@pytest.mark.parametrize("status", ["failed", "partial", "unknown"])
def test_terminal_failure_is_persisted_once_and_unconfigured_never_succeeds(
    runtime, status
):
    run = completed(runtime, status)
    reporter = HealthReporter(runtime)
    reporter.observe({})
    reporter.observe({})
    result = events(runtime)
    assert len(result) == 1
    assert result[0]["event_key"] == "run:" + run
    assert result[0]["state"] == "route_unconfigured"
    assert reporter.health()["route_unconfigured"]
    assert reporter.health()["sent"] == 0
    assert not runtime.store.list_runs(job_id=JOB)


@pytest.mark.parametrize("status", ["succeeded", "skipped"])
def test_success_or_policy_skip_does_not_create_alert(runtime, status):
    completed(runtime, status)
    reporter = HealthReporter(runtime)
    reporter.observe({})
    assert events(runtime) == []
    assert reporter.health()["pending"] == 0


def test_http_intent_is_committed_before_callback_and_reply_is_read_back(runtime):
    completed(runtime)
    received = []

    def send(event):
        observed = events(runtime)
        assert observed[0]["state"] == "submitted"
        assert observed[0]["attempts"] == 1
        core_run = runtime.store.get(observed[0]["run_id"])
        assert core_run["side_effect"] == 1
        received.append(event)
        return {
            "outcome": "sent",
            "message_id": "platform-message",
            "secret": "drop-me",
        }

    reporter = HealthReporter(runtime, send)
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["sent"] == 1)
    assert len(received) == 1
    assert events(runtime)[0]["result"] == {
        "outcome": "sent",
        "message_id": "platform-message",
    }
    reporter.observe({})
    assert len(events(runtime)) == 1


def test_result_older_than_cursor_is_detected_when_it_finishes_after_restart(runtime):
    runtime.store.owner_started("fixture")
    old = runtime.submit("business.run", {})
    claim = runtime.store.claim("fixture", "default", ["business.run"])
    newer = runtime.submit("business.run", {})
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE execution_runs SET status='succeeded',finished=? WHERE id=?",
            (time.time(), newer),
        )
    reporter = HealthReporter(runtime)
    reporter.observe({})
    assert not events(runtime)
    runtime.store.finish(
        old, claim["claim_token"], RunResult("unknown", "owner_offline")
    )
    # No active run is needed: persisted old failures must still produce notices.
    reporter.observe({"active": [], "running": True, "failure": ""})
    assert [e["payload"]["run_id"] for e in events(runtime)] == [old]


def test_real_owner_offline_interrupted_conversation_is_not_silent(runtime):
    runtime.store.owner_started("gone")
    run = runtime.submit("business.run", {}, session_key="chat:user")
    runtime.store.claim("gone", "default", ["business.run"])
    runtime.store.owner_stopped("gone")
    assert runtime.store.recover_offline() == 1
    reporter = HealthReporter(runtime)
    reporter.observe({"running": True, "active": []})
    alert = events(runtime)[0]["payload"]
    assert alert["run_id"] == run
    assert alert["status"] == "unknown"
    assert alert["reason"] == "owner_offline"


def test_known_failed_delivery_retries_with_bounded_attempts(runtime):
    completed(runtime)
    calls = []

    def send(event):
        calls.append(event)
        return {"outcome": "failed", "reason": "rejected_before_send"}

    reporter = HealthReporter(runtime, send, retry_seconds=0.02, max_attempts=3)
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["exhausted"] == 1)
    assert len(calls) == 3
    assert events(runtime)[0]["attempts"] == 3
    assert reporter.health()["pending"] == 0


def test_failed_then_sent_stops_retrying(runtime):
    completed(runtime)
    calls = []

    def send(event):
        calls.append(event)
        return "failed" if len(calls) == 1 else "sent"

    reporter = HealthReporter(runtime, send, retry_seconds=0.02)
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["sent"] == 1)
    assert len(calls) == 2


@pytest.mark.parametrize("outcome", ["unknown", None, True, {"status": "success"}])
def test_unknown_or_missing_receipt_never_automatically_retries(runtime, outcome):
    completed(runtime)
    calls = []

    def send(event):
        calls.append(event)
        return outcome

    reporter = HealthReporter(runtime, send, retry_seconds=0.01)
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["unknown"] == 1)
    reporter.observe({})
    assert len(calls) == 1
    assert events(runtime)[0]["attempts"] == 1


def test_http_exception_is_unknown_and_exception_text_is_not_persisted(runtime):
    completed(runtime)

    def send(event):
        raise TimeoutError("token=private")

    reporter = HealthReporter(runtime, send)
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["unknown"] == 1)
    assert "private" not in str(events(runtime))
    assert events(runtime)[0]["result"]["reason"] == "TimeoutError"


def test_health_delivery_failure_does_not_recursively_generate_its_own_alert(runtime):
    completed(runtime)
    reporter = HealthReporter(runtime, lambda event: "unknown")
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["unknown"] == 1)
    eventually(
        lambda: any(
            r["status"] == "partial" for r in runtime.store.list_runs(job_id=JOB)
        )
    )
    reporter.observe({})
    assert len(events(runtime)) == 1


def test_data_readiness_is_separate_from_runtime_failure_and_issue_is_once_per_episode(
    runtime,
):
    reporter = HealthReporter(runtime)
    issue = {
        "id": "market.missing_quote",
        "module_id": "personal_monitor",
        "kind": "data_not_ready",
        "reason": "quote unavailable",
    }
    reporter.observe({}, [issue])
    reporter.observe({}, [issue])
    assert len(events(runtime)) == 1
    assert reporter.health()["data_not_ready"][0]["kind"] == "data_not_ready"
    reporter.observe({})  # None preserves external issue rather than resolving it.
    assert len(reporter.health()["issues"]) == 1
    reporter.observe({}, [])
    assert not reporter.health()["issues"]
    old, recovered = events(runtime)
    assert old["state"] == "superseded"
    assert recovered["payload"]["kind"] == "issue_recovered"
    assert recovered["payload"]["previous_delivery"] == "route_unconfigured"
    reporter.observe({}, [issue])
    assert len(events(runtime)) == 3
    assert events(runtime)[-1]["payload"]["episode"] == 2


def test_recovery_supersedes_failed_down_before_retry(runtime):
    reporter = HealthReporter(runtime, lambda event: "failed", retry_seconds=60)
    reporter.observe({}, [{"id": "receiver", "reason": "offline"}])
    runtime.start()
    eventually(lambda: reporter.health()["failed"] == 1)
    reporter.observe({}, [])
    result = events(runtime)
    assert result[0]["state"] == "superseded"
    assert result[1]["payload"]["kind"] == "issue_recovered"


def test_dispatcher_failure_is_a_separate_issue_and_clears_on_recovery(runtime):
    reporter = HealthReporter(runtime)
    reporter.observe({"failure": "dispatcher_failed:OSError"})
    reporter.observe({"failure": "dispatcher_failed:OSError"})
    assert len(events(runtime)) == 1
    assert reporter.health()["issues"][0]["kind"] == "runtime_failure"
    reporter.observe({"failure": ""})
    assert len(events(runtime)) == 2
    assert not reporter.health()["issues"]


def test_idle_observation_has_no_new_runs_or_durable_writes(runtime):
    reporter = HealthReporter(runtime)
    reporter.observe({"failure": ""}, [])
    reader = sqlite3.connect(runtime.store.path)
    try:
        before = reader.execute("PRAGMA data_version").fetchone()[0]
        for _ in range(4):
            reporter.observe({"failure": ""}, [])
        after = reader.execute("PRAGMA data_version").fetchone()[0]
    finally:
        reader.close()
    assert before == after
    assert runtime.store.list_runs(job_id=JOB) == []


def test_stop_preserves_queue_and_start_resumes_delivery(runtime):
    completed(runtime)
    calls = []
    reporter = HealthReporter(runtime, lambda event: calls.append(event) or "sent")
    reporter.observe({})
    reporter.stop()
    runtime.start()
    assert reporter.health()["stopped"]
    assert not calls
    assert events(runtime)[0]["state"] == "queued"
    reporter.start()
    eventually(lambda: reporter.health()["sent"] == 1)
    assert len(calls) == 1


def test_later_configured_sender_flushes_persisted_unconfigured_alert(runtime):
    completed(runtime)
    reporter = HealthReporter(runtime)
    reporter.observe({})
    calls = []
    reporter.set_sender(lambda event: calls.append(event) or "sent")
    runtime.start()
    eventually(lambda: reporter.health()["sent"] == 1)
    assert len(calls) == 1


def test_startup_sender_can_flush_previous_unconfigured_alert(runtime):
    completed(runtime)
    previous = HealthReporter(runtime)
    previous.observe({})
    previous.stop()
    calls = []
    current = HealthReporter(runtime, lambda event: calls.append(event) or "sent")
    runtime.start()
    eventually(lambda: current.health()["sent"] == 1)
    assert len(calls) == 1


def submitted_intent(runtime, *, owner="missing", run_id=None):
    completed(runtime)
    reporter = HealthReporter(runtime)
    reporter.observe({})
    reporter.stop()
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE execution_health_outbox SET state='submitted',owner_id=?,"
            "run_id=?,token='old-intent',attempts=1",
            (owner, run_id),
        )
    return reporter


def test_unproven_owner_is_not_cleared_without_exclusive_startup(runtime):
    submitted_intent(runtime)
    current = HealthReporter(runtime, lambda event: "sent")
    assert current.health()["sending"] == 1
    assert current.health()["unknown"] == 0


def test_explicit_exclusive_startup_recovers_submitted_to_unknown_never_retry(runtime):
    submitted_intent(runtime)
    calls = []
    current = HealthReporter(
        runtime, lambda event: calls.append(event) or "sent", exclusive=True
    )
    runtime.start()
    current.observe({})
    assert current.health()["unknown"] == 1
    assert current.health()["pending"] == 0
    assert not calls


def test_proven_offline_owner_recovers_without_exclusive_flag(runtime):
    runtime.store.owner_started("old")
    runtime.store.owner_stopped("old")
    submitted_intent(runtime, owner="old")
    current = HealthReporter(runtime)
    assert current.health()["unknown"] == 1


def test_active_sender_is_not_invalidated_even_with_exclusive_host_flag(runtime):
    completed(runtime)
    entered, release = threading.Event(), threading.Event()

    def sender(event):
        entered.set()
        release.wait(3)
        return "sent"

    first = HealthReporter(runtime, sender)
    first.observe({})
    runtime.start()
    try:
        assert entered.wait(3)
        second = HealthReporter(runtime, sender, exclusive=True)
        assert second.health()["sending"] == 1
        assert second.health()["unknown"] == 0
    finally:
        release.set()
    eventually(lambda: first.health()["sent"] == 1)


def test_reserved_control_lane_does_not_use_conversation_capacity(runtime):
    HealthReporter(runtime)
    with runtime.store.connection() as db:
        row = db.execute(
            "SELECT lane FROM execution_jobs WHERE id=?", (JOB,)
        ).fetchone()
    assert row["lane"] == "control"
    assert runtime.module_status(MODULE)["enabled"]
    assert not runtime.running


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
def test_invalid_retry_delay_is_rejected(runtime, bad):
    with pytest.raises(ValueError):
        HealthReporter(runtime, retry_seconds=bad)


def test_explicit_disabled_module_stays_disabled_across_reporter_reconstruction(
    runtime,
):
    completed(runtime)
    first = HealthReporter(runtime)
    first.observe({})
    first.stop(disable=True)
    calls = []
    second = HealthReporter(runtime, lambda event: calls.append(event) or "sent")
    assert second.health()["stopped"]
    assert not runtime.module_status(MODULE)["enabled"]
    runtime.start()
    assert not calls
    second.start()
    eventually(lambda: second.health()["sent"] == 1)
    assert len(calls) == 1


def test_local_cleanup_does_not_persistently_disable_health_module(runtime):
    reporter = HealthReporter(runtime)
    reporter.stop()
    assert runtime.module_status(MODULE)["enabled"]
    assert reporter.health()["stopped"]
    replacement = HealthReporter(runtime)
    assert not replacement.health()["stopped"]


def test_platform_message_id_receipt_has_proof_compatible_alias(runtime):
    completed(runtime)
    reporter = HealthReporter(
        runtime,
        lambda event: {"outcome": "sent", "platform_message_id": "om_test_platform"},
    )
    reporter.observe({})
    runtime.start()
    eventually(lambda: reporter.health()["sent"] == 1)
    assert events(runtime)[0]["result"]["message_id"] == "om_test_platform"
    assert events(runtime)[0]["result"]["platform_message_id"] == "om_test_platform"


def test_invalid_sender_fails_before_creating_health_tables(runtime):
    with pytest.raises(TypeError):
        HealthReporter(runtime, send_callback=True)
    with runtime.store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='execution_health_outbox'"
        ).fetchone()
