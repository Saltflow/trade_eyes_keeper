"""Actual executor/SQLite integration for report cleanup ownership failures."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import report_execution as report_module
from src.core.execution import ExecutionRuntime, ExecutionStore, RunContext
from src.core.execution_health import HealthReporter


class OriginalReportError(RuntimeError):
    pass


def wait_until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class Child:
    def __init__(self, ordinal):
        self.ordinal, self.pid = ordinal, 910000 + ordinal
        self.exit = threading.Event()
        self.returncode = None
        self.initial_error = None
        self.wait_faults = []
        self.wait_errors = 0
        self.poll_errors = 0
        self.immediate_timeout = False
        self.started_at = time.monotonic()

    def poll(self):
        if self.poll_errors:
            self.poll_errors -= 1
            raise OSError("injected_poll_failure")
        if self.exit.is_set():
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        if self.initial_error is not None:
            error, self.initial_error = self.initial_error, None
            raise error
        if self.wait_errors:
            self.wait_errors -= 1
            self.wait_faults.append(time.monotonic())
            if self.immediate_timeout:
                raise subprocess.TimeoutExpired("FAKE", timeout)
            raise OSError("injected_wait_failure_during_cleanup")
        if not self.exit.wait(timeout):
            raise subprocess.TimeoutExpired("FAKE", timeout)
        self.returncode = 0
        return 0


def assemble(monkeypatch, directory, scenario):
    reports = report_module
    store = ExecutionStore(directory / "runtime.sqlite3")
    runtime = ExecutionRuntime(store)
    reports.register_report_jobs(runtime, config={})
    children, faults = [], []
    first_seen, second_seen, cleanup_seen = (threading.Event() for _ in range(3))
    initial_error = OriginalReportError("preserve_this_primary_error")

    def spawn(_command, **_kwargs):
        child = Child(len(children) + 1)
        if child.ordinal == 1 and scenario in {
            "wait_exception",
            "interrupt_store_failure",
            "interrupt_store_recovers",
            "interrupt_store_recovers_fake_clock",
        }:
            child.initial_error = initial_error
        children.append(child)
        (first_seen if child.ordinal == 1 else second_seen).set()
        return child

    def cleanup_denied(child):
        faults.append({"ordinal": child.ordinal, "at": time.monotonic()})
        if scenario in {"repeat_wait_and_poll", "immediate_timeout"}:
            child.wait_errors = 3
            child.poll_errors = 3
            child.immediate_timeout = scenario == "immediate_timeout"
        cleanup_seen.set()
        state = {
            "api": None,
            "parent": None,
            "parent_birth": None,
            "descendants": {},
            "enumeration_complete": True,
        }
        raise reports._ReportCleanupPending(
            state, PermissionError("injected_cleanup_denial_no_OS_call")
        )

    monkeypatch.setattr(reports.subprocess, "Popen", spawn)
    monkeypatch.setattr(reports, "_stop_child", cleanup_denied)
    if scenario == "diagnostics_fail":
        previous_progress = RunContext.progress

        def failed_progress(context, stage):
            if stage == "report_cleanup_waiting_for_exit":
                raise OSError("diagnostic_store_unavailable")
            return previous_progress(context, stage)

        monkeypatch.setattr(RunContext, "progress", failed_progress)
        monkeypatch.setattr(
            reports.logger,
            "error",
            Mock(side_effect=RuntimeError("logger_unavailable")),
        )
    interrupt_calls = []
    diagnostic_log = Mock()
    if scenario.startswith("interrupt_store_"):
        monkeypatch.setattr(reports.logger, "error", diagnostic_log)
        original_interrupt = store.interrupt

        def denied_interrupt(*args):
            interrupt_calls.append(time.monotonic())
            if scenario == "interrupt_store_failure" or len(interrupt_calls) == 1:
                raise OSError("injected_interrupt_store_failure")
            return original_interrupt(*args)

        monkeypatch.setattr(store, "interrupt", denied_interrupt)
        if scenario == "interrupt_store_recovers_fake_clock":
            monkeypatch.setattr(
                reports,
                "time",
                SimpleNamespace(
                    monotonic=lambda: 0.0, sleep=lambda _seconds: None, time=time.time
                ),
            )
    payload = {"kind": "daily", "artifact_root": str(directory / "reports")}
    first_id = runtime.submit("reports.daily", payload, request_id="first")
    second_id = runtime.submit("reports.daily", payload, request_id="second")
    return SimpleNamespace(
        reports=reports,
        store=store,
        runtime=runtime,
        children=children,
        faults=faults,
        interrupt_calls=interrupt_calls,
        diagnostic_log=diagnostic_log,
        first_seen=first_seen,
        second_seen=second_seen,
        cleanup_seen=cleanup_seen,
        first_id=first_id,
        second_id=second_id,
    )


def assert_runtime_held(state):
    first, second = state.store.get(state.first_id), state.store.get(state.second_id)
    assert first["released_at"] is None and first["claim_token"]
    assert second["status"] == "queued" and not state.second_seen.is_set()
    with state.runtime._lock:
        assert state.first_id in state.runtime._active
    return {"first": first, "second": second}


def record(directory, state, extra):
    evidence = {
        "actual_runtime": type(state.runtime).__module__
        + "."
        + type(state.runtime).__name__,
        "candidate_sha256": hashlib.sha256(
            Path(state.reports.__file__).read_bytes()
        ).hexdigest(),
        "first": state.store.get(state.first_id),
        "second": state.store.get(state.second_id),
        "cleanup_calls": state.faults,
        "all_fake_children_exited": all(c.exit.is_set() for c in state.children),
        "runtime_stopped": not state.runtime.running,
        "active_final": list(state.runtime._active),
        "real_Popen": 0,
        "real_kill": 0,
        "model_calls": 0,
        "sends": 0,
        **extra,
    }
    (directory / "observation.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf8"
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "cancel",
        "wait_exception",
        "interrupt_store_failure",
        "interrupt_store_recovers",
        "interrupt_store_recovers_fake_clock",
        "repeat_wait_and_poll",
        "diagnostics_fail",
        "immediate_timeout",
    ],
)
def test_real_runtime_holds_claim_until_confirmed_natural_exit(
    monkeypatch, tmp_path, scenario
):
    directory = tmp_path / "cases" / scenario
    directory.mkdir(parents=True)
    state = assemble(monkeypatch, directory, scenario)
    runtime, first_id, second_id = state.runtime, state.first_id, state.second_id
    facts = {}
    try:
        runtime.start()
        assert state.first_seen.wait(5)
        if scenario not in {
            "wait_exception",
            "interrupt_store_failure",
            "interrupt_store_recovers",
            "interrupt_store_recovers_fake_clock",
        }:
            assert runtime.cancel(first_id)
        assert state.cleanup_seen.wait(5)
        if scenario in {"repeat_wait_and_poll", "immediate_timeout"}:
            assert wait_until(lambda: len(state.children[0].wait_faults) == 3)
            times = state.children[0].wait_faults
            assert all(b - a >= 0.20 for a, b in zip(times, times[1:]))
            facts["cleanup_wait_error_times"] = times.copy()
        else:
            time.sleep(0.35)
        first, second = state.store.get(first_id), state.store.get(second_id)
        assert not state.children[0].exit.is_set()
        assert first["released_at"] is None and first["claim_token"]
        assert first["concurrency_key"] == "reports"
        assert second["status"] == "queued" and not state.second_seen.is_set()
        with runtime._lock:
            assert first_id in runtime._active
        facts["while_child_alive"] = {
            "first": first,
            "second": second,
            "second_started": False,
        }
        health = HealthReporter(runtime)
        expected_reason = first["reason"]
        if scenario.startswith("interrupt_store_"):
            assert first["status"] == "running"
            assert len(state.interrupt_calls) == 1
            with runtime._lock:
                assert runtime._active[first_id][1].cancel_event.is_set()
            assert wait_until(lambda: len(state.interrupt_calls) >= 2)
            assert state.interrupt_calls[1] - state.interrupt_calls[0] >= 1.0
            facts["while_notice_retried"] = assert_runtime_held(state)
            if scenario != "interrupt_store_failure":
                assert wait_until(
                    lambda: state.store.get(first_id)["status"] == "unknown"
                )
                expected_reason = "report_cleanup_waiting_for_exit"
                assert state.store.get(first_id)["reason"] == expected_reason
                time.sleep(1.10)
                assert len(state.interrupt_calls) == 2
                facts["diagnostic_recovered_and_retries_stopped"] = True
            assert state.diagnostic_log.call_count == 1
            assert not state.diagnostic_log.call_args.kwargs
            facts["diagnostic_log_calls"] = state.diagnostic_log.call_count
            facts["interrupt_attempt_times"] = state.interrupt_calls.copy()
            facts["failed_notice_did_not_release_or_busy_retry"] = True
        else:
            assert first["status"] == "unknown"
            assert expected_reason == (
                "report_cleanup_waiting_for_exit"
                if scenario == "wait_exception"
                else "cancelled"
            )
        if scenario != "interrupt_store_failure":
            health.observe(runtime.health())
            with state.store.connection() as db:
                health_row = db.execute(
                    "SELECT payload FROM execution_health_outbox WHERE event_key=?",
                    ("run:" + first_id,),
                ).fetchone()
            event = json.loads(health_row[0])
            assert event["status"] == "unknown" and event["reason"] == expected_reason
            facts["health_event_while_key_held"] = event
        exited_at = time.monotonic()
        state.children[0].exit.set()
        assert state.second_seen.wait(5), "second report never starts after first exits"
        assert wait_until(lambda: state.store.get(first_id)["released_at"] is not None)
        assert state.children[1].started_at >= exited_at
        first = state.store.get(first_id)
        assert first["status"] == "unknown"
        if scenario == "interrupt_store_failure":
            assert first["reason"] == "handler_raised:OriginalReportError"
        else:
            assert first["reason"] == expected_reason
            health.observe(runtime.health())
            with state.store.connection() as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM execution_health_outbox WHERE event_key=?",
                        ("run:" + first_id,),
                    ).fetchone()[0]
                    == 1
                )
        facts["first_exit_requested_at"] = exited_at
        facts["second_child_started_at"] = state.children[1].started_at
        facts["ownership_contract_passed"] = True
    finally:
        for child in state.children:
            child.exit.set()
        runtime.stop(drain=True, timeout=5)
        record(directory, state, facts)


def test_incomplete_drain_retains_claim_until_exit_then_same_runtime_resumes(
    monkeypatch,
    tmp_path,
):
    directory = tmp_path / "cases" / "incomplete_drain"
    directory.mkdir(parents=True)
    state = assemble(monkeypatch, directory, "cancel")
    runtime, first_id, second_id = state.runtime, state.first_id, state.second_id
    facts = {}
    try:
        runtime.start()
        assert state.first_seen.wait(5)
        runtime.stop(drain=True, timeout=0.05)
        assert state.cleanup_seen.wait(5)
        first, second = state.store.get(first_id), state.store.get(second_id)
        assert first["released_at"] is None and first["claim_token"]
        assert second["status"] == "queued" and not state.second_seen.is_set()
        with runtime._lock:
            assert first_id in runtime._active
        with state.store.connection() as db:
            owner = db.execute(
                "SELECT state FROM execution_owners WHERE id=?", (runtime.owner_id,)
            ).fetchone()[0]
        assert owner == "running"
        with pytest.raises(RuntimeError, match="previous handlers have not drained"):
            runtime.start()
        facts["incomplete_drain"] = {
            "first": first,
            "second": second,
            "owner_state": owner,
        }
        exit_at = time.monotonic()
        state.children[0].exit.set()
        assert wait_until(lambda: not runtime._active)
        assert state.store.get(first_id)["released_at"] is not None
        runtime.start()
        assert state.second_seen.wait(5)
        assert state.children[1].started_at >= exit_at
        facts["same_runtime_resumed_only_after_first_exit"] = True
    finally:
        for child in state.children:
            child.exit.set()
        runtime.stop(drain=True, timeout=5)
        record(directory, state, facts)


@pytest.mark.parametrize("invalid_wait_result", [None, True])
def test_unproven_wait_return_cannot_release_or_spin(monkeypatch, invalid_wait_result):
    reports = report_module
    exit_event, done = threading.Event(), threading.Event()
    calls = []

    def wait(timeout):
        calls.append(time.monotonic())
        return 0 if exit_event.is_set() else invalid_wait_result

    process = SimpleNamespace(
        wait=wait, poll=lambda: 0 if exit_event.is_set() else None
    )
    state = {
        "api": None,
        "parent": None,
        "parent_birth": None,
        "descendants": {},
        "enumeration_complete": True,
    }
    cleanup_error = reports._ReportCleanupPending(
        state, PermissionError("cleanup denied")
    )
    monkeypatch.setattr(reports, "_stop_child", Mock(side_effect=cleanup_error))
    context = SimpleNamespace(progress=Mock(side_effect=OSError("diagnostic denied")))
    thread = threading.Thread(
        target=lambda: (reports._stop_child_until_exit(process, context), done.set())
    )
    thread.start()
    try:
        time.sleep(0.40)
        assert not done.is_set()
        assert len(calls) <= 3
        exit_event.set()
        assert done.wait(3)
    finally:
        exit_event.set()
        thread.join(3)
    assert not thread.is_alive()


class FakeNoSuchProcess(Exception):
    pass


class FakeAccessDenied(Exception):
    pass


class FakeTimeoutExpired(Exception):
    pass


def assemble_tree(monkeypatch, directory, scenario):
    reports = report_module
    store = ExecutionStore(directory / "runtime.sqlite3")
    runtime = ExecutionRuntime(store)
    reports.register_report_jobs(runtime, config={})
    world = SimpleNamespace(
        reports=reports,
        store=store,
        runtime=runtime,
        scenario=scenario,
        roots=[],
        process_calls=[],
        snapshots=[],
        stages=[],
        diagnostics=[],
        allow_census=threading.Event(),
        allow_read=threading.Event(),
        cleanup_seen=threading.Event(),
        first_seen=threading.Event(),
        second_seen=threading.Event(),
        teardown=threading.Event(),
    )
    if not scenario.startswith("census_"):
        world.allow_census.set()
    if scenario != "descendant_state_denied":
        world.allow_read.set()

    class Descendant:
        def __init__(self, root):
            self.root, self.pid = root, root.pid + 1000
            self.original_birth = self.birth = 1000.0 + root.ordinal
            self.exit = threading.Event()
            self.gone = False
            self.kill_births = []
            self.wait_calls = []

        def create_time(self):
            if (
                scenario == "census_partial_then_parent_exit"
                and not world.allow_census.is_set()
            ):
                self.root.exit.set()
                raise FakeAccessDenied("injected birth read denial")
            return self.birth

        def is_running(self):
            if self.gone:
                raise FakeNoSuchProcess("original identity is gone")
            if self.root.exit.is_set() and not world.allow_read.is_set():
                raise FakeAccessDenied("injected state denial")
            if scenario == "invalid_state_return" and not self.exit.is_set():
                return None
            return not self.exit.is_set() and self.birth == self.original_birth

        def wait(self, timeout=0):
            self.wait_calls.append(time.monotonic())
            if self.gone:
                raise FakeNoSuchProcess("original identity is gone")
            if scenario == "descendant_wait_denied" and not self.exit.is_set():
                raise FakeAccessDenied("injected wait denial")
            if not self.exit.is_set():
                raise FakeTimeoutExpired("fake descendant is live")
            return None  # noqa: RET501, PLR1711 - psutil non-child exit receipt.

        def terminate(self):
            pass

        def kill(self):
            self.kill_births.append(self.birth)
            world.cleanup_seen.set()
            if scenario == "kill_denied":
                raise FakeAccessDenied("injected kill denial")

    class Parent:
        def __init__(self, root):
            self.root = root
            self.pid = root.pid
            self.birth = 100.0 + root.ordinal
            self.census_attempts = 0

        def create_time(self):
            return self.birth

        def is_running(self):
            return not self.root.exit.is_set()

        def children(self, recursive=False):
            assert recursive is True
            self.census_attempts += 1
            if scenario == "census_partial_then_parent_exit":
                return [self.root.descendant]
            if not world.allow_census.is_set():
                if scenario in {
                    "census_denied_then_parent_exit",
                    "census_denied_wait_error_parent_exit",
                }:
                    self.root.exit.set()
                raise FakeAccessDenied("injected census denial")
            return [self.root.descendant]

    class Root:
        def __init__(self, ordinal):
            self.ordinal, self.pid = ordinal, 930000 + ordinal
            self.returncode = None
            self.initial_wait_error = (
                scenario == "census_denied_wait_error_parent_exit" and ordinal == 1
            )
            self.exit = threading.Event()
            self.started_at = time.monotonic()
            self.descendant = Descendant(self)
            self.parent = Parent(self)
            if world.teardown.is_set():
                self.exit.set()
                self.descendant.exit.set()

        def poll(self):
            if self.exit.is_set():
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.exit.set()

        def kill(self):
            self.exit.set()

        def wait(self, timeout=None):
            if self.initial_wait_error:
                self.initial_wait_error = False
                raise RuntimeError("injected report wait failure")
            if not self.exit.wait(timeout):
                raise reports.subprocess.TimeoutExpired("FAKE", timeout)
            self.returncode = 0
            return 0

    def spawn(_command, **_kwargs):
        root = Root(len(world.roots) + 1)
        world.roots.append(root)
        (world.first_seen if root.ordinal == 1 else world.second_seen).set()
        return root

    def process(pid):
        world.process_calls.append(pid)
        return next(root.parent for root in world.roots if root.pid == pid)

    def wait_procs(members, timeout):
        assert timeout == 5
        return (
            [member for member in members if isinstance(member, Parent)],
            [member for member in members if isinstance(member, Descendant)],
        )

    fake_psutil = ModuleType("psutil")
    fake_psutil.NoSuchProcess = FakeNoSuchProcess
    fake_psutil.AccessDenied = FakeAccessDenied
    fake_psutil.TimeoutExpired = FakeTimeoutExpired
    fake_psutil.Process, fake_psutil.wait_procs = process, wait_procs
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(reports.subprocess, "Popen", spawn)
    original_snapshot = reports._snapshot_report_children

    def capture_snapshot(root, state):
        if not world.snapshots or world.snapshots[-1] is not state:
            world.snapshots.append(state)
        return original_snapshot(root, state)

    monkeypatch.setattr(reports, "_snapshot_report_children", capture_snapshot)
    original_progress = RunContext.progress

    def capture_progress(context, stage):
        world.stages.append(stage)
        if stage.startswith("report_cleanup_"):
            world.cleanup_seen.set()
        return original_progress(context, stage)

    monkeypatch.setattr(RunContext, "progress", capture_progress)
    monkeypatch.setattr(
        reports.logger,
        "error",
        Mock(
            side_effect=lambda *a, **kw: world.diagnostics.append(
                {"args": list(a), "kwargs": kw}
            )
        ),
    )
    payload = {"kind": "daily", "artifact_root": str(directory / "reports")}
    world.first = runtime.submit("reports.daily", payload, request_id="first")
    world.second = runtime.submit("reports.daily", payload, request_id="second")
    return world


def assert_held(world):
    first, second = world.store.get(world.first), world.store.get(world.second)
    assert first["released_at"] is None and first["claim_token"]
    assert first["concurrency_key"] == "reports"
    assert second["status"] == "queued" and not world.second_seen.is_set()
    with world.runtime._lock:
        assert world.first in world.runtime._active
    return {"first": first, "second": second, "second_started": False}


def finish_and_record(directory, world, facts):
    world.teardown.set()
    world.allow_read.set()
    for root in world.roots:
        root.exit.set()
        root.descendant.exit.set()
    # Fixture-only independent verification closes all fake identities. This is
    # not an automatic production escape from an unverified descendant census.
    incomplete = [
        state for state in world.snapshots if not state["enumeration_complete"]
    ]
    if incomplete:
        assert all(
            root.exit.is_set() and root.descendant.exit.is_set() for root in world.roots
        )
        for state in incomplete:
            state["enumeration_complete"] = True
        facts["explicit_fixture_clearance"] = {
            "test_teardown_only": True,
            "all_fake_roots_and_descendants_independently_exited": True,
            "production_override_exists": False,
        }
    world.runtime.stop(drain=True, timeout=5)
    assert wait_until(lambda: not world.runtime._active)
    assert not world.runtime.running
    evidence = {
        "scenario": world.scenario,
        "actual_runtime": type(world.runtime).__module__
        + "."
        + type(world.runtime).__name__,
        "candidate_sha256": hashlib.sha256(
            Path(world.reports.__file__).read_bytes()
        ).hexdigest(),
        "first_final": world.store.get(world.first),
        "second_final": world.store.get(world.second),
        "process_factory_calls": world.process_calls,
        "stages": world.stages,
        "diagnostics": world.diagnostics,
        "all_fake_roots_and_descendants_exited": all(
            root.exit.is_set() and root.descendant.exit.is_set() for root in world.roots
        ),
        "runtime_stopped": not world.runtime.running,
        "active_final": list(world.runtime._active),
        "real_Popen": 0,
        "real_kill": 0,
        "model_calls": 0,
        "sends": 0,
        **facts,
    }
    (directory / "observation.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf8"
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "kill_denied",
        "kill_returns_but_still_alive",
        "descendant_state_denied",
        "descendant_wait_denied",
        "pid_reused",
        "no_such_process",
        "invalid_state_return",
    ],
)
def test_actual_runtime_keeps_key_for_known_live_or_unknown_descendant(
    monkeypatch, tmp_path, scenario
):
    directory = tmp_path / "tree_cases" / scenario
    directory.mkdir(parents=True)
    world = assemble_tree(monkeypatch, directory, scenario)
    facts = {}
    try:
        world.runtime.start()
        assert world.first_seen.wait(5)
        assert world.runtime.cancel(world.first)
        assert world.cleanup_seen.wait(5)
        time.sleep(0.40)
        root = world.roots[0]
        assert root.poll() == 0
        assert not root.descendant.exit.is_set()
        facts["while_parent_exited_descendant_unproven"] = assert_held(world)
        assert world.snapshots[0]["enumeration_complete"] is True
        assert world.process_calls == [root.pid]
        if scenario == "descendant_state_denied":
            root.descendant.exit.set()
            time.sleep(0.35)
            facts["even_exited_but_access_denied_still_held"] = assert_held(world)
            world.allow_read.set()
        elif scenario == "pid_reused":
            root.descendant.birth += 500.0
            facts["replacement_identity_is_live"] = not root.descendant.exit.is_set()
        elif scenario == "no_such_process":
            root.descendant.gone = True
        else:
            root.descendant.exit.set()
        exit_proof_at = time.monotonic()
        assert world.second_seen.wait(5)
        assert wait_until(
            lambda: world.store.get(world.first)["released_at"] is not None
        )
        assert world.roots[1].started_at >= exit_proof_at
        assert world.store.get(world.first)["status"] == "unknown"
        assert world.process_calls == [root.pid], (
            "must not reopen a reused descendant PID"
        )
        assert all(
            birth == root.descendant.original_birth
            for birth in root.descendant.kill_births
        )
        assert all(not item["kwargs"] for item in world.diagnostics)
        facts["second_started_only_after_original_identity_exit_evidence"] = True
        facts["descendant_kill_births"] = root.descendant.kill_births
        facts["ownership_contract_passed"] = True
    finally:
        finish_and_record(directory, world, facts)


def test_failed_census_can_retry_original_live_parent_then_wait_for_known_tree(
    monkeypatch,
    tmp_path,
):
    directory = tmp_path / "tree_cases" / "census_retry_live_parent"
    directory.mkdir(parents=True)
    world = assemble_tree(monkeypatch, directory, "census_retry_live_parent")
    facts = {}
    try:
        world.runtime.start()
        assert world.first_seen.wait(5)
        assert world.runtime.cancel(world.first)
        assert world.cleanup_seen.wait(5)
        time.sleep(0.35)
        facts["census_unknown"] = assert_held(world)
        assert world.snapshots[0]["enumeration_complete"] is False
        world.allow_census.set()
        assert wait_until(lambda: world.snapshots[0]["enumeration_complete"])
        root = world.roots[0]
        root.exit.set()
        time.sleep(0.35)
        facts["parent_exited_known_descendant_live"] = assert_held(world)
        root.descendant.exit.set()
        assert world.second_seen.wait(5)
        assert world.process_calls == [root.pid]
        facts["census_retried_same_original_object_only"] = True
        facts["ownership_contract_passed"] = True
    finally:
        finish_and_record(directory, world, facts)


@pytest.mark.parametrize(
    "scenario",
    [
        "census_denied_then_parent_exit",
        "census_partial_then_parent_exit",
        "census_denied_wait_error_parent_exit",
    ],
)
def test_incomplete_census_after_parent_exit_stays_fail_closed(
    monkeypatch, tmp_path, scenario
):
    directory = tmp_path / "tree_cases" / scenario
    directory.mkdir(parents=True)
    world = assemble_tree(monkeypatch, directory, scenario)
    facts = {}
    try:
        world.runtime.start()
        assert world.first_seen.wait(5)
        if scenario != "census_denied_wait_error_parent_exit":
            assert world.runtime.cancel(world.first)
        assert world.cleanup_seen.wait(5)
        root = world.roots[0]
        assert root.poll() == 0
        root.descendant.exit.set()
        world.allow_census.set()
        time.sleep(0.65)
        facts["all_known_exited_but_census_unknown"] = assert_held(world)
        assert world.snapshots[0]["enumeration_complete"] is False
        assert "report_cleanup_unverified_tree" in world.stages
        assert world.process_calls == [root.pid]
        assert root.parent.census_attempts == 1
        assert all(not item["kwargs"] for item in world.diagnostics)
        if scenario == "census_denied_wait_error_parent_exit":
            first = world.store.get(world.first)
            assert first["status"] == "unknown"
            assert first["reason"] == "report_cleanup_unverified_tree"
            health = HealthReporter(world.runtime)
            health.observe(world.runtime.health())
            with world.store.connection() as db:
                event = json.loads(
                    db.execute(
                        "SELECT payload FROM execution_health_outbox WHERE event_key=?",
                        ("run:" + world.first,),
                    ).fetchone()[0]
                )
            assert event["status"] == "unknown"
            assert event["reason"] == "report_cleanup_unverified_tree"
            facts["health_event_while_key_held"] = event
        facts["no_automatic_release_after_parent_exit"] = True
        facts["partial_snapshot_retained"] = len(world.snapshots[0]["descendants"])
        facts["ownership_contract_passed"] = True
    finally:
        finish_and_record(directory, world, facts)


def test_incomplete_drain_holds_known_descendant_then_same_runtime_resumes(
    monkeypatch, tmp_path
):
    directory = tmp_path / "tree_cases" / "descendant_incomplete_drain"
    directory.mkdir(parents=True)
    world = assemble_tree(monkeypatch, directory, "kill_returns_but_still_alive")
    facts = {}
    try:
        world.runtime.start()
        assert world.first_seen.wait(5)
        world.runtime.stop(drain=True, timeout=0.05)
        assert world.cleanup_seen.wait(5)
        assert world.roots[0].poll() == 0
        facts["incomplete_drain"] = assert_held(world)
        with world.store.connection() as db:
            owner_state = db.execute(
                "SELECT state FROM execution_owners WHERE id=?",
                (world.runtime.owner_id,),
            ).fetchone()[0]
        assert owner_state == "running"
        with pytest.raises(RuntimeError, match="previous handlers have not drained"):
            world.runtime.start()
        world.roots[0].descendant.exit.set()
        assert wait_until(lambda: not world.runtime._active)
        assert world.store.get(world.first)["released_at"] is not None
        world.runtime.start()
        assert world.second_seen.wait(5)
        facts["owner_held_during_incomplete_drain"] = owner_state
        facts["same_runtime_resumed_only_after_tree_exit"] = True
        facts["ownership_contract_passed"] = True
    finally:
        finish_and_record(directory, world, facts)
