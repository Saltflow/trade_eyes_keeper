"""Public execution contracts; no production data, network or external sends."""

import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.core import execution
from src.core.execution import (
    ExecutionRuntime,
    ExecutionStore,
    JobSpec,
    ModuleDisabled,
    ModuleSpec,
    QueueFull,
    RunResult,
    get_runtime,
)


@pytest.fixture
def store(tmp_path):
    return ExecutionStore(tmp_path / "shared.sqlite3")


@pytest.fixture
def runtime(store):
    value = ExecutionRuntime(store)
    yield value
    value.stop(timeout=3)


def register(runtime, handler=lambda p, c: RunResult(), **kwargs):
    job = JobSpec("test.run", handler, **kwargs)
    runtime.register(ModuleSpec("test", (job,)))
    return job


def eventually(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("condition did not become true")


def test_reused_connection_keeps_full_sync_and_rolls_back_failed_transaction(store):
    with store.connection() as db:
        identity = id(db)
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        db.execute("CREATE TABLE reuse_probe(value TEXT)")
    with pytest.raises(ValueError, match="abort"), store.connection() as db:
        assert id(db) == identity
        db.execute("INSERT INTO reuse_probe VALUES ('not committed')")
        raise ValueError("abort")
    with store.connection() as db:
        assert id(db) == identity
        assert db.execute("SELECT * FROM reuse_probe").fetchall() == []
    store.close_idle_connections()
    with store.connection() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("SELECT * FROM reuse_probe").fetchall() == []


def test_nested_store_read_never_commits_or_observes_outer_admission(store):
    with store.connection() as outer:
        outer.execute("BEGIN IMMEDIATE")
        run_id = store.submit("pending", {}, request_id="nested", db=outer)
        assert store.get(run_id) is None
        assert outer.in_transaction
        outer.rollback()
    assert store.get(run_id) is None


@pytest.mark.parametrize("autocommit", [False, True])
def test_idle_connection_moves_between_live_threads_without_reopening(
    store, monkeypatch, autocommit
):
    opened, handles, threads = [], [], []
    returned, release = threading.Event(), threading.Event()
    original = store._open_connection

    def track(*args):
        connection = original(*args)
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_open_connection", track)

    def first():
        threads.append(threading.get_ident())
        with store.connection(autocommit=autocommit) as db:
            handles.append(db)
            db.execute("CREATE TABLE thread_reuse(value TEXT)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO thread_reuse VALUES ('durable')")
            db.commit()
        returned.set()
        assert release.wait(5)

    def second():
        assert returned.wait(5)
        threads.append(threading.get_ident())
        with pytest.raises(ValueError, match="abort"), store.connection(
            autocommit=autocommit
        ) as db:
            assert db is handles[0]
            assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO thread_reuse VALUES ('rollback')")
            raise ValueError("abort")

    with execution.ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(first)
        try:
            pool.submit(second).result(timeout=5)
        finally:
            release.set()
        waiting.result(timeout=5)
    assert len(set(threads)) == 2
    with store.connection(autocommit=autocommit) as db:
        assert db is handles[0]
        assert [row[0] for row in db.execute("SELECT * FROM thread_reuse")] == [
            "durable"
        ]
    assert opened == handles
    assert len(store._connections) == 1


def test_cross_thread_borrow_cannot_take_an_active_transaction(store):
    writing, release = threading.Event(), threading.Event()
    handles = []
    with store.connection() as db:
        db.execute("CREATE TABLE active_borrow(value TEXT)")

    def writer():
        with store.connection() as db:
            handles.append(db)
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO active_borrow VALUES ('uncommitted')")
            writing.set()
            assert release.wait(5)
            db.rollback()

    def reader():
        assert writing.wait(5)
        with store.connection() as db:
            assert db is not handles[0]
            assert db.execute("SELECT * FROM active_borrow").fetchall() == []
            store.close_idle_connections()
            assert db.execute("SELECT 1").fetchone()[0] == 1

    with execution.ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(writer)
        try:
            pool.submit(reader).result(timeout=5)
        finally:
            release.set()
        waiting.result(timeout=5)
    with store.connection() as db:
        assert db.execute("SELECT * FROM active_borrow").fetchall() == []
    assert len(store._connections) <= 4


def test_idle_handle_with_unfinished_transaction_is_not_moved_to_another_thread(
    store,
):
    with store.connection() as dirty:
        dirty.execute("CREATE TABLE cleanup_failure(value TEXT)")
    # Inject the state a failed transaction cleanup could leave in an idle slot.
    dirty.execute("BEGIN IMMEDIATE")
    dirty.execute("INSERT INTO cleanup_failure VALUES ('uncommitted')")

    def read():
        with store.connection() as db:
            assert db is not dirty
            assert db.execute("SELECT * FROM cleanup_failure").fetchall() == []

    try:
        with execution.ThreadPoolExecutor(1) as pool:
            pool.submit(read).result(timeout=5)
    finally:
        dirty.rollback()
    with store.connection() as db:
        assert db.execute("SELECT * FROM cleanup_failure").fetchall() == []


def test_connection_cleanup_does_not_close_a_borrowed_worker_connection(store):
    borrowed, release = threading.Event(), threading.Event()

    def worker():
        with store.connection() as db:
            borrowed.set()
            assert release.wait(2)
            return db.execute("SELECT 1").fetchone()[0]

    with execution.ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker)
        assert borrowed.wait(1)
        store.close_idle_connections()
        release.set()
        assert future.result(timeout=2) == 1
    store.close_idle_connections()
    assert not store._connections


