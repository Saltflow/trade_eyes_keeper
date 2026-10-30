"""Real ownership, admission and recovery boundaries; no external traffic."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import run_personal_monitor_feishu_group_test as group
from scripts.personal_monitor_process import FileLease
from scripts.personal_monitor_shared_test import (
    AdmissionPaused,
    SharedPersonalTestBridge,
)
from scripts.personal_monitor_transport import validate_profile_binding
from src.core.execution import (
    ExecutionRuntime,
    ExecutionStore,
    JobSpec,
    ModuleSpec,
    RunResult,
)
from src.core.execution_health import HealthReporter
from src.core.process_lock import exclusive_process_lock
from src.core.runtime_service import (
    RuntimeService,
    read_service_status,
    request_service_stop,
)
from src.personal_monitor import MonitorStore


class Receiver:
    def __init__(self, trace=None, pause=None, outcome="sent"):
        self.trace = trace if trace is not None else []
        self.closed = threading.Event()
        self.entered = threading.Event()
        self.paused = False
        self.on_pause = pause
        self.assistant = None
        self.sent = []
        self.outcome = outcome
        self.app = SimpleNamespace(
            allowed_chat_ids={"fixture-chat"},
            assistant_enabled=False,
            gate=SimpleNamespace(is_allowed=lambda route: route == "fixture-chat"),
            send_personal_message=self.send,
        )

    def send(self, route, text):
        self.sent.append((route, text))
        return {"outcome": self.outcome, "platform_message_id": "fixture-receipt"}

    def validate_config(self):
        self.trace.append("validate")

    def run(self):
        self.entered.set()
        self.closed.wait()

    def health(self):
        return {"ready": not self.paused}

    def pause_admission(self):
        self.trace.append("pause")
        self.paused = True
        if self.on_pause:
            self.on_pause()

    def stop(self):
        self.trace.append("transport_stop")
        self.closed.set()


def configuration(path):
    return {"storage": {"data_dir": str(path)}, "interactive": {}}


def host_for(tmp_path, receiver=None, execution=None, **kwargs):
    execution = execution or ExecutionRuntime(
        ExecutionStore(tmp_path / "profile.sqlite3")
    )
    receiver = receiver or Receiver()
    host = RuntimeService(
        configuration(tmp_path / "status"),
        scheduling=False,
        execution_runtime=execution,
        bot_factory=lambda _config, _runtime: {"feishu": receiver},
        register_reports=False,
        **kwargs,
    )
    return host, execution, receiver


def test_factory_runs_inside_exact_database_lock_without_starting_constructor(tmp_path):
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "test.sqlite3"))
    receiver = Receiver()
    calls = []

    def factory(config, supplied):
        assert supplied is execution and not execution.running
        with exclusive_process_lock(tmp_path / ".service.lock") as acquired:
            assert not acquired
        calls.append(config)
        return {"feishu": receiver}

    host = RuntimeService(
        configuration(tmp_path / "different-status"),
        scheduling=False,
        execution_runtime=execution,
        bot_factory=factory,
        register_reports=False,
    )
    assert not calls and not execution.running
    host.run(on_started=host.stop)
    assert len(calls) == 1
    with execution.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM execution_owners").fetchone()[0] == 1
        assert not db.execute(
            "SELECT 1 FROM execution_modules WHERE id='reports'"
        ).fetchone()


def test_same_database_different_status_paths_have_only_one_factory(tmp_path):
    database = tmp_path / "original.sqlite3"
    first, execution, receiver = host_for(
        tmp_path / "first", execution=ExecutionRuntime(ExecutionStore(database))
    )
    entered, release = threading.Event(), threading.Event()
    errors = []

    def started():
        entered.set()
        release.wait(5)
        first.stop()

    def run():
        try:
            first.run(on_started=started)
        except BaseException as exc:  # noqa: BLE001 -- return child-thread failures to the assertion
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3)
    factory = Mock()
    other = RuntimeService(
        configuration(tmp_path / "second"),
        scheduling=False,
        execution_runtime=ExecutionRuntime(ExecutionStore(database)),
        bot_factory=factory,
        register_reports=False,
    )
    try:
        with pytest.raises(RuntimeError, match="already running"):
            other.run()
        factory.assert_not_called()
        assert not other.status_path.exists()
        assert execution.running
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors and receiver.closed.is_set()


def test_live_execution_owner_rejects_before_factory_and_is_not_stopped(tmp_path):
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "original.sqlite3"))
    execution.start()
    other = ExecutionRuntime(ExecutionStore(execution.store.path))
    factory = Mock()
    host = RuntimeService(
        configuration(tmp_path / "status"),
        scheduling=False,
        execution_runtime=other,
        bot_factory=factory,
        register_reports=False,
    )
    try:
        with pytest.raises(RuntimeError, match="execution owner"):
            host.run()
        factory.assert_not_called()
        assert execution.running and execution.store.has_live_owner()
        assert not host.status_path.exists()
        with execution.store.connection() as db:
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='execution_health_outbox'"
            ).fetchone()
    finally:
        execution.stop()


def test_default_bot_rejects_half_bound_database_before_factory(tmp_path):
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "original.sqlite3"))
    with pytest.raises(ValueError, match="share configured SQLite"):
        RuntimeService(configuration(tmp_path / "wrong"), execution_runtime=execution)
    assert not execution.running


def test_pause_is_before_real_handler_drain_and_transport_close(tmp_path):
    trace, release, entered = [], threading.Event(), threading.Event()
    receiver = Receiver(trace, pause=release.set)
    host, execution, _ = host_for(tmp_path, receiver)

    def work(_payload, _context):
        entered.set()
        assert release.wait(3)
        assert not receiver.closed.is_set()
        trace.append("handler_finished")
        return RunResult("succeeded", "actual_completion")

    execution.register(ModuleSpec("fixture", (JobSpec("fixture.work", work),)))
    original = execution.stop

    def stop(**kwargs):
        original(**kwargs)
        trace.append("execution_stopped")

    execution.stop = stop

    def started():
        execution.submit("fixture.work")
        assert entered.wait(3)
        host.stop()

    host.run(on_started=started)
    assert (
        trace.index("pause")
        < trace.index("handler_finished")
        < trace.index("execution_stopped")
        < trace.index("transport_stop")
    )
    assert not receiver.sent


@pytest.mark.parametrize("failing", ["execution", "health", "pause", "status"])
def test_cleanup_failure_keeps_original_exception_and_reaps_receivers(
    tmp_path, monkeypatch, failing
):
    host, execution, receiver = host_for(tmp_path)
    original_stop = execution.stop

    def started():
        if failing == "execution":

            def stop(**kwargs):
                original_stop(**kwargs)
                raise sqlite3.OperationalError("private ignored body")

            monkeypatch.setattr(execution, "stop", stop)
        elif failing == "health":
            monkeypatch.setattr(
                host.health_reporter,
                "stop",
                Mock(side_effect=OSError("private ignored body")),
            )
        elif failing == "pause":
            receiver.pause_admission = Mock(side_effect=OSError("private ignored body"))
        else:
            monkeypatch.setattr(
                host,
                "_write_status",
                Mock(side_effect=PermissionError("private ignored body")),
            )
        raise ValueError("original callback failure")

    try:
        with pytest.raises(ValueError, match="original callback failure"):
            host.run(on_started=started)
        assert receiver.closed.is_set()
        assert all(not thread.is_alive() for thread in host.threads.values())
        assert any(
            d["exception_class"] in {"OSError", "PermissionError", "OperationalError"}
            for d in host._diagnostics
        )
        assert "private ignored body" not in json.dumps(host._diagnostics)
    finally:
        original_stop()


def test_failure_after_owner_commit_does_not_leave_a_live_owner(tmp_path, monkeypatch):
    host, execution, receiver = host_for(tmp_path)
    start = execution.start_if_unowned

    def broken_start():
        assert start()
        raise OSError("after commit")

    monkeypatch.setattr(execution, "start_if_unowned", broken_start)
    with pytest.raises(OSError, match="after commit"):
        host.run()
    assert not execution.running and not execution.store.has_live_owner()
    assert receiver.closed.is_set()


@pytest.mark.parametrize(
    "snapshot",
    [
        {"ready": False, "issues": []},
        {"ready": True, "state": "degraded", "issues": []},
        {"ready": True, "degraded": True, "issues": []},
        {"ready": True, "issues": [{"id": "transport.explicit", "reason": "fixture"}]},
    ],
)
def test_transport_false_without_issues_is_not_ready_and_planned_stop_does_not_notify(
    tmp_path,
    snapshot,
):
    host, execution, receiver = host_for(tmp_path)
    receiver.health = lambda: snapshot

    def started():
        status = read_service_status(host.config)
        assert not status["ready"] and not status["transport_ready"]
        host.stop()

    # Keep the health module disabled: readiness must not depend on its delivery.
    execution.register(ModuleSpec("execution_health", (), enabled=False))
    host.run(on_started=started)
    assert not receiver.sent
    assert not execution.module_status("execution_health")["enabled"]


def test_stopped_health_never_opens_a_transport_outage(tmp_path):
    host, execution, receiver = host_for(tmp_path)
    host.run(on_started=host.stop)
    assert receiver.paused and not receiver.sent
    with execution.store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM execution_health_outbox").fetchone()[0]
            == 0
        )
    assert read_service_status(host.config)["state"] == "stopped"


def test_host_recovery_is_stable_unknown_and_does_not_resolve_other_issues(tmp_path):
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "original.sqlite3"))
    reporter = HealthReporter(execution, lambda event: {"outcome": "unknown"})
    reporter.observe(
        execution.health(), issues=[{"id": "module-still-bad", "reason": "kept"}]
    )
    reporter.stop()
    metadata = {
        "pid": 4123,
        "birth": "old-birth",
        "started_at": "2026-10-25T00:00:00+00:00",
    }
    reporter.record_host_recovery("old-owner", metadata)
    with execution.store.connection() as db:
        db.execute(
            "UPDATE execution_health_outbox SET state='unknown' WHERE event_key='host-recovery:old-owner'"
        )
        before = tuple(
            db.execute(
                "SELECT * FROM execution_health_outbox WHERE event_key='host-recovery:old-owner'"
            ).fetchone()
        )
    reporter.record_host_recovery("old-owner", metadata)
    with execution.store.connection() as db:
        after = tuple(
            db.execute(
                "SELECT * FROM execution_health_outbox WHERE event_key='host-recovery:old-owner'"
            ).fetchone()
        )
        assert before == after
        assert (
            db.execute(
                "SELECT state FROM execution_health_issues WHERE id='module-still-bad'"
            ).fetchone()[0]
            == "active"
        )
        assert not db.execute(
            "SELECT 1 FROM execution_health_outbox WHERE event_key LIKE 'issue:module-still-bad:%:up'"
        ).fetchone()
    execution.stop()


def test_previous_session_requires_exact_owner_evidence_and_ignores_planned_stop(
    tmp_path,
):
    host, execution, _receiver = host_for(tmp_path)
    old_id = "proved-dead-owner"
    with execution.store.connection() as db:
        db.execute(
            "INSERT INTO execution_owners VALUES(?,?,?,?,?,?)",
            (old_id, os.getpid(), "not-current-birth", "running", 1, 1),
        )
    host.status_path.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "state": "running",
        "execution": {"owner_id": old_id},
        "process_birth": "not-current-birth",
    }
    host.status_path.write_text(json.dumps(status), encoding="utf8")
    assert host._previous_session()["key"] == old_id
    status["planned_stop"] = True
    host.status_path.write_text(json.dumps(status), encoding="utf8")
    assert host._previous_session() is None


def test_original_scope_binding_rejects_new_app_without_schema_or_fact_changes(
    tmp_path,
):
    database = tmp_path / "test.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE test_application(app_id TEXT PRIMARY KEY); INSERT INTO test_application VALUES('original-app'); CREATE TABLE retained_fact(value TEXT); INSERT INTO retained_fact VALUES('original');"
        )
    before = database.read_bytes()
    with pytest.raises(ValueError, match="其他应用"):
        validate_profile_binding({"app_id": "new-app"}, database)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "corruption", ["list", "string", "null", "pid", "birth", "state"]
)
def test_invalid_previous_status_never_blocks_idle_start_or_fabricates_recovery(
    tmp_path, corruption
):
    host, execution, receiver = host_for(tmp_path)
    old_id = "dead-fixture-owner"
    with execution.store.connection() as db:
        db.execute(
            "INSERT INTO execution_owners VALUES(?,?,?,?,?,?)",
            (old_id, os.getpid(), "old-birth", "running", 1, 1),
        )
    status = {
        "pid": os.getpid(),
        "process_birth": "old-birth",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "state": "running",
        "execution": {"owner_id": old_id},
    }
    if corruption in {"list", "string", "null"}:
        status["execution"] = {
            "list": ["not-a-mapping"],
            "string": "invalid",
            "null": None,
        }[corruption]
    elif corruption == "pid":
        status["pid"] += 1
    elif corruption == "birth":
        status["process_birth"] = "other-birth"
    else:
        status["state"] = ["running"]
    host.status_path.parent.mkdir(parents=True, exist_ok=True)
    host.status_path.write_text(json.dumps(status), encoding="utf8")
    host.run(on_started=host.stop)
    assert not receiver.sent and not execution.running
    with execution.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM execution_health_outbox WHERE event_key LIKE 'host-recovery:%'"
            ).fetchone()[0]
            == 0
        )


def test_legacy_poller_lease_precedes_runtime_construction(tmp_path, monkeypatch):
    settings = {"workspace": tmp_path}
    monkeypatch.setattr(group, "validate_production_filter", Mock())
    constructor = Mock(side_effect=AssertionError("must not construct a store"))
    monkeypatch.setattr("src.core.execution.ExecutionStore", constructor)
    with FileLease(tmp_path / "poller.lock"), pytest.raises(
        RuntimeError, match="已运行"
    ):
        group.run_host(settings, proof_path="unused")
    constructor.assert_not_called()


def test_paused_durable_history_message_is_not_consumed(tmp_path):
    settings = {
        "workspace": tmp_path,
        "app_id": "fixture-app",
        "chat_id": "fixture-chat",
        "allowed_sender_ids": ["fixture-owner"],
    }
    state = group.GroupState(settings, started_ms=1000)
    bridge = object.__new__(SharedPersonalTestBridge)
    bridge._admission_paused, bridge._admission_lock = (
        threading.Event(),
        threading.RLock(),
    )
    bridge.pause_admission()
    item = {"message_id": "fixture-message", "create_time": "2000"}
    api = SimpleNamespace(collect_window=lambda _start, _until: [item])
    poller = group.GroupPoller(settings, api, state, bridge)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(group, "item_to_event", lambda *_args: {"authenticated": True})
        with pytest.raises(AdmissionPaused):
            poller.poll_once(6000)
    assert not state.seen("fixture-message")
    with sqlite3.connect(state.path) as db:
        assert (
            db.execute("SELECT completed_until_ms FROM group_binding").fetchone()[0]
            == 1000
        )


def profile_settings(tmp_path):
    return {
        "workspace": tmp_path,
        "app_id": "fixture-app",
        "app_secret": "fixture-secret",
        "api_key": "fixture-key",
        "chat_id": "fixture-chat",
        "allowed_chat_ids": ["fixture-chat"],
        "allowed_sender_ids": ["fixture-owner"],
        "market_check_times": ["09:50", "14:30", "19:00"],
        "poll_seconds": 5,
    }


def test_external_bridge_uses_same_database_without_runtime_or_health_ownership(
    tmp_path, monkeypatch
):
    import scripts.personal_monitor_shared_test as module

    settings = profile_settings(tmp_path)
    execution = ExecutionRuntime(ExecutionStore(tmp_path / "test.sqlite3"))
    business = MonitorStore(execution.store.path)
    business.ensure_account("fixture-owner", "fixture-account")
    for index in range(4):
        business.import_snapshot(
            "fixture-owner",
            "fixture-account",
            [{"instrument": "601088", "quantity": index + 1}],
            "2026-10-25T00:00:00+00:00",
            index,
            "manual",
        )
    before = business.status("fixture-owner", "fixture-account")
    assistant = Mock()
    constructor = Mock(return_value=assistant)
    monkeypatch.setattr(module, "FeishuAssistant", constructor)
    transport = Mock()
    bridge = SharedPersonalTestBridge(
        settings, execution_runtime=execution, transport=transport
    )
    assert bridge.runtime is execution and bridge.health_reporter is None
    assert not execution.running and "test_profile.health" not in execution._timers
    kwargs = constructor.call_args.kwargs
    assert kwargs["execution_runtime"] is execution and kwargs["capabilities"] == {
        "personal"
    }
    assert (
        Path(kwargs["store"].path).resolve()
        == Path(kwargs["personal_store"].path).resolve()
        == execution.store.path
    )
    assert business.status("fixture-owner", "fixture-account") == before
    spy = Mock(wraps=execution.stop)
    monkeypatch.setattr(execution, "stop", spy)
    bridge.stop()
    spy.assert_not_called()
    assistant.stop.assert_called_once()
    transport.stop.assert_called_once()


def test_wrong_external_bridge_database_rejects_before_original_store_creation(
    tmp_path,
):
    settings = profile_settings(tmp_path / "original")
    foreign = ExecutionRuntime(ExecutionStore(tmp_path / "foreign.sqlite3"))
    with pytest.raises(ValueError, match="原测试数据库"):
        SharedPersonalTestBridge(settings, execution_runtime=foreign, transport=Mock())
    assert not (settings["workspace"] / "test.sqlite3").exists()


def test_host_permission_failure_keeps_safe_class_stage_without_private_text(tmp_path):
    host, _execution, receiver = host_for(tmp_path)

    def fail():
        raise PermissionError(13, "private raw credential and user message")

    receiver.run = fail
    with pytest.raises(RuntimeError, match="receiver failed"):
        host.run()
    saved = json.loads(host.status_path.read_text(encoding="utf8"))
    assert any(
        row["stage"] == "bot.feishu.run" and row["exception_class"] == "PermissionError"
        for row in saved["diagnostics"]
    )
    assert "private raw" not in json.dumps(saved)


def test_real_process_exit_then_host_restart_records_one_unknown_summary(tmp_path):
    """An actual killed host/OS lease, not Mock.stop; native Task is a later gate."""
    database = tmp_path / "profile.sqlite3"
    status_dir = tmp_path / "status"
    code = """
