"""Shared, bounded execution without business imports or a network listener.

SQLite is the execution fact; the in-memory event is only a wake-up hint. A
handler must bound network calls and honour ``cancel_event``. Python threads
cannot be forcibly killed: timed-out handlers retain their concurrency hold
until they return or their owning process is proved dead.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
TERMINAL = frozenset({"succeeded", "skipped", "failed", "partial", "unknown"})
DEFAULT_LANES = {"conversation": 2, "personal": 2, "report": 1, "default": 1}


class QueueFull(RuntimeError):
    """Admission failed before an execution item was created."""


class ModuleDisabled(RuntimeError):
    """A registered module stopped accepting new requests."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _timestamp(value: datetime | str | float | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must have a timezone")
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError("deadline must be an aware datetime or epoch seconds")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("deadline must be finite")
    return result


def _identity(pid: int) -> str | None:
    """Read birth identity; None is proven absent, '?' is access-limited alive."""
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return "?" if ctypes.get_last_error() == 5 else None
        try:
            created = ctypes.c_ulonglong()
            exited = ctypes.c_ulonglong()
            system = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
            if kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(system),
                ctypes.byref(user),
            ):
                return None if exited.value else str(created.value)
            return "?"
        finally:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(handle)
    try:
        # starttime is field 22; comm may contain spaces or parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
            return "?"
        except PermissionError:
            return "?"
        except ProcessLookupError:
            return None


@dataclass(frozen=True)
class RunResult:
    status: str = "succeeded"
    reason: str = ""
    artifacts: Any = None
    deliveries: Any = None

    def __post_init__(self) -> None:
        if self.status not in TERMINAL:
            raise ValueError(f"invalid result status: {self.status}")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "artifacts": self.artifacts,
            "deliveries": self.deliveries,
        }

    @classmethod
    def from_dict(cls, value: dict) -> RunResult:
        return cls(
            **{key: value[key] for key in cls.__dataclass_fields__ if key in value}
        )


@dataclass(frozen=True)
class JobSpec:
    id: str
    handler: Callable[[dict, RunContext], RunResult]
    lane: str = "default"
    concurrency_key: str | None = None
    timeout_seconds: float = 300
    recovery: str = "unknown"
    starting_side_effect: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.lane or not callable(self.handler):
            raise ValueError("job id, lane and callable handler are required")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.recovery != "unknown":
            raise ValueError("only safe 'unknown' running recovery is supported")
        if self.starting_side_effect is not None and (
            not isinstance(self.starting_side_effect, str)
            or not self.starting_side_effect
            or len(self.starting_side_effect) > 160
        ):
            raise ValueError("starting side-effect stage must be 1 to 160 characters")


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    jobs: tuple[JobSpec, ...]
    version: str = "1"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id or not self.version:
            raise ValueError("module id and version are required")
        if len({job.id for job in self.jobs}) != len(self.jobs):
            raise ValueError("duplicate job id in module")


@dataclass
class _StoreConnection:
    db: sqlite3.Connection
    borrowed: bool = False


