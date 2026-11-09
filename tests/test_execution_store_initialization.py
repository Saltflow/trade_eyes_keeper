"""Real SQLite initialization races; never execute an admitted business job."""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.task_runtime import execution
from src.task_runtime.execution import ExecutionStore, JobSpec, ModuleSpec, RunResult


@pytest.mark.parametrize("existing", [False, True])
def test_wal_transition_recovers_real_busy_after_reader_releases(
    tmp_path, monkeypatch, existing
):
    path = tmp_path / "execution.sqlite3"
    blocker = sqlite3.connect(path)
    if existing:
        blocker.execute("CREATE TABLE preserved(value TEXT)")
        blocker.execute("INSERT INTO preserved VALUES('old evidence')")
        blocker.commit()
    blocker.execute("BEGIN")
    blocker.execute("SELECT name FROM sqlite_master").fetchall()
    busy = threading.Event()
    original_connect = sqlite3.connect
    open_connection = ExecutionStore._open_connection

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.OperationalError:
                if sql == "PRAGMA journal_mode=WAL":
                    busy.set()
                raise

    def connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        return original_connect(*args, **kwargs)

    def short_native_wait(self, database, autocommit):
        db = open_connection(self, database, autocommit)
        # Simulate the immediate SQLITE_BUSY returned by a journal lock upgrade.
        # The error still comes from a real SQLite lock, never a fake exception.
        db.execute("PRAGMA busy_timeout=1")
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(ExecutionStore, "_open_connection", short_native_wait)
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(ExecutionStore, path)
            try:
                assert busy.wait(5), "the test must observe an actual WAL lock conflict"
            finally:
                blocker.rollback()
            store = pending.result(timeout=5)
        with store.connection() as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
            if existing:
                assert (
                    db.execute("SELECT value FROM preserved").fetchone()[0]
                    == "old evidence"
                )
        store.close_idle_connections()
    finally:
        blocker.close()


@pytest.mark.parametrize("existing_wal", [False, True])
@pytest.mark.parametrize("round_number", range(3))
def test_parallel_store_initializers_preserve_requests_and_schema(
    tmp_path, existing_wal, round_number
):
    path = tmp_path / f"execution-{round_number}.sqlite3"
    job = JobSpec("initialize.check", lambda *_: RunResult())
    module = ModuleSpec("initialize", (job,))
    if existing_wal:
        previous = ExecutionStore(path)
        previous.apply(module)
        previous.submit(
            job.id, {"value": "old evidence"}, request_id="existing", source="test"
        )
        previous.close_idle_connections()
    barrier = threading.Barrier(8)

    def initialize(number):
        barrier.wait(timeout=5)
        store = ExecutionStore(path)
        try:
            assert store.apply(module) == 1
            return store.submit(
                job.id, {"number": number}, request_id=str(number), source="test"
            )
        finally:
            store.close_idle_connections()

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(initialize, range(8)))
    assert len(set(results)) == 8
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
            == 8 + existing_wal
        )
        assert db.execute("SELECT DISTINCT status FROM execution_runs").fetchall() == [
            ("queued",)
        ]
        if existing_wal:
            assert (
                "old evidence"
                in db.execute(
                    "SELECT payload FROM execution_runs WHERE request_id='existing'"
                ).fetchone()[0]
            )


def test_wal_initialization_busy_budget_expires_and_restores_native_wait(
    tmp_path, monkeypatch
):
    path = tmp_path / "blocked.sqlite3"
    blocker = sqlite3.connect(path)
    blocker.execute("CREATE TABLE preserved(value)")
    blocker.commit()
    blocker.execute("BEGIN")
    blocker.execute("SELECT * FROM preserved").fetchall()
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=1")
    monkeypatch.setattr(execution, "_STORE_TIMEOUT_SECONDS", 0.1)
    statements = []
    db.set_trace_callback(statements.append)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            ExecutionStore._initialize_journal(db)
        assert time.monotonic() - started < 2
        assert statements.count("PRAGMA journal_mode=WAL") >= 2
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert not db.in_transaction
    finally:
        blocker.rollback()
        blocker.close()
        db.close()


def test_wal_initialization_does_not_retry_non_busy_errors(tmp_path):
    path = tmp_path / "readonly.sqlite3"
    with sqlite3.connect(path) as initial:
        initial.execute("CREATE TABLE preserved(value)")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA busy_timeout=777")
    statements = []
    db.set_trace_callback(statements.append)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ExecutionStore._initialize_journal(db)
        assert statements.count("PRAGMA journal_mode=WAL") == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 777
    finally:
        db.close()