import os, sys, threading
from pathlib import Path
from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.runtime_service import RuntimeService
class Bot:
    assistant = None
    def validate_config(self): pass
    def run(self): threading.Event().wait()
    def health(self): return {"ready": True}
    def pause_admission(self): pass
    def stop(self): pass
runtime = ExecutionRuntime(ExecutionStore(Path(sys.argv[1])))
host = RuntimeService({"storage": {"data_dir": sys.argv[2]}}, scheduling=False,
    execution_runtime=runtime, bot_factory=lambda config, runtime: {"fixture": Bot()},
    register_reports=False)
host.run(on_started=lambda: os._exit(23))
"""
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", code, str(database), str(status_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 23, (stdout, stderr)
    first = json.loads(
        (status_dir / "runtime/service_status.json").read_text(encoding="utf8")
    )
    assert first["state"] == "running" and first["pid"] == process.pid
    previous_owner = first["execution"]["owner_id"]
    receiver = Receiver(outcome="unknown")
    host, execution, _ = host_for(tmp_path, receiver)

    def started():
        deadline = time.monotonic() + 4
        while not receiver.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(receiver.sent) == 1
        host.stop()

    host.run(on_started=started)
    with execution.store.connection() as db:
        rows = db.execute(
            "SELECT * FROM execution_health_outbox WHERE event_key LIKE 'host-recovery:%'"
        ).fetchall()
        assert (
            len(rows) == 1
            and rows[0]["state"] == "unknown"
            and rows[0]["attempts"] == 1
        )
        assert rows[0]["event_key"] == "host-recovery:" + previous_owner
        evidence = json.loads(rows[0]["payload"])["previous_session"]
        assert (
            evidence["pid"] == process.pid
            and evidence["birth"] == first["process_birth"]
        )
    restarted, new_runtime, next_receiver = host_for(tmp_path)
    restarted.run(on_started=restarted.stop)
    assert not next_receiver.sent
    with new_runtime.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM execution_health_outbox WHERE event_key LIKE 'host-recovery:%'"
            ).fetchone()[0]
            == 1
        )


def test_history_bad_scope_does_not_construct_a_store_under_host_entry(
    tmp_path, monkeypatch
):
    settings = profile_settings(tmp_path)
    database = tmp_path / "test.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE test_application(app_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO test_application VALUES('other-app')")
    before = database.read_bytes()
    monkeypatch.setattr(group, "validate_production_filter", Mock())
    constructor = Mock(side_effect=AssertionError("no store on invalid binding"))
    monkeypatch.setattr("src.core.execution.ExecutionStore", constructor)
    with pytest.raises(ValueError, match="其他应用"):
        group.run_host(settings, proof_path="unused")
    constructor.assert_not_called()
    assert database.read_bytes() == before


def test_bound_stop_request_drains_and_persists_planned_without_recovery(
    tmp_path, monkeypatch
):
    from src.core import runtime_service as service

    monkeypatch.setattr(service, "HEARTBEAT_SECONDS", 0.01)
    host, runtime, receiver = host_for(tmp_path)
    observed = []
    original = service.atomic_write_json

    def write(path, payload, **kwargs):
        if path == host.status_path:
            observed.append(payload["state"])
        return original(path, payload, **kwargs)

    monkeypatch.setattr(service, "atomic_write_json", write)
    requested = []

    def started():
        requested.append(request_service_stop(host.config))
        assert request_service_stop(host.config) == requested[0]

    host.run(on_started=started)
    status = read_service_status(host.config)
    assert status["state"] == "stopped" and status["planned_stop"] is True
    assert status["accepted_stop_request_id"] == requested[0]["request_id"]
    assert observed == ["running", "stopping", "stopped"]
    assert receiver.trace.index("pause") < receiver.trace.index("transport_stop")
    assert not runtime.running and not runtime.store.has_live_owner()
    next_host, next_runtime, _ = host_for(tmp_path)
    next_host.run(on_started=next_host.stop)
    with next_runtime.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM execution_health_outbox WHERE event_key LIKE 'host-recovery:%'"
            ).fetchone()[0]
            == 0
        )


def test_old_stop_request_cannot_stop_a_new_host_session(tmp_path):
    first, _, _ = host_for(tmp_path)
    first.run(on_started=lambda: (request_service_stop(first.config), first.stop()))
    old = first.status_path.with_name("service_stop.json").read_bytes()
    second, _, _ = host_for(tmp_path)

    def started():
        assert second._consume_stop_request() is False
        assert not second.stop_event.is_set()
        assert second.status_path.with_name("service_stop.json").read_bytes() == old
        second.stop()

    second.run(on_started=started)


@pytest.mark.parametrize(
    "corruption", ["missing", "pid_bool", "birth_bool", "dead", "execution_list"]
)
def test_stop_request_rejects_unavailable_or_invalid_identity_without_write(
    tmp_path, corruption
):
    host, _, _ = host_for(tmp_path)
    if corruption == "missing":
        with pytest.raises(ValueError):
            request_service_stop(host.config)
        assert not host.status_path.with_name("service_stop.json").exists()
        return

    def started():
        original = host.status_path.read_text(encoding="utf8")
        status = json.loads(original)
        if corruption == "pid_bool":
            status["pid"] = True
        elif corruption == "birth_bool":
            status["process_birth"] = True
        elif corruption == "execution_list":
            status["execution"] = []
        else:
            status["process_birth"] = "wrong-birth"
        host.status_path.write_text(json.dumps(status), encoding="utf8")
        with pytest.raises(ValueError):
            request_service_stop(host.config)
        assert not host.status_path.with_name("service_stop.json").exists()
        host.status_path.write_text(original, encoding="utf8")
        host.stop()

    host.run(on_started=started)


def test_planned_marker_write_failure_keeps_failure_and_cleans_up(
    tmp_path, monkeypatch
):
    from src.core import runtime_service as service

    host, runtime, receiver = host_for(tmp_path)
    original = service.atomic_write_json

    def write(path, payload, **kwargs):
        if path == host.status_path and payload.get("state") == "stopping":
            raise PermissionError("sensitive path must not appear")
        return original(path, payload, **kwargs)

    monkeypatch.setattr(service, "atomic_write_json", write)
    with pytest.raises(RuntimeError):
        host.run(on_started=host.stop)
    status = read_service_status(host.config)
    assert status["planned_stop"] is False and status["state"] == "failed"
    assert any(
        item["exception_class"] == "PermissionError" for item in status["diagnostics"]
    )
    assert "sensitive path" not in json.dumps(status)
    assert receiver.closed.is_set() and not runtime.running


def test_control_cli_has_no_credentials_proof_or_host_start_and_only_canonical_mode(
    tmp_path, monkeypatch
):
    settings = {"workspace": tmp_path}
    load = Mock(return_value=settings)
    monkeypatch.setattr(group, "load_settings", load)
    request = Mock(return_value={"request_id": "fixture"})
    monkeypatch.setattr("src.core.runtime_service.request_service_stop", request)
    host = Mock(return_value=0)
    monkeypatch.setattr(group, "run_host", host)
    assert group.main(["--config", "fixture.yaml", "--request-stop"]) == 0
    load.assert_called_once_with(group.Path("fixture.yaml"), control_only=True)
    request.assert_called_once_with({"storage": {"data_dir": str(tmp_path)}})
    host.assert_not_called()
    with pytest.raises(SystemExit) as error:
        group.main(["--config", "fixture.yaml", "--shared-runtime"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        group.main(["--config", "fixture.yaml"])
    assert error.value.code == 2
    host.assert_not_called()
    assert group.main(["--config", "fixture.yaml", "--host-runtime"]) == 0
    host.assert_called_once()


def test_stop_intent_invalid_time_identity_and_command_are_ignored(tmp_path):
    host, _, _ = host_for(tmp_path)

    def started():
        request = request_service_stop(host.config)
        path = host.status_path.with_name("service_stop.json")
        for change in (
            {"command": "arbitrary"},
            {"pid": True},
            {"process_birth": True},
            {
                "requested_at": (
                    datetime.fromisoformat(host.started_at) - timedelta(seconds=1)
                ).isoformat()
            },
            {
                "requested_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=60)
                ).isoformat()
            },
            {"requested_at": "2026-10-25T00:00:00"},
        ):
            path.write_text(json.dumps(dict(request, **change)), encoding="utf8")
            assert host._consume_stop_request() is False
            assert not host.stop_event.is_set()
        host.stop()

    host.run(on_started=started)


def test_control_settings_does_not_read_any_credentials(tmp_path, monkeypatch):
    config = tmp_path / "isolated.yaml"
    config.write_text(
        "test_mode: true\nchat_id: oc_fixture\nallowed_sender_ids: [ou_fixture]\nworkspace_name: fixture\n",
        encoding="utf8",
    )
    credentials = Mock(side_effect=AssertionError("control must not read secrets"))
    monkeypatch.setattr(group, "read_credentials", credentials)
    result = group.load_settings(config, root=tmp_path, control_only=True)
    assert result == {
        "workspace": tmp_path / "data/personal_monitor_feishu_group_test/fixture"
    }
    credentials.assert_not_called()