def test_late_busy_worker_can_reuse_a_bounded_cache_after_idle_threads(store):
    barrier = threading.Barrier(5)
    release = threading.Event()

    def previous_worker():
        with store.connection() as db:
            assert db.execute("SELECT 1").fetchone()[0] == 1
        barrier.wait(timeout=3)
        assert release.wait(3)

    def busy_worker():
        with store.connection() as first:
            assert first.execute("SELECT 1").fetchone()[0] == 1
        with store.connection() as second:
            assert second.execute("SELECT 1").fetchone()[0] == 1
        return first is second

    with execution.ThreadPoolExecutor(5) as pool:
        previous = [pool.submit(previous_worker) for _ in range(4)]
        barrier.wait(timeout=3)
        try:
            assert pool.submit(busy_worker).result(timeout=3)
            assert len(store._connections) <= 4
        finally:
            release.set()
        for future in previous:
            future.result(timeout=3)


def test_external_transaction_rolls_back_receipt_and_execution_together(store):
    with store.connection() as db:
        db.execute("CREATE TABLE receipts (id TEXT PRIMARY KEY)")
    db = sqlite3.connect(store.path)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO receipts VALUES ('message')")
        run_id = store.submit("test", {}, request_id="message", db=db)
        assert db.in_transaction
        assert store.get(run_id) is None
        db.rollback()
    finally:
        db.close()
    assert store.get(run_id) is None
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_external_commit_is_not_performed_by_submit(store):
    db = sqlite3.connect(store.path)
    try:
        db.execute("BEGIN IMMEDIATE")
        run_id = store.submit("test", {"value": 1}, db=db)
        assert store.get(run_id) is None
        db.commit()
        assert store.get(run_id)["payload"] == {"value": 1}
    finally:
        db.close()


def test_queue_capacity_and_duplicate_admission_are_transactional(store):
    first = store.submit("test", {}, request_id="one", max_pending=1)
    assert store.submit("test", {}, request_id="one", max_pending=1) == first
    with pytest.raises(QueueFull):
        store.submit("test", {}, request_id="two", max_pending=1)
    assert store.count_pending("test") == 1
    with pytest.raises(ValueError, match="different input"):
        store.submit("test", {"changed": True}, request_id="one")
    with pytest.raises(ValueError, match="different input"):
        store.submit("test", {}, request_id="one", session_key="someone_else")


def test_caller_rollback_does_not_leave_a_queue_full_rejection(store):
    db = sqlite3.connect(store.path)
    try:
        db.execute("BEGIN IMMEDIATE")
        store.submit("test", {}, max_pending=1, db=db)
        with pytest.raises(QueueFull):
            store.submit("test", {}, max_pending=1, db=db)
        db.rollback()
    finally:
        db.close()
    assert store.count_pending("test") == 0


def test_list_filter_and_get_run_alias(store):
    first = store.submit("one", {})
    store.submit("two", {})
    assert store.get_run(first)["job_id"] == "one"
    assert [row["id"] for row in store.list_runs(job_id="one")] == [first]
    assert len(store.list(status="queued")) == 2


def test_duplicate_claim_is_impossible_across_connections(store):
    store.apply(ModuleSpec("test", (JobSpec("work", lambda p, c: None),)))
    run_id = store.submit("work", {})
    store.owner_started("one")
    store.owner_started("two")
    barrier = threading.Barrier(2)

    def claim(owner):
        barrier.wait()
        return store.claim(owner, "default", ["work"])

    with execution.ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(claim, ["one", "two"]))
    claimed = [row for row in rows if row]
    assert len(claimed) == 1
    assert claimed[0]["id"] == run_id


def test_stale_lease_and_new_store_do_not_reclaim_live_work(store):
    store.apply(ModuleSpec("test", (JobSpec("work", lambda p, c: None),)))
    store.owner_started("live")
    run_id = store.submit("work", {})
    claim = store.claim("live", "default", ["work"], lease_seconds=-1)
    reopened = ExecutionStore(store.path)
    assert reopened.get(run_id)["status"] == "running"
    assert reopened.recover_offline() == 0
    assert reopened.claim("different", "default", ["work"]) is None
    assert store.finish(run_id, claim["claim_token"], RunResult())