class ExecutionStore:
    """Short transactions, optionally sharing an existing admission transaction."""

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection_lock = threading.Lock()
        self._connections: dict[tuple[int, str, bool], _StoreConnection] = {}
        self._connection_pid = os.getpid()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS execution_modules (
                    id TEXT PRIMARY KEY, version TEXT NOT NULL,
                    generation INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_jobs (
                    id TEXT PRIMARY KEY, module_id TEXT NOT NULL,
                    lane TEXT NOT NULL, concurrency_key TEXT,
                    timeout_seconds REAL NOT NULL, enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_owners (
                    id TEXT PRIMARY KEY, pid INTEGER NOT NULL, birth TEXT,
                    state TEXT NOT NULL, heartbeat REAL NOT NULL,
                    started REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_sessions (
                    session_key TEXT PRIMARY KEY, last_claimed REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_runs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    job_id TEXT NOT NULL, module_id TEXT NOT NULL DEFAULT '',
                    config_version TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT, session_key TEXT, source TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    available_at REAL NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, started REAL, finished REAL,
                    owner_id TEXT, claim_token TEXT, lease_until REAL,
                    progress_at REAL, stage TEXT NOT NULL DEFAULT 'queued',
                    side_effect INTEGER NOT NULL DEFAULT 0,
                    concurrency_key TEXT, released_at REAL,
                    result TEXT, reason TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS execution_request
                    ON execution_runs(job_id,request_id) WHERE request_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS execution_pending
                    ON execution_runs(status,available_at,seq);
                CREATE INDEX IF NOT EXISTS execution_session
                    ON execution_runs(session_key,seq,status);
                CREATE INDEX IF NOT EXISTS execution_owner
                    ON execution_runs(owner_id,status);
                CREATE INDEX IF NOT EXISTS execution_hold
                    ON execution_runs(concurrency_key,released_at);
                COMMIT;
            """)
        if os.name != "nt":
            self.path.chmod(0o600)
        # Construction commonly happens on a thread that never executes work.
        self.close_idle_connections()

    @contextmanager
    def connection(self, *, database=None, autocommit=False):
        # Reopening the last WAL connection for every SELECT/commit repeatedly
        # parses the schema and checkpoints the WAL. Reuse a bounded number of
        # connections; no bookkeeping lock spans SQL or an adapter callback.
        database = os.path.normcase(os.path.abspath(database or self.path))
        identity = (threading.get_ident(), database, autocommit)
        with self._connection_lock:
            if self._connection_pid != os.getpid():
                # A fork must not borrow a parent's SQLite connection.
                for previous in self._connections.values():
                    previous.db.close()
                self._connections.clear()
                self._connection_pid = os.getpid()
            cached = self._connections.get(identity)
            if cached is None:
                # A returned handle has no thread affinity (check_same_thread=False).
                # Reuse an idle compatible handle before evicting and reopening it.
                for previous_id, previous in list(self._connections.items()):
                    if (
                        previous_id[1:] == identity[1:]
                        and not previous.borrowed
                        and not previous.db.in_transaction
                    ):
                        cached = self._connections.pop(previous_id)
                        self._connections[identity] = cached
                        break
            if cached is None and len(self._connections) >= 4:
                for previous_id, previous in list(self._connections.items()):
                    if not previous.borrowed:
                        previous.db.close()
                        del self._connections[previous_id]
                        break
            if cached is None and len(self._connections) < 4:
                cached = _StoreConnection(self._open_connection(database, autocommit))
                self._connections[identity] = cached
            if cached is not None and not cached.borrowed:
                cached.borrowed = True
                self._connections.pop(identity)
                self._connections[identity] = cached
                db = cached.db
            else:
                # Nested borrowing keeps its old independent-transaction
                # semantics; it must never commit an outer admission transaction.
                cached = None
                db = self._open_connection(database, autocommit)
        try:
            if autocommit:
                try:
                    yield db
                finally:
                    # The business adapter explicitly commits write=True.
                    # A read context must retain close-on-exit rollback semantics.
                    if db.in_transaction:
                        db.rollback()
            else:
                with db:
                    yield db
        finally:
            if cached is None:
                db.close()
            else:
                with self._connection_lock:
                    cached.borrowed = False

    def _open_connection(self, database, autocommit):
        timeout = 30 if autocommit else 10
        db = sqlite3.connect(
            database,
            timeout=timeout,
            check_same_thread=False,
            cached_statements=16,
            isolation_level=None if autocommit else "",
        )
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={timeout * 1000}")
        db.execute("PRAGMA synchronous=FULL")
        # Four cached handles must not each retain SQLite's default 2 MiB.
        db.execute("PRAGMA cache_size=-64")
        return db

    def close_idle_connections(self, *, database=None, autocommit=None) -> None:
        """Release cached SQLite state after drain without closing active work."""
        if database is not None:
            database = os.path.normcase(os.path.abspath(database))
        with self._connection_lock:
            for identity, cached in list(self._connections.items()):
                if database is not None and identity[1] != database:
                    continue
                if autocommit is not None and identity[2] != autocommit:
                    continue
                if not cached.borrowed:
                    cached.db.close()
                    del self._connections[identity]

    def __del__(self):
        try:
            self.close_idle_connections()
        except (AttributeError, sqlite3.Error):
            pass

    def apply(self, module: ModuleSpec, preserve_enabled: bool = False) -> int:
        fingerprint = _json(
            [
                [j.id, j.lane, j.concurrency_key, j.timeout_seconds, j.recovery]
                + (
                    [j.starting_side_effect]
                    if j.starting_side_effect is not None
                    else []
                )
                for j in module.jobs
            ]
        )
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM execution_modules WHERE id=?", (module.id,)
            ).fetchone()
            changed = bool(
                old
                and (
                    old["version"] != module.version
                    or old["fingerprint"] != fingerprint
                )
            )
            generation = (old["generation"] + int(changed)) if old else 1
            enabled = old["enabled"] if old and preserve_enabled else module.enabled
            for job in module.jobs:
                previous = db.execute(
                    "SELECT module_id FROM execution_jobs WHERE id=?", (job.id,)
                ).fetchone()
                if previous and previous[0] != module.id:
                    raise ValueError("job id already belongs to another module")
            db.execute(
                "INSERT INTO execution_modules VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
                "generation=excluded.generation,enabled=excluded.enabled,"
                "fingerprint=excluded.fingerprint,updated=excluded.updated",
                (
                    module.id,
                    module.version,
                    generation,
                    enabled,
                    fingerprint,
                    now,
                ),
            )
            db.execute(
                "UPDATE execution_jobs SET enabled=0 WHERE module_id=?", (module.id,)
            )
            for job in module.jobs:
                db.execute(
                    "INSERT INTO execution_jobs VALUES (?,?,?,?,?,1) "
                    "ON CONFLICT(id) DO UPDATE SET lane=excluded.lane,"
                    "concurrency_key=excluded.concurrency_key,"
                    "timeout_seconds=excluded.timeout_seconds,enabled=1",
                    (
                        job.id,
                        module.id,
                        job.lane,
                        job.concurrency_key,
                        job.timeout_seconds,
                    ),
                )
            if changed:
                # Only timer requests are superseded. Explicit/manual admitted
                # work retains its payload/configuration snapshot and order.
                db.execute(
                    "UPDATE execution_runs SET status='skipped',reason='superseded',"
                    "finished=?,updated=? WHERE module_id=? AND status='queued' "
                    "AND source IN ('schedule','scheduled','timer')",
                    (now, now, module.id),
                )
        return generation

    def set_enabled(self, module_id: str, enabled: bool) -> None:
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE execution_modules SET enabled=?,updated=? WHERE id=?",
                (bool(enabled), time.time(), module_id),
            )
            if not cursor.rowcount:
                raise KeyError(module_id)

    def submit(
        self,
        job_id: str,
        payload: dict | None = None,
        request_id: str | None = None,
        session_key: str | None = None,
        module_id: str = "",
        config_version: str = "",
        source: str = "",
        db: sqlite3.Connection | None = None,
        not_before: datetime | str | float | None = None,
        max_pending: int | None = None,
    ) -> str:
        if not job_id or (request_id is not None and not request_id):
            raise ValueError("job_id and nonempty request_id are required")
        if not isinstance(payload or {}, dict):
            raise TypeError("payload must be a dict")
        encoded = _json(payload or {})
        due = _timestamp(not_before)

        def insert(connection):
            if request_id is not None:
                old = connection.execute(
                    "SELECT id,payload,session_key FROM execution_runs "
                    "WHERE job_id=? AND request_id=?",
                    (job_id, request_id),
                ).fetchone()
                if old:
                    if old[1] != encoded or old[2] != session_key:
                        raise ValueError("request_id already has different input")
                    return old[0]
            spec = connection.execute(
                "SELECT j.module_id,m.version,m.generation,m.enabled,j.enabled "
                "FROM execution_jobs j "
                "JOIN execution_modules m ON m.id=j.module_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
            if spec and not (spec[3] and spec[4]):
                raise ModuleDisabled("module is not accepting new requests")
            if max_pending is not None:
                if max_pending < 1:
                    raise ValueError("max_pending must be positive")
                if self.count_pending(job_id, db=connection) >= max_pending:
                    raise QueueFull("execution queue is full")
            selected_module = module_id or (spec[0] if spec else "")
            version = config_version or (spec[1] if spec else "")
            if spec and selected_module != spec[0]:
                raise ValueError("module_id does not match job registration")
            generation = spec[2] if spec else 0
            stale = bool(spec and config_version and config_version != spec[1])
            run_id, now = uuid.uuid4().hex, time.time()
            connection.execute(
                "INSERT INTO execution_runs "
                "(id,job_id,module_id,config_version,generation,request_id,session_key,"
                "source,payload,status,available_at,created,updated,reason,finished) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    job_id,
                    selected_module,
                    version,
                    generation,
                    request_id,
                    session_key,
                    source,
                    encoded,
                    "skipped" if stale else "queued",
                    due,
                    now,
                    now,
                    "stale_configuration" if stale else "",
                    now if stale else None,
                ),
            )
            return run_id

        if db is not None:
            # Never BEGIN, commit or rollback somebody else's transaction.
            return insert(db)
        with self.connection() as owned:
            owned.execute("BEGIN IMMEDIATE")
            return insert(owned)

    @staticmethod
    def _decode(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def get(self, run_id: str) -> dict | None:
        with self.connection() as db:
            return self._decode(
                db.execute(
                    "SELECT * FROM execution_runs WHERE id=?", (run_id,)
                ).fetchone()
            )

    get_run = get

    def count_pending(self, job_id: str, db: sqlite3.Connection | None = None) -> int:
        def count(connection):
            return connection.execute(
                "SELECT COUNT(*) FROM execution_runs WHERE job_id=? AND status='queued'",
                (job_id,),
            ).fetchone()[0]

        if db is not None:
            return count(db)
        with self.connection() as connection:
            return count(connection)

    def module_status(self, module_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM execution_modules WHERE id=?", (module_id,)
            ).fetchone()
            return dict(row) if row else None

    def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        return self.list_runs(status=status, limit=limit)

    def list_runs(
        self, job_id: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict]:
        limit = max(1, min(int(limit), 10000))
        query, args, filters = "SELECT * FROM execution_runs", [], []
        if job_id:
            filters.append("job_id=?")
            args.append(job_id)
        if status:
            filters.append("status=?")
            args.append(status)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        with self.connection() as db:
            rows = db.execute(query + " ORDER BY seq DESC LIMIT ?", [*args, limit])
            return [self._decode(row) for row in rows]

    def status(self) -> dict:
        with self.connection() as db:
            counts = dict(
                db.execute("SELECT status,COUNT(*) FROM execution_runs GROUP BY status")
            )
            oldest = db.execute(
                "SELECT MIN(available_at) FROM execution_runs "
                "WHERE status='queued' AND available_at<=?",
                (time.time(),),
            ).fetchone()[0]
            modules = [dict(r) for r in db.execute("SELECT * FROM execution_modules")]
        return {
            "counts": counts,
            "oldest_wait_seconds": max(0, time.time() - oldest) if oldest else 0,
            "modules": modules,
        }

    def owner_started(self, owner_id: str, only_if_unowned: bool = False) -> bool:
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if only_if_unowned:
                owners = db.execute(
                    "SELECT * FROM execution_owners WHERE state='running' AND id<>?",
                    (owner_id,),
                ).fetchall()
                if any(self._owner_alive(dict(owner)) for owner in owners):
                    return False
            db.execute(
                "INSERT INTO execution_owners VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state='running',heartbeat=excluded.heartbeat",
                (owner_id, os.getpid(), _identity(os.getpid()), "running", now, now),
            )
        return True

    def owner_stopped(self, owner_id: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE execution_owners SET state='stopped',heartbeat=? WHERE id=?",
                (time.time(), owner_id),
            )

    def active_owners(self) -> list[dict]:
        with self.connection() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM execution_owners WHERE state='running'"
                )
            ]
        return [row for row in rows if self._owner_alive(row)]

    def has_live_owner(self) -> bool:
        return bool(self.active_owners())

    @staticmethod
    def _owner_alive(owner: dict) -> bool:
        actual = _identity(owner["pid"])
        return bool(actual and (actual == "?" or actual == owner["birth"]))

    def recover_offline(self) -> int:
        """Recover only proved-dead/stopped owners, never stale-but-live PIDs."""
        with self.connection() as db:
            candidates = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM execution_owners WHERE id IN "
                    "(SELECT DISTINCT owner_id FROM execution_runs "
                    "WHERE claim_token IS NOT NULL AND released_at IS NULL)"
                )
            ]
        dead = [
            r["id"]
            for r in candidates
            if r["state"] == "stopped" or not self._owner_alive(r)
        ]
        count, now = 0, time.time()
        for owner_id in dead:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT * FROM execution_owners WHERE id=?", (owner_id,)
                ).fetchone()
                if (
                    current
                    and current["state"] == "running"
                    and self._owner_alive(dict(current))
                ):
                    continue
                rows = db.execute(
                    "SELECT id,status FROM execution_runs WHERE owner_id=? "
                    "AND claim_token IS NOT NULL AND released_at IS NULL",
                    (owner_id,),
                ).fetchall()
                for row in rows:
                    if row["status"] == "running":
                        result = RunResult("unknown", "owner_offline").to_dict()
                        db.execute(
                            "UPDATE execution_runs SET status='unknown',result=?,"
                            "reason='owner_offline',finished=?,updated=? WHERE id=?",
                            (_json(result), now, now, row["id"]),
                        )
                        count += 1
                    db.execute(
                        "UPDATE execution_runs SET claim_token=NULL,released_at=? "
                        "WHERE id=?",
                        (now, row["id"]),
                    )
                db.execute(
                    "UPDATE execution_owners SET state='offline' WHERE id=?",
                    (owner_id,),
                )
        return count

    def claim(
        self,
        owner_id: str,
        lane: str,
        job_ids: list[str],
        lease_seconds: float = 30,
        lane_capacity: int = 1,
        starting_side_effects: dict[str, str] | None = None,
    ) -> dict | None:
        if not job_ids:
            return None
        now, token = time.time(), uuid.uuid4().hex
        marks = ",".join("?" for _ in job_ids)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT COUNT(*) FROM execution_runs r JOIN execution_jobs j "
                "ON j.id=r.job_id WHERE j.lane=? AND r.claim_token IS NOT NULL "
                "AND r.released_at IS NULL",
                (lane,),
            ).fetchone()[0]
            if active >= lane_capacity:
                return None
            row = db.execute(
                "SELECT r.*,j.concurrency_key AS job_key FROM execution_runs r "
                "JOIN execution_jobs j ON j.id=r.job_id "
                "JOIN execution_modules m ON m.id=j.module_id "
                "LEFT JOIN execution_sessions s ON s.session_key=r.session_key "
                f"WHERE r.status='queued' AND r.available_at<=? AND j.id IN ({marks}) "
                "AND j.lane=? AND j.enabled=1 AND m.enabled=1 "
                "AND NOT EXISTS (SELECT 1 FROM execution_runs p "
                "WHERE r.session_key IS NOT NULL AND p.session_key=r.session_key "
                "AND p.seq<r.seq AND (p.status='queued' OR "
                "(p.claim_token IS NOT NULL AND p.released_at IS NULL))) "
                "AND NOT EXISTS (SELECT 1 FROM execution_runs p "
                "WHERE j.concurrency_key IS NOT NULL "
                "AND p.concurrency_key=j.concurrency_key "
                "AND p.claim_token IS NOT NULL AND p.released_at IS NULL) "
                "ORDER BY COALESCE(s.last_claimed,0),r.seq LIMIT 1",
                (now, *job_ids, lane),
            ).fetchone()
            if not row:
                return None
            if row["session_key"] is not None:
                db.execute(
                    "INSERT INTO execution_sessions VALUES (?,?) "
                    "ON CONFLICT(session_key) DO UPDATE "
                    "SET last_claimed=excluded.last_claimed",
                    (row["session_key"], now),
                )
            db.execute(
                "UPDATE execution_runs SET status='running',started=?,updated=?,"
                "progress_at=?,owner_id=?,claim_token=?,lease_until=?,"
                "concurrency_key=?,side_effect=MAX(side_effect,?),stage=? "
                "WHERE id=? AND status='queued'",
                (
                    now,
                    now,
                    now,
                    owner_id,
                    token,
                    now + lease_seconds,
                    row["job_key"],
                    int(row["job_id"] in (starting_side_effects or {})),
                    (starting_side_effects or {}).get(row["job_id"], "starting"),
                    row["id"],
                ),
            )
            result = self._decode(
                db.execute(
                    "SELECT * FROM execution_runs WHERE id=?", (row["id"],)
                ).fetchone()
            )
        return result

    def progress(
        self, run_id: str, token: str, stage: str, side_effect: bool = False
    ) -> bool:
        with self.connection() as db:
            return bool(
                db.execute(
                    "UPDATE execution_runs SET stage=?,progress_at=?,updated=?,"
                    "side_effect=MAX(side_effect,?) WHERE id=? AND claim_token=? "
                    "AND status='running'",
                    (
                        stage[:160],
                        time.time(),
                        time.time(),
                        int(side_effect),
                        run_id,
                        token,
                    ),
                ).rowcount
            )

    def finish(self, run_id: str, token: str, result: RunResult) -> bool:
        now = time.time()
        encoded = _json(result.to_dict())
        with self.connection() as db:
            return bool(
                db.execute(
                    "UPDATE execution_runs SET status=?,result=?,reason=?,finished=?,"
                    "updated=?,released_at=?,stage='finished' WHERE id=? "
                    "AND claim_token=? AND status='running'",
                    (
                        result.status,
                        encoded,
                        result.reason,
                        now,
                        now,
                        now,
                        run_id,
                        token,
                    ),
                ).rowcount
            )

    def interrupt(self, run_id: str, token: str, reason: str) -> bool:
        """Invalidate result writes but retain the hold until the handler exits."""
        now = time.time()
        with self.connection() as db:
            return bool(
                db.execute(
                    "UPDATE execution_runs SET status='unknown',result=?,reason=?,"
                    "finished=?,updated=?,stage='cancellation_requested' "
                    "WHERE id=? AND claim_token=? AND status='running'",
                    (
                        _json(RunResult("unknown", reason).to_dict()),
                        reason,
                        now,
                        now,
                        run_id,
                        token,
                    ),
                ).rowcount
            )

    def release(self, run_id: str, token: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE execution_runs SET released_at=? WHERE id=? "
                "AND claim_token=? AND status<>'running' AND released_at IS NULL",
                (time.time(), run_id, token),
            )

    def heartbeat(self, owner_id: str, lease_seconds: float = 30) -> None:
        now = time.time()
        with self.connection() as db:
            db.execute(
                "UPDATE execution_owners SET heartbeat=? WHERE id=? AND state='running'",
                (now, owner_id),
            )
            db.execute(
                "UPDATE execution_runs SET lease_until=? WHERE owner_id=? "
                "AND status='running'",
                (now + lease_seconds, owner_id),
            )

    def ready_jobs(self, job_ids: list[str]) -> set[str]:
        if not job_ids:
            return set()
        marks = ",".join("?" for _ in job_ids)
        with self.connection() as db:
            rows = db.execute(
                "SELECT DISTINCT r.job_id FROM execution_runs r "
                "JOIN execution_jobs j ON j.id=r.job_id "
                "JOIN execution_modules m ON m.id=j.module_id "
                f"WHERE r.status='queued' AND r.job_id IN ({marks}) "
                "AND r.available_at<=? AND j.enabled=1 AND m.enabled=1",
                (*job_ids, time.time()),
            )
            return {row[0] for row in rows}

    def has_ready(self, job_ids: list[str]) -> bool:
        return bool(self.ready_jobs(job_ids))

    def cancel_queued(self, run_id: str) -> bool:
        now = time.time()
        with self.connection() as db:
            return bool(
                db.execute(
                    "UPDATE execution_runs SET status='skipped',reason='cancelled',"
                    "result=?,updated=?,finished=? WHERE id=? AND status='queued'",
                    (
                        _json(RunResult("skipped", "cancelled").to_dict()),
                        now,
                        now,
                        run_id,
                    ),
                ).rowcount
            )


class RunContext:
    def __init__(self, store: ExecutionStore, row: dict):
        self.run_id = row["id"]
        self.job_id = row["job_id"]
        self.module_id = row["module_id"]
        self.config_version = row["config_version"]
        self.cancel_event = threading.Event()
        self._store, self._token = store, row["claim_token"]
        self._starting_side_effect = (
            row.get("stage", "") if row.get("side_effect") else None
        )
        self._last_stage = self._starting_side_effect or ""
        self._last_progress = time.monotonic() if self._starting_side_effect else 0.0

    def progress(self, stage: str) -> bool:
        if self.cancel_event.is_set():
            return False
        stage = str(stage)
        now = time.monotonic()
        if stage == self._last_stage and now - self._last_progress < 10:
            return True
        saved = self._store.progress(self.run_id, self._token, stage)
        if saved:
            self._last_stage, self._last_progress = stage, now
        return saved

    def interrupt(self, reason: str) -> bool:
        """Expose an unsafe result while retaining ownership until handler exit."""
        self.cancel_event.set()
        return self._store.interrupt(self.run_id, self._token, str(reason))

    def mark_side_effect(self, stage: str = "side_effect") -> bool:
        if self.cancel_event.is_set():
            return False
        if (
            self._starting_side_effect is not None
            and stage == self._starting_side_effect
            and stage == self._last_stage
            and time.monotonic() - self._last_progress < 10
        ):
            # A committed start intent avoids another FULL write, but never
            # substitutes cached authorization for the current claim/status.
            row = self._store.get(self.run_id)
            return bool(
                row
                and row["status"] == "running"
                and row["claim_token"] == self._token
                and row["side_effect"]
                and not self.cancel_event.is_set()
            )
        saved = self._store.progress(self.run_id, self._token, stage, side_effect=True)
        if saved:
            self._last_stage, self._last_progress = stage, time.monotonic()
        return saved


class ExecutionRuntime:
    """One event-driven dispatcher, bounded lazily-created lane pools."""

    def __init__(self, store: ExecutionStore, lane_workers: dict | None = None):
        self.store = store
        self.lane_workers = {**DEFAULT_LANES, **(lane_workers or {})}
        if any(not isinstance(n, int) or n < 1 for n in self.lane_workers.values()):
            raise ValueError("lane capacity must be a positive integer")
        self.owner_id = uuid.uuid4().hex
        self._jobs: dict[str, JobSpec] = {}
        self._modules: dict[str, ModuleSpec] = {}
        self._generations: dict[str, int] = {}
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._active: dict[str, tuple[Future, RunContext, JobSpec, float]] = {}
        self._timers: dict[str, tuple[float, Callable, str, int]] = {}
        self._lifecycle: dict[str, Callable[[bool], None]] = {}
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._completion = threading.Condition()
        # Only callers currently waiting retain a per-run notification version.
        self._waiters: dict[str, list[int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._failure = ""

    @property
    def running(self) -> bool:
        return bool(
            self._thread and self._thread.is_alive() and not self._stop.is_set()
        )

    def register(self, module: ModuleSpec) -> dict:
        return self._apply(module, preserve_enabled=True)

    def apply(self, module: ModuleSpec) -> dict:
        return self._apply(module, preserve_enabled=False)

    def _apply(self, module: ModuleSpec, preserve_enabled: bool) -> dict:
        with self._lock:
            for job in module.jobs:
                for other in self._modules.values():
                    if other.id != module.id and any(
                        j.id == job.id for j in other.jobs
                    ):
                        raise ValueError("job id already belongs to another module")
            previous_state = self.store.module_status(module.id)
            generation = self.store.apply(module, preserve_enabled=preserve_enabled)
            persisted = self.store.module_status(module.id)
            module = replace(module, enabled=bool(persisted["enabled"]))
            previous = self._modules.get(module.id)
            if previous:
                for job in previous.jobs:
                    self._jobs.pop(job.id, None)
            self._modules[module.id] = module
            self._generations[module.id] = generation
            self._jobs.update({job.id: job for job in module.jobs})
        if previous_state and (
            bool(previous_state["enabled"]) != module.enabled
            or previous_state["generation"] != generation
        ):
            self._notify_lifecycle(module.id, module.enabled)
        self.wake()
        return {
            "module_id": module.id,
            "version": module.version,
            "generation": generation,
            "enabled": module.enabled,
        }

    def disable(self, module_id: str) -> dict:
        self.store.set_enabled(module_id, False)
        with self._lock:
            self._timers = {
                key: value
                for key, value in self._timers.items()
                if value[2] != module_id
            }
            active = any(
                c.module_id == module_id for _, c, _, _ in self._active.values()
            )
        self._notify_lifecycle(module_id, False)
        self.wake()
        return {"module_id": module_id, "state": "draining" if active else "disabled"}

    def enable(self, module_id: str) -> None:
        self.store.set_enabled(module_id, True)
        self._notify_lifecycle(module_id, True)
        self.wake()

    def bind_lifecycle(self, module_id: str, callback: Callable[[bool], None]) -> None:
        """Bind a short re-arm/disarm callback; never call lifecycle recursively.

        Binding itself has no effects. Enable always re-arms, including first
        startup. Callbacks run outside host locks and must not stop in-flight
        business work; per-run snapshots remain owned until drain completes.
        """
        if not module_id or not callable(callback):
            raise ValueError("module id and lifecycle callback are required")
        with self._lock:
            self._lifecycle[module_id] = callback

    def _notify_lifecycle(self, module_id: str, enabled: bool) -> None:
        with self._lock:
            callback = self._lifecycle.get(module_id)
        if callback is None:
            return
        try:
            callback(enabled)
        except Exception as exc:
            self.store.set_enabled(module_id, False)
            with self._lock:
                self._timers = {
                    key: value
                    for key, value in self._timers.items()
                    if value[2] != module_id
                }
            self._failure = f"lifecycle_failed:{module_id}:{type(exc).__name__}"
            raise RuntimeError(self._failure) from exc
        if self._failure.startswith(f"lifecycle_failed:{module_id}:"):
            self._failure = ""

    def unregister(self, module_id: str) -> dict:
        result = self.disable(module_id)
        with self._lock:
            previous = self._modules.pop(module_id, None)
            if previous:
                for job in previous.jobs:
                    self._jobs.pop(job.id, None)
            self._generations.pop(module_id, None)
            self._lifecycle.pop(module_id, None)
        # In-flight callbacks own their immutable JobSpec snapshot until done;
        # removing the registration neither kills them nor deletes pending data.
        return result

    def module_status(self, module_id: str) -> dict | None:
        return self.store.module_status(module_id)

    def is_registered(self, module_id: str) -> bool:
        return module_id in self._modules

    def cancel(self, run_id: str) -> bool:
        if self.store.cancel_queued(run_id):
            self._notify_completion(run_id)
            self.wake()
            return True
        with self._lock:
            active = self._active.get(run_id)
            if active:
                _, context, _, _ = active
                context.cancel_event.set()
                changed = self.store.interrupt(run_id, context._token, "cancelled")
                if changed:
                    self._notify_completion(run_id)
                return changed
        return False

    def submit(self, job_id: str, payload: dict | None = None, **kwargs) -> str:
        run_id = self.store.submit(job_id, payload, **kwargs)
        self.wake()
        return run_id

    def schedule(self, job_id: str, when, payload: dict | None = None, **kwargs) -> str:
        kwargs.setdefault("source", "schedule")
        return self.submit(job_id, payload, not_before=when, **kwargs)

    def add_timer(
        self, key: str, deadline, callback: Callable, *, module_id: str = ""
    ) -> None:
        if not key or not callable(callback):
            raise ValueError("timer key and callback are required")
        with self._lock:
            self._timers[key] = (
                _timestamp(deadline),
                callback,
                module_id,
                self._generations.get(module_id, 0),
            )
        self.wake()

    def cancel_timer(self, key: str) -> None:
        with self._lock:
            self._timers.pop(key, None)
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def _notify_completion(self, run_id: str) -> None:
        """A dispatcher wake says nothing about the run a caller is waiting on."""
        with self._completion:
            state = self._waiters.get(run_id)
            if state is not None:
                state[1] += 1
                self._completion.notify_all()

    def start(self) -> None:
        self._start(only_if_unowned=False)

    def start_if_unowned(self) -> bool:
        """Atomically take CLI/standalone ownership without a competing host."""
        return self._start(only_if_unowned=True)

    def _start(self, only_if_unowned: bool) -> bool:
        with self._lock:
            if self.running:
                return True
            if self._thread and self._thread.is_alive():
                raise RuntimeError("previous dispatcher has not stopped")
            if self._active:
                raise RuntimeError("previous handlers have not drained")
            self._stop.clear()
            self._failure = ""
            # A stop/start is a new ownership epoch, even in the same process.
            self.store.owner_stopped(self.owner_id)
            self.owner_id = uuid.uuid4().hex
            if not self.store.owner_started(self.owner_id, only_if_unowned):
                return False
            self.store.recover_offline()
            self._last_heartbeat = time.monotonic()
            self._thread = threading.Thread(
                target=self._loop, name="shared-execution", daemon=True
            )
            self._thread.start()
            return True

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.clear()
                self.tick()
                with self._lock:
                    deadlines = [item[0] for item in self._timers.values()]
                    timeouts = [
                        started + spec.timeout_seconds - time.monotonic()
                        for _, _, spec, started in self._active.values()
                    ]
                delay = max(0, min(deadlines) - time.time()) if deadlines else 1.0
                if timeouts:
                    delay = min(delay, max(0.01, min(timeouts)))
                # Bounded fallback discovers submissions from other processes;
                # SQLite reads do not imply heartbeat or idle run writes.
                self._wake.wait(min(1.0, delay))
        except BaseException as exc:  # noqa: BLE001 - contain a dead dispatcher
            self._failure = f"dispatcher_failed:{type(exc).__name__}"
            logger.error("%s", self._failure)
            self._stop.set()

    def _invoke(self, spec: JobSpec, row: dict, context: RunContext) -> None:
        finished = False
        try:
            result = spec.handler(row["payload"], context)
            if result is None or (isinstance(result, dict) and "status" not in result):
                result = RunResult("unknown", "handler_receipt_missing")
            elif isinstance(result, dict):
                result = RunResult.from_dict(result)
            if not isinstance(result, RunResult):
                result = RunResult("unknown", "handler_receipt_invalid")
            finished = self.store.finish(row["id"], row["claim_token"], result)
        except BaseException as exc:  # noqa: BLE001 - contain one user's handler
            # Exception text may contain credentials or a user's entire input.
            reason = f"handler_raised:{type(exc).__name__}"
            result = RunResult(
                "unknown" if self.store.get(row["id"])["side_effect"] else "failed",
                reason,
            )
            finished = self.store.finish(row["id"], row["claim_token"], result)
        finally:
            if not finished:
                self.store.release(row["id"], row["claim_token"])
            self._notify_completion(row["id"])
            self.wake()

    def tick(self) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self) -> None:
        now = time.monotonic()
        if not self._last_heartbeat:
            self.store.owner_started(self.owner_id)
        if now - self._last_heartbeat >= 10:
            self.store.heartbeat(self.owner_id)
            self.store.recover_offline()
            self._last_heartbeat = now
        with self._lock:
            for run_id, (future, ctx, spec, started) in list(self._active.items()):
                if future.done():
                    del self._active[run_id]
                elif now - started >= spec.timeout_seconds:
                    ctx.cancel_event.set()
                    if self.store.interrupt(run_id, ctx._token, "timeout"):
                        self._notify_completion(run_id)
            due = [
                (key, value)
                for key, value in self._timers.items()
                if value[0] <= time.time()
            ]
            for key, _ in due:
                del self._timers[key]
        # Business callbacks must never run under a host lock. Adapters can
        # hold their own lock while calling add_timer from a worker callback.
        for key, (_, callback, module_id, generation) in due:
            module = self.store.module_status(module_id) if module_id else None
            enabled = bool(
                module and module["enabled"] and module["generation"] == generation
            )
            if module_id and not enabled:
                continue
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - isolate timer registration
                self._failure = f"timer_failed:{key}:{type(exc).__name__}"
                logger.error("%s", self._failure)
        with self._lock:
            if self._stop.is_set():
                return
            ready = self.store.ready_jobs(list(self._jobs))
            lanes = {spec.lane for spec in self._jobs.values()}
            for lane in lanes:
                capacity = self.lane_workers.get(lane, 1)
                active = sum(s.lane == lane for _, _, s, _ in self._active.values())
                job_ids = [
                    job.id
                    for job in self._jobs.values()
                    if job.lane == lane and job.id in ready
                ]
                if active >= capacity or not job_ids:
                    continue
                for _ in range(max(0, capacity - active)):
                    row = self.store.claim(
                        self.owner_id,
                        lane,
                        job_ids,
                        lane_capacity=capacity,
                        starting_side_effects={
                            job.id: job.starting_side_effect
                            for job in self._jobs.values()
                            if job.starting_side_effect is not None
                        },
                    )
                    if row is None:
                        break
                    spec = self._jobs[row["job_id"]]
                    ctx = RunContext(self.store, row)
                    pool = self._pools.get(lane)
                    if pool is None:
                        pool = ThreadPoolExecutor(
                            max_workers=capacity, thread_name_prefix=f"run-{lane}"
                        )
                        self._pools[lane] = pool
                    future = pool.submit(self._invoke, spec, row, ctx)
                    self._active[row["id"]] = (future, ctx, spec, time.monotonic())
                    future.add_done_callback(
                        lambda f, run=row["id"]: self._done(run, f)
                    )

    def _done(self, run_id: str, future: Future) -> None:
        with self._lock:
            if future.exception() is not None:
                self._failure = "handler_result_persistence_failed"
            self._active.pop(run_id, None)
            if self._stop.is_set() and not self._active:
                self.store.owner_stopped(self.owner_id)
                self.store.close_idle_connections()
        self.wake()

    def stop(self, drain: bool = True, timeout: float = 30) -> None:
        deadline = time.monotonic() + max(0, timeout)
        self._stop.set()
        self.wake()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        with self._lock:
            active = list(self._active.items())
        if drain:
            for _, (future, _, _, _) in active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    future.result(timeout=remaining)
                except TimeoutError:
                    break
                except Exception as exc:  # noqa: BLE001 - handler already journaled
                    logger.error("Drain failed: %s", type(exc).__name__)
        with self._lock:
            for run_id, (future, ctx, _, _) in list(self._active.items()):
                if not future.done():
                    ctx.cancel_event.set()
                    if self.store.interrupt(run_id, ctx._token, "shutdown_incomplete"):
                        self._notify_completion(run_id)
                else:
                    del self._active[run_id]
            for pool in self._pools.values():
                pool.shutdown(wait=False, cancel_futures=True)
            self._pools.clear()
            if not self._active:
                self.store.owner_stopped(self.owner_id)
                self.store.close_idle_connections()
            # If a noncooperative thread survives, owner stays live and holds
            # stay occupied. A new process cannot execute overlapping effects.

    def get_run(self, run_id: str) -> dict | None:
        return self.store.get(run_id)

    def wait(self, run_id: str, timeout: float | None = None) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._completion:
            state = self._waiters.setdefault(run_id, [0, 0])
            state[0] += 1
        try:
            while True:
                with self._completion:
                    sequence = state[1]
                row = self.get_run(run_id)
                if row is None:
                    raise KeyError(run_id)
                if row["status"] in TERMINAL:
                    return row
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(run_id)
                remaining = (
                    1.0 if deadline is None else max(0, deadline - time.monotonic())
                )
                with self._completion:
                    # Predicate waiting absorbs spurious and unrelated wakes.
                    # A version captured before the SELECT closes the commit /
                    # subscribe race. Other processes remain visible within 1s.
                    self._completion.wait_for(
                        lambda expected=sequence: state[1] != expected,
                        timeout=min(1.0, remaining),
                    )
        finally:
            with self._completion:
                state[0] -= 1
                if not state[0]:
                    del self._waiters[run_id]

    def health(self) -> dict:
        snapshot = self.store.status()
        with self._lock:
            active = [self.store.get(run_id) for run_id in self._active]
        interrupted = [r for r in active if r and r["status"] == "unknown"]
        return {
            **snapshot,
            "running": self.running,
            "owner_id": self.owner_id,
            "failure": self._failure,
            "active": active,
            "ready": self.running and not self._failure and not interrupted,
        }


_RUNTIMES: dict[str, ExecutionRuntime] = {}
_RUNTIMES_LOCK = threading.Lock()


def get_runtime(path: Path | str, lane_workers: dict | None = None) -> ExecutionRuntime:
    """Reuse one runtime per profile in this process; never auto-start it."""
    resolved = str(Path(path).resolve())
    key = resolved.casefold() if os.name == "nt" else resolved
    with _RUNTIMES_LOCK:
        if key not in _RUNTIMES:
            _RUNTIMES[key] = ExecutionRuntime(ExecutionStore(path), lane_workers)
        elif lane_workers:
            runtime = _RUNTIMES[key]
            if runtime.running and any(
                runtime.lane_workers.get(k, 1) != v for k, v in lane_workers.items()
            ):
                raise RuntimeError("cannot change lane capacity while running")
            runtime.lane_workers.update(lane_workers)
        return _RUNTIMES[key]