def test_reused_pid_identity_is_recovered_without_blind_replay(store, monkeypatch):
    store.apply(ModuleSpec("test", (JobSpec("work", lambda p, c: None),)))
    store.owner_started("old")
    run_id = store.submit("work", {})
    claimed = store.claim("old", "default", ["work"])
    monkeypatch.setattr(execution, "_identity", lambda pid: "different_birth")
    assert store.recover_offline() == 1
    row = store.get(run_id)
    assert row["status"] == "unknown"
    assert row["reason"] == "owner_offline"
    assert not store.finish(run_id, claimed["claim_token"], RunResult())
    assert store.claim("other", "default", ["work"]) is None


def test_real_dead_process_running_work_is_unknown_queued_work_survives(store):
    code = """
import sys
from src.core.execution import ExecutionStore, ModuleSpec, JobSpec
s=ExecutionStore(sys.argv[1])
s.apply(ModuleSpec('test',(JobSpec('work',lambda p,c:None),)))
s.owner_started('child')
first=s.submit('work',{'n':1})
second=s.submit('work',{'n':2})
s.claim('child','default',['work'])
print(first,second,flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, str(store.path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    running, queued = result.stdout.strip().split()
    assert store.recover_offline() == 1
    assert store.get(running)["status"] == "unknown"
    assert store.get(queued)["status"] == "queued"


def test_owner_with_access_limited_identity_is_not_assumed_dead(store, monkeypatch):
    store.owner_started("protected")
    monkeypatch.setattr(execution, "_identity", lambda pid: "?")
    assert store.has_live_owner()


def test_session_ordering_does_not_block_other_sessions(store):
    store.apply(ModuleSpec("test", (JobSpec("work", lambda p, c: None),)))
    store.owner_started("one")
    first = store.submit("work", {}, session_key="A")
    second = store.submit("work", {}, session_key="A")
    independent = store.submit("work", {}, session_key="B")
    a = store.claim("one", "default", ["work"], lane_capacity=2)
    b = store.claim("one", "default", ["work"], lane_capacity=2)
    assert (a["id"], b["id"]) == (first, independent)
    assert store.get(second)["status"] == "queued"


def test_concurrency_key_is_global_across_jobs_and_sessions(store):
    jobs = tuple(
        JobSpec(key, lambda p, c: None, concurrency_key="reports")
        for key in ("daily", "brief")
    )
    store.apply(ModuleSpec("reports", jobs))
    store.owner_started("one")
    first = store.submit("daily", {}, session_key="A")
    second = store.submit("brief", {}, session_key="B")
    claimed = store.claim("one", "default", ["daily", "brief"], lane_capacity=2)
    assert claimed["id"] == first
    assert store.claim("two", "default", ["daily", "brief"], lane_capacity=2) is None
    store.finish(first, claimed["claim_token"], RunResult())
    assert (
        store.claim("two", "default", ["daily", "brief"], lane_capacity=2)["id"]
        == second
    )


def test_timeout_token_rejects_late_success_and_retains_effect_hold(store):
    store.apply(
        ModuleSpec(
            "test", (JobSpec("work", lambda p, c: None, concurrency_key="account"),)
        )
    )
    store.owner_started("one")
    first = store.submit("work", {}, session_key="A")
    second = store.submit("work", {}, session_key="A")
    claimed = store.claim("one", "default", ["work"], lane_capacity=2)
    token = claimed["claim_token"]
    assert store.interrupt(first, token, "timeout")
    assert not store.finish(first, token, RunResult())
    assert not store.progress(first, token, "late progress")
    assert store.claim("two", "default", ["work"], lane_capacity=2) is None
    store.release(first, token)
    assert store.claim("two", "default", ["work"], lane_capacity=2)["id"] == second


def test_two_sessions_execute_in_parallel_with_separate_contexts(runtime):
    arrived = threading.Barrier(3)
    release = threading.Event()
    seen = []

    def handle(payload, context):
        seen.append((payload["screenshot"], context.run_id, context.cancel_event))
        context.progress("ocr")
        arrived.wait(timeout=3)
        assert release.wait(3)
        return RunResult(artifacts={"batch": payload["screenshot"]})

    register(runtime, handle, lane="conversation")
    one = runtime.submit("test.run", {"screenshot": "A"}, session_key="A")
    two = runtime.submit("test.run", {"screenshot": "B"}, session_key="B")
    runtime.start()
    try:
        arrived.wait(timeout=3)
        assert len(seen) == 2
        assert seen[0][1:] != seen[1][1:]
    finally:
        release.set()
    assert runtime.wait(one, 3)["result"]["artifacts"] == {"batch": "A"}
    assert runtime.wait(two, 3)["result"]["artifacts"] == {"batch": "B"}


def test_same_session_serializes_and_exception_does_not_block_next(runtime):
    seen = []

    def handle(payload, context):
        seen.append(payload["step"])
        if payload["step"] == 1:
            raise ValueError("secret-must-not-be-recorded")
        return RunResult()

    register(runtime, handle, lane="conversation")
    first = runtime.submit("test.run", {"step": 1}, session_key="A")
    second = runtime.submit("test.run", {"step": 2}, session_key="A")
    runtime.start()
    assert runtime.wait(second, 3)["status"] == "succeeded"
    assert seen == [1, 2]
    assert runtime.get_run(first)["reason"] == "handler_raised:ValueError"
    assert "secret" not in str(runtime.get_run(first))


def test_exception_after_effect_is_unknown(runtime):
    def handle(payload, context):
        context.mark_side_effect("http_submitted")
        raise ValueError("private")

    register(runtime, handle)
    run_id = runtime.submit("test.run", {})
    runtime.start()
    assert runtime.wait(run_id, 3)["status"] == "unknown"


def test_saturated_conversation_lane_does_not_block_delivery(runtime):
    started = threading.Barrier(3)
    release = threading.Event()

    def blocked(payload, context):
        started.wait(timeout=3)
        release.wait(3)
        return RunResult()

    runtime.register(ModuleSpec("chat", (JobSpec("chat", blocked, "conversation"),)))
    runtime.register(
        ModuleSpec(
            "personal",
            (JobSpec("send", lambda p, c: RunResult(), "personal_delivery"),),
        )
    )
    runtime.submit("chat", {}, session_key="A")
    runtime.submit("chat", {}, session_key="B")
    runtime.start()
    try:
        started.wait(timeout=3)
        delivery = runtime.submit("send", {})
        assert runtime.wait(delivery, 1)["status"] == "succeeded"
    finally:
        release.set()


def test_timeout_sets_cancel_and_never_overwrites_unknown(runtime):
    started, release = threading.Event(), threading.Event()
    contexts = []

    def handle(payload, context):
        contexts.append(context)
        started.set()
        release.wait(3)
        return RunResult()

    register(runtime, handle, timeout_seconds=0.1, concurrency_key="single")
    run_id = runtime.submit("test.run", {})
    runtime.start()
    try:
        assert started.wait(2)
        assert runtime.wait(run_id, 2)["reason"] == "timeout"
        assert contexts[0].cancel_event.is_set()
        assert not runtime.health()["ready"]
    finally:
        release.set()
    eventually(lambda: runtime.get_run(run_id)["released_at"])
    assert runtime.get_run(run_id)["status"] == "unknown"


def test_disable_drains_current_run_and_preserves_pending(runtime):
    started, release = threading.Event(), threading.Event()

    def handle(payload, context):
        started.set()
        release.wait(3)
        return RunResult()

    register(runtime, handle)
    first = runtime.submit("test.run", {})
    second = runtime.submit("test.run", {})
    runtime.start()
    try:
        assert started.wait(2)
        assert runtime.disable("test")["state"] == "draining"
        release.set()
        assert runtime.wait(first, 2)["status"] == "succeeded"
        assert runtime.get_run(second)["status"] == "queued"
        # Ordinary repeated adapter registration must not revive a disabled module.
        register(runtime, handle)
        assert not runtime.module_status("test")["enabled"]
        runtime.enable("test")
        assert runtime.wait(second, 2)["status"] == "succeeded"
    finally:
        release.set()


def test_apply_is_atomic_on_conflicting_job_and_keeps_old_registration(runtime):
    register(runtime)
    before = runtime.module_status("test")
    with pytest.raises(ValueError, match="belongs"):
        runtime.apply(ModuleSpec("conflict", (JobSpec("test.run", lambda p, c: None),)))
    assert runtime.module_status("test") == before
    assert runtime.module_status("conflict") is None


def test_old_timer_generation_and_old_scheduled_work_do_not_revive(runtime):
    register(runtime)
    calls = []
    runtime.add_timer("old", time.time(), lambda: calls.append("old"), module_id="test")
    scheduled = runtime.schedule("test.run", time.time() + 60)
    manual = runtime.submit("test.run", {})
    runtime.apply(
        ModuleSpec("test", (JobSpec("test.run", lambda p, c: RunResult()),), "2")
    )
    runtime.start()
    assert runtime.wait(manual, 2)["status"] == "succeeded"
    assert runtime.get_run(scheduled)["reason"] == "superseded"
    assert calls == []
    stale = runtime.submit("test.run", {}, config_version="1", source="schedule")
    assert runtime.get_run(stale)["reason"] == "stale_configuration"


def test_disabled_module_timer_is_removed_and_light_timer_can_submit(runtime):
    register(runtime)
    calls = []
    runtime.add_timer("old", time.time(), lambda: calls.append("bad"), module_id="test")
    runtime.disable("test")
    runtime.enable("test")
    runtime.add_timer(
        "new",
        time.time(),
        lambda: calls.append(runtime.submit("test.run", {})),
        module_id="test",
    )
    runtime.start()
    eventually(lambda: calls)
    assert len(calls) == 1
    assert runtime.wait(calls[0], 2)["status"] == "succeeded"


def test_not_before_aware_deadline_and_persistent_queue(runtime):
    register(runtime)
    with pytest.raises(ValueError, match="timezone"):
        runtime.schedule("test.run", datetime(2026, 10, 25))  # noqa: DTZ001
    run_id = runtime.schedule(
        "test.run", datetime.now(timezone.utc) + timedelta(seconds=0.2)
    )
    runtime.start()
    assert runtime.get_run(run_id)["status"] == "queued"
    assert runtime.wait(run_id, 2)["status"] == "succeeded"


def test_graceful_stop_and_restart_preserve_unstarted_work(runtime):
    register(runtime)
    runtime.start()
    runtime.stop()
    run_id = runtime.submit("test.run", {})
    assert not runtime.store.has_live_owner()
    runtime.start()
    assert runtime.wait(run_id, 2)["status"] == "succeeded"


def test_cancel_queued_is_skipped_and_wait_timeout_does_not_cancel(runtime):
    register(runtime)
    run_id = runtime.submit("test.run", {})
    with pytest.raises(TimeoutError):
        runtime.wait(run_id, 0.01)
    assert runtime.cancel(run_id)
    assert runtime.wait(run_id, 0.01)["reason"] == "cancelled"


def test_singleton_is_profile_scoped_and_never_autostarts(tmp_path):
    one = get_runtime(tmp_path / "one.sqlite")
    assert get_runtime(tmp_path / "one.sqlite") is one
    assert get_runtime(tmp_path / "two.sqlite") is not one
    assert not one.running


@pytest.mark.parametrize(
    "status", ["succeeded", "skipped", "failed", "partial", "unknown"]
)
def test_run_result_roundtrips_all_terminal_statuses(status):
    result = RunResult(
        status, "why", artifacts=["report.pdf"], deliveries={"email": "sent"}
    )
    assert RunResult.from_dict(result.to_dict()) == result


def test_unsafe_recovery_and_invalid_result_are_rejected():
    with pytest.raises(ValueError, match="recovery"):
        JobSpec("id", lambda p, c: None, recovery="retry")
    with pytest.raises(ValueError, match="status"):
        RunResult("running")


def test_idle_ticks_do_not_create_runs_or_rewrite_progress(runtime):
    register(runtime)
    runtime.tick()
    before = runtime.module_status("test")
    for _ in range(3):
        runtime.tick()
    assert runtime.store.list() == []
    assert runtime.module_status("test") == before


def test_current_process_identity_is_available():
    assert execution._identity(os.getpid())


def test_start_if_unowned_is_atomic_for_competing_standalone_clients(store):
    values = [ExecutionRuntime(store), ExecutionRuntime(ExecutionStore(store.path))]
    barrier = threading.Barrier(2)

    def start(runtime):
        barrier.wait()
        return runtime.start_if_unowned()

    try:
        with execution.ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(start, values)) == [False, True]
        assert len(store.active_owners()) == 1
    finally:
        for value in values:
            value.stop()
    assert not store.has_live_owner()


def test_timer_callbacks_can_acquire_adapter_lock_without_host_lock_inversion(runtime):
    completed, callback_done = threading.Event(), threading.Event()
    workers = []

    def callback():
        # A worker holds an adapter lock and calls back into the runtime while
        # the timer wants that lock. add_timer must not wait on the host lock.
        def worker():
            runtime.add_timer("from-worker", time.time() + 60, lambda: None)
            completed.set()

        thread = threading.Thread(target=worker)
        workers.append(thread)
        thread.start()
        assert completed.wait(1)
        callback_done.set()

    runtime.add_timer("call", time.time(), callback)
    runtime.start()
    try:
        assert callback_done.wait(2)
        assert runtime.health()["failure"] == ""
    finally:
        for worker in workers:
            worker.join(timeout=2)


def test_stop_does_not_release_noncooperative_effect_hold(runtime):
    started, release = threading.Event(), threading.Event()

    def handle(payload, context):
        started.set()
        release.wait(3)
        return RunResult()

    job = register(runtime, handle, concurrency_key="exclusive")
    first = runtime.submit(job.id, {})
    runtime.start()
    other = ExecutionRuntime(ExecutionStore(runtime.store.path))
    other.register(ModuleSpec("test", (job,)))
    try:
        assert started.wait(2)
        runtime.stop(drain=False, timeout=0)
        second = other.submit(job.id, {})
        assert runtime.get_run(first)["reason"] == "shutdown_incomplete"
        # The real owner process is alive, so another runtime cannot take over.
        assert not other.start_if_unowned()
        other.start()
        assert other.get_run(second)["status"] == "queued"
        release.set()
        assert other.wait(second, 2)["status"] == "succeeded"
        assert runtime.get_run(first)["status"] == "unknown"
    finally:
        release.set()
        other.stop(timeout=3)


def test_unregister_releases_local_handlers_after_disabling_without_data_loss(runtime):
    register(runtime)
    queued = runtime.submit("test.run", {})
    assert runtime.unregister("test")["state"] == "disabled"
    assert not runtime.is_registered("test")
    assert runtime.get_run(queued)["status"] == "queued"
    register(runtime)
    assert not runtime.module_status("test")["enabled"]
    runtime.enable("test")
    runtime.start()
    assert runtime.wait(queued, 2)["status"] == "succeeded"


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_finite_and_positive(invalid):
    with pytest.raises(ValueError, match="positive"):
        JobSpec("work", lambda p, c: None, timeout_seconds=invalid)


def test_concurrent_adapter_registration_cannot_revive_disabled_module(runtime):
    job = register(runtime)
    for _ in range(3):
        runtime.enable("test")
        barrier = threading.Barrier(2)

        def disable(barrier=barrier):
            barrier.wait()
            runtime.disable("test")

        def reregister(barrier=barrier):
            barrier.wait()
            runtime.register(ModuleSpec("test", (job,)))

        with execution.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(disable), pool.submit(reregister)]
            for future in futures:
                future.result(timeout=3)
        assert not runtime.module_status("test")["enabled"]


def test_idle_dispatch_probes_shared_queue_once_for_all_lanes(runtime, monkeypatch):
    for lane in ("conversation", "personal_market", "personal_delivery", "report"):
        runtime.register(ModuleSpec(lane, (JobSpec(lane, lambda p, c: None, lane),)))
    calls = []
    original = runtime.store.ready_jobs

    def probe(job_ids):
        calls.append(job_ids)
        return original(job_ids)

    monkeypatch.setattr(runtime.store, "ready_jobs", probe)
    runtime.tick()
    assert len(calls) == 1
    assert set(calls[0]) == {
        "conversation",
        "personal_market",
        "personal_delivery",
        "report",
    }
    assert runtime.store.list() == []


def test_wait_is_notification_driven_without_fifty_sqlite_reads_per_second(
    runtime, monkeypatch
):
    register(runtime)
    run_id = runtime.schedule("test.run", time.time() + 60)
    calls = []
    original = runtime.get_run

    def get(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(runtime, "get_run", get)
    with pytest.raises(TimeoutError):
        runtime.wait(run_id, 0.15)
    assert len(calls) <= 2


def test_unrelated_dispatch_wakes_do_not_make_waiter_reread_sqlite(
    runtime, monkeypatch
):
    register(runtime)
    run_id = runtime.schedule("test.run", time.time() + 60)
    calls = []
    original = runtime.get_run
    first_read = threading.Event()

    def get(value):
        calls.append(value)
        first_read.set()
        return original(value)

    monkeypatch.setattr(runtime, "get_run", get)

    def unrelated_changes():
        assert first_read.wait(1)
        for index in range(8):
            runtime.add_timer(f"unrelated-{index}", time.time() + 60, lambda: None)
            time.sleep(0.005)

    producer = threading.Thread(target=unrelated_changes)
    producer.start()
    try:
        with pytest.raises(TimeoutError):
            runtime.wait(run_id, 0.15)
    finally:
        producer.join(timeout=1)
    assert not producer.is_alive()
    assert len(calls) <= 2


def test_terminal_change_between_waiter_read_and_subscribe_is_not_lost(
    runtime, monkeypatch
):
    register(runtime)
    run_id = runtime.submit("test.run", {})
    original = runtime.get_run
    reads = []

    def get(value):
        row = original(value)
        reads.append(value)
        if len(reads) == 1:
            assert runtime.cancel(run_id)
        return row

    monkeypatch.setattr(runtime, "get_run", get)
    started = time.monotonic()
    assert runtime.wait(run_id, 0.2)["status"] == "skipped"
    assert time.monotonic() - started < 0.15
    assert len(reads) == 2
    assert runtime._waiters == {}


@pytest.mark.parametrize("receipt", [None, {}, {"reason": "process_exited"}])
def test_missing_business_receipt_fails_closed_to_unknown(runtime, receipt):
    register(runtime, lambda payload, context: receipt)
    run_id = runtime.submit("test.run", {})
    runtime.start()
    row = runtime.wait(run_id, 2)
    assert row["status"] == "unknown"
    assert row["reason"] == "handler_receipt_missing"


def test_invalid_business_receipt_is_never_success(runtime):
    register(runtime, lambda payload, context: "exit code zero")
    run_id = runtime.submit("test.run", {})
    runtime.start()
    assert runtime.wait(run_id, 2)["reason"] == "handler_receipt_invalid"


def test_public_enable_rearms_disabled_module_timer_without_adapter_restart(runtime):
    register(runtime)
    runs, states = [], []

    def lifecycle(enabled):
        states.append(enabled)
        if enabled:
            runtime.add_timer(
                "next",
                time.time() + 0.05,
                lambda: runs.append(
                    runtime.submit("test.run", {}, request_id="maturity")
                ),
                module_id="test",
            )

    runtime.bind_lifecycle("test", lifecycle)
    runtime.enable("test")
    runtime.disable("test")
    runtime.start()
    assert runs == []
    runtime.enable("test")
    eventually(lambda: runs)
    assert runtime.wait(runs[0], 2)["status"] == "succeeded"
    assert states == [True, False, True]


def test_lifecycle_callback_does_not_hold_host_lock(runtime):
    register(runtime)
    completed = threading.Event()
    workers = []

    def lifecycle(enabled):
        thread = threading.Thread(
            target=lambda: (
                runtime.add_timer("worker", time.time() + 60, lambda: None),
                completed.set(),
            )
        )
        workers.append(thread)
        thread.start()
        assert completed.wait(1)

    runtime.bind_lifecycle("test", lifecycle)
    try:
        runtime.enable("test")
        assert completed.is_set()
    finally:
        for worker in workers:
            worker.join(2)


def test_lifecycle_failure_is_visible_and_does_not_leave_half_enabled_module(runtime):
    register(runtime)

    def broken(enabled):
        runtime.add_timer("partial", time.time() + 60, lambda: None, module_id="test")
        raise ValueError("private configuration")

    runtime.bind_lifecycle("test", broken)
    with pytest.raises(RuntimeError, match="lifecycle_failed:test:ValueError"):
        runtime.enable("test")
    assert not runtime.module_status("test")["enabled"]
    assert runtime.health()["failure"] == "lifecycle_failed:test:ValueError"
    runtime.bind_lifecycle("test", lambda enabled: None)
    runtime.enable("test")
    assert runtime.health()["failure"] == ""


def test_disabled_admission_rolls_back_receipt_and_keeps_existing_duplicate(runtime):
    register(runtime)
    first = runtime.submit("test.run", {}, request_id="accepted")
    runtime.disable("test")
    assert runtime.store.submit("test.run", {}, request_id="accepted") == first
    with runtime.store.connection() as db:
        db.execute("CREATE TABLE receipts(id TEXT PRIMARY KEY)")
    with pytest.raises(ModuleDisabled), runtime.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO receipts VALUES ('new')")
        runtime.store.submit("test.run", {}, request_id="new", db=db)
    assert runtime.store.count_pending("test.run") == 1
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert runtime.get_run(first)["status"] == "queued"


def test_manual_tick_then_start_if_unowned_does_not_leave_ghost_owner(runtime):
    register(runtime)
    runtime.tick()
    assert runtime.store.has_live_owner()
    assert runtime.start_if_unowned()
    assert len(runtime.store.active_owners()) == 1
    runtime.stop()
    assert not runtime.store.has_live_owner()


def test_stop_incomplete_dispatcher_cannot_be_reactivated_into_two_loops(runtime):
    started, release = threading.Event(), threading.Event()

    def callback():
        started.set()
        release.wait(3)

    runtime.add_timer("inflight", time.time(), callback)
    runtime.start()
    try:
        assert started.wait(2)
        runtime.stop(drain=False, timeout=0)
        with pytest.raises(RuntimeError, match="dispatcher"):
            runtime.start()
    finally:
        release.set()
        eventually(lambda: not runtime._thread.is_alive())
    assert runtime.start_if_unowned()


def test_conversation_backlog_does_not_take_next_slot_ahead_of_new_session(store):
    store.apply(ModuleSpec("test", (JobSpec("work", lambda p, c: None),)))
    store.owner_started("owner")
    a1 = store.submit("work", {}, session_key="A")
    a2 = store.submit("work", {}, session_key="A")
    b = store.submit("work", {}, session_key="B")
    c = store.submit("work", {}, session_key="C")
    first = store.claim("owner", "default", ["work"], lane_capacity=2)
    assert first["id"] == a1
    assert store.claim("owner", "default", ["work"], lane_capacity=2)["id"] == b
    store.finish(a1, first["claim_token"], RunResult())
    assert store.claim("owner", "default", ["work"], lane_capacity=2)["id"] == c
    assert store.get(a2)["status"] == "queued"


def test_duplicate_progress_is_coalesced_but_effect_intents_are_always_committed(
    runtime, monkeypatch
):
    contexts = []

    def handle(payload, context):
        contexts.append(context)
        assert context.progress("model")
        assert context.progress("model")
        assert context.mark_side_effect("tool")
        assert context.mark_side_effect("tool")
        return RunResult()

    progress, release = [], []
    original_progress = runtime.store.progress
    original_release = runtime.store.release

    def track_progress(*args, **kwargs):
        progress.append((args, kwargs))
        return original_progress(*args, **kwargs)

    def track_release(*args):
        release.append(args)
        return original_release(*args)

    monkeypatch.setattr(runtime.store, "progress", track_progress)
    monkeypatch.setattr(runtime.store, "release", track_release)
    register(runtime, handle)
    run_id = runtime.submit("test.run", {})
    runtime.start()
    assert runtime.wait(run_id, 3)["status"] == "succeeded"
    assert len(progress) == 3
    assert len([kwargs for _, kwargs in progress if kwargs.get("side_effect")]) == 2
    assert release == []
    contexts[0].cancel_event.set()
    assert not contexts[0].progress("model")


def test_shared_connection_provider_preserves_business_full_and_database_isolation(
    tmp_path,
):
    from src.personal_monitor import MonitorStore

    execution = ExecutionStore(tmp_path / "execution.sqlite3")
    facts = MonitorStore(tmp_path / "facts.sqlite3")
    facts.set_connection_provider(execution.connection)
    with facts.connection() as first:
        assert first.isolation_level is None
        assert first.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert first.execute("PRAGMA cache_size").fetchone()[0] == -64
        assert first.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with facts.connection() as second:
        assert second is first
    with pytest.raises(RuntimeError), facts.connection(write=True) as db:
        db.execute("INSERT INTO routes VALUES('owner','not-committed')")
        raise RuntimeError("rollback")
    assert facts.allowed_routes("owner") == []
    with execution.connection() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='routes'").fetchone()
            is None
        )
    execution.close_idle_connections()
    facts.register_route("owner", "durable")
    assert facts.allowed_routes("owner") == ["durable"]


def test_business_read_nested_in_same_database_admission_never_commits_outer(tmp_path):
    from src.personal_monitor import MonitorStore

    path = tmp_path / "shared.sqlite3"
    execution = ExecutionStore(path)
    facts = MonitorStore(path)
    facts.set_connection_provider(execution.connection)
    with execution.connection() as outer:
        outer.execute("BEGIN IMMEDIATE")
        outer.execute("INSERT INTO routes VALUES('owner','not-committed')")
        assert facts.allowed_routes("owner") == []
        assert outer.in_transaction
        outer.rollback()
    assert facts.allowed_routes("owner") == []
    with facts.connection(write=True) as outer:
        outer.execute("INSERT INTO routes VALUES('owner','also-not-committed')")
        assert facts.allowed_routes("owner") == []
        assert outer.in_transaction
        outer.rollback()
    assert facts.allowed_routes("owner") == []
    assert len(execution._connections) <= 4


def test_profile_injection_does_not_bind_an_unrelated_store(tmp_path):
    from src.personal_monitor import MonitorStore
    from src.personal_monitor_runtime import PersonalMonitorRuntime

    profile = tmp_path / "current"
    host = ExecutionRuntime(ExecutionStore(profile / "execution.sqlite3"))
    current = MonitorStore(profile / "facts.sqlite3")
    other = MonitorStore(tmp_path / "other" / "facts.sqlite3")
    monitor = PersonalMonitorRuntime(
        current, {}, lambda *_: None, execution_runtime=host
    )
    monitor._registered_tasks()
    current.register_route("owner", "current")
    other.register_route("owner", "other")
    assert other._connection_provider is None
    assert current.allowed_routes("owner") == ["current"]
    assert other.allowed_routes("owner") == ["other"]
    paths = {key[1] for key in host.store._connections}
    assert os.path.normcase(str((profile / "facts.sqlite3").resolve())) in paths
    assert (
        os.path.normcase(str((tmp_path / "other/facts.sqlite3").resolve())) not in paths
    )


def test_filtered_business_cache_cleanup_never_closes_active_borrow(tmp_path):
    from src.personal_monitor import MonitorStore

    execution = ExecutionStore(tmp_path / "execution.sqlite3")
    facts = MonitorStore(tmp_path / "facts.sqlite3")
    facts.set_connection_provider(execution.connection)
    borrowed, release = threading.Event(), threading.Event()
    failures = []

    def work():
        try:
            with facts.connection(write=True) as db:
                db.execute("INSERT INTO routes VALUES('owner','durable')")
                borrowed.set()
                assert release.wait(5)
                assert db.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 1
        except BaseException as error:  # noqa: BLE001 - surface worker assertions in the caller
            failures.append(error)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert borrowed.wait(5)
        execution.close_idle_connections(database=facts.path, autocommit=True)
        release.set()
        worker.join(5)
        assert not worker.is_alive() and not failures
        assert facts.allowed_routes("owner") == ["durable"]
        with execution.connection() as host_db:
            execution.close_idle_connections(database=facts.path, autocommit=True)
            assert host_db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        release.set()
        worker.join(5)


@pytest.mark.parametrize("injected", [False, True])
def test_business_read_context_never_commits_an_unrequested_manual_transaction(
    tmp_path, injected
):
    from src.personal_monitor import MonitorStore

    facts = MonitorStore(tmp_path / "facts.sqlite3")
    execution = ExecutionStore(tmp_path / "execution.sqlite3")
    if injected:
        facts.set_connection_provider(execution.connection)
    with facts.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO routes VALUES('owner','not-requested')")
    assert facts.allowed_routes("owner") == []
