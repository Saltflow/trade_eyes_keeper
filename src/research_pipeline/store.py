"""Durable research facts and accounting, sharing the existing execution DB.

Money is attributed to the producer of a real attempt, never to cache consumers.
Historical attempts are evidence and are not charged to a new authorization.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psutil

from src.core.execution import ExecutionStore

from .common import canonical, file_sha, now

_DATABASE_GATES_LOCK = threading.Lock()
_DATABASE_GATES = {}


def _database_gate(path):
    identity = os.path.normcase(str(Path(path).resolve()))
    with _DATABASE_GATES_LOCK:
        return _DATABASE_GATES.setdefault(identity, threading.RLock())


class ResearchExecutionStore(ExecutionStore):
    """The existing queue with the research domain's short SQL admission gate.

    SQLite still owns cross-process atomicity and lane claims. Serializing local
    transactions prevents a burst of settlement/event writers from starving a
    queue admission; no runtime wait, snapshot or provider call holds this gate.
    """

    @contextmanager
    def connection(self, *, database=None, autocommit=False):
        gate = _database_gate(database or self.path)
        connection = super().connection(database=database, autocommit=autocommit)
        with gate, connection as db:
            yield db

    def _open_connection(self, database, autocommit):
        db = super()._open_connection(database, autocommit)
        db.execute("PRAGMA busy_timeout=30000")
        return db


def money(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("nonnegative_finite_money_required")
    return amount


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False
    )


def _alive(pid, birth):
    try:
        return abs(psutil.Process(int(pid)).create_time() - float(birth)) < 0.01
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, ValueError, TypeError):
        # Uncertain ownership must never enable a second paid producer.
        return True


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "research.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    at TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS research_events_run ON research_events(run_id,seq);
                CREATE TABLE IF NOT EXISTS research_artifacts (
                    key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_artifact_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_attempts (
                    key TEXT PRIMARY KEY, producer_run TEXT, status TEXT NOT NULL,
                    capture_dir TEXT NOT NULL, reservation TEXT NOT NULL,
                    repair INTEGER NOT NULL, sent_intent INTEGER NOT NULL DEFAULT 0,
                    historical INTEGER NOT NULL DEFAULT 0, owner_pid INTEGER,
                    owner_birth REAL, receipt TEXT, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS research_attempt_run ON research_attempts(producer_run);
                CREATE TABLE IF NOT EXISTS research_provider_blocks (
                    provider_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS research_execution_authority (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    data TEXT NOT NULL);
            """)
            authority = db.execute(
                "SELECT data FROM research_execution_authority WHERE singleton=1"
            ).fetchone()
        # Old queue facts remain in place and are never migrated or claimed.
        # New runs bind their explicit authority before any execution begins.
        self.execution = ResearchExecutionStore(self.db_path)
        if authority:
            self._use_execution_authority(json.loads(authority[0]))
        self.reconcile()

    def _use_execution_authority(self, record):
        from src.shared_contracts import AuthorityRef
        from src.task_runtime.authority import bind_authority

        authority = AuthorityRef(
            Path(record["root"]),
            scope=record.get("scope", "public"),
            isolated=bool(record.get("isolated", False)),
        )
        bind_authority(authority, expected_id=record["authority_id"])
        if self.execution.path != authority.execution_db:
            self.execution.close_idle_connections()
            self.execution = ResearchExecutionStore(authority.execution_db)
        self.execution_authority = authority

    def bind_execution_authority(self, authority):
        from src.task_runtime.authority import bind_authority

        record = bind_authority(authority)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT data FROM research_execution_authority WHERE singleton=1"
            ).fetchone()
            if previous and json.loads(previous[0]) != record:
                raise ValueError("research_execution_authority_changed")
            if not previous and self.execution.path != authority.execution_db:
                # A first migration is an offline handoff, not permission to
                # split a live queue across two independent capacity ledgers.
                owners = db.execute(
                    "SELECT * FROM execution_owners WHERE state='running'"
                ).fetchall()
                outstanding = db.execute(
                    "SELECT COUNT(*) FROM execution_runs "
                    "WHERE claim_token IS NOT NULL AND released_at IS NULL"
                ).fetchone()[0]
                if outstanding or any(
                    self.execution._owner_alive(dict(owner)) for owner in owners
                ):
                    raise ValueError("legacy_execution_not_drained")
            db.execute(
                "INSERT OR IGNORE INTO research_execution_authority VALUES(1,?)",
                (_encoded(record),),
            )
        self._use_execution_authority(record)

    @contextmanager
    def connection(self):
        with _database_gate(self.db_path):
            db = sqlite3.connect(self.db_path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def create_run(self, spec_dict, run_id=None):
        if spec_dict.get("authority_root"):
            from src.shared_contracts import AuthorityRef

            self.bind_execution_authority(
                AuthorityRef(
                    Path(spec_dict["authority_root"]),
                    isolated=bool(spec_dict.get("authority_isolated", False)),
                )
            )
        run_id = run_id or "research_" + uuid.uuid4().hex
        if not isinstance(run_id, str) or not run_id or len(run_id) > 160:
            raise ValueError("invalid_run_id")
        money(spec_dict.get("budget_limit_usd", 0))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM research_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row:
                if row["spec"] != _encoded(spec_dict):
                    raise ValueError("run_id_has_different_spec")
                return json.loads(row["data"])
            record = {
                "id": run_id,
                "run_id": run_id,
                "spec": spec_dict,
                "state": "created",
                "requires_resume": False,
                "created_at": now(),
                "updated_at": now(),
            }
            db.execute(
                "INSERT INTO research_runs VALUES(?,?,?)",
                (run_id, _encoded(spec_dict), _encoded(record)),
            )
        self.event(run_id, "run_created")
        return record

    def get_run(self, run_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM research_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError("research_run_not_found")
        return json.loads(row[0])

    @staticmethod
    def dispatch_allowed(run, repair=False):
        if run.get("state") in {
            "stopped",
            "user_stopped",
            "cancelled",
            "source_blocked",
            "auth_blocked",
            "ledger_blocked",
            "funds_blocked",
            "budget_stop",
        }:
            return False
        output = run.get("spec", {}).get("output")
        if output and (Path(output) / "STOP").exists():
            return False
        repairable = run.get("pause_reason") in {
            "program_error",
            "code_fault",
            "awaiting_fix",
            "repair_pending",
        }
        return not run.get("requires_resume") or bool(repair and repairable)

    def update_run(self, run_id, **fields):
        if set(fields) & {"id", "run_id", "spec", "created_at"}:
            raise ValueError("immutable_run_identity")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM research_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError("research_run_not_found")
            record = json.loads(row[0])
            record.update(fields, updated_at=now())
            db.execute(
                "UPDATE research_runs SET data=? WHERE id=?", (_encoded(record), run_id)
            )
        return record

    def claim_run(self, run_id, pid, birth):
        """Take one controller owner, never steal even an in-process live run."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM research_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError("research_run_not_found")
            run = json.loads(row[0])
            if run.get("requires_resume"):
                return False
            if run.get("state") == "running":
                if _alive(run.get("owner_pid"), run.get("owner_birth")):
                    return False
                run.update(
                    state="paused",
                    requires_resume=True,
                    pause_reason="owner_offline",
                    updated_at=now(),
                )
                db.execute(
                    "UPDATE research_runs SET data=? WHERE id=?",
                    (_encoded(run), run_id),
                )
                return False
            if run.get("state") not in {
                "created",
                "planned",
                "prepared",
            } or not self.dispatch_allowed(run):
                return False
            run.update(
                state="running",
                owner_pid=int(pid),
                owner_birth=float(birth),
                updated_at=now(),
            )
            db.execute(
                "UPDATE research_runs SET data=? WHERE id=?", (_encoded(run), run_id)
            )
        return True

    def event(self, run_id, event, **fields):
        at = now()
        with self.connection() as db:
            cursor = db.execute(
                "INSERT INTO research_events(run_id,at,event,data) VALUES(?,?,?,?)",
                (run_id, at, str(event), _encoded(fields)),
            )
        return {
            "seq": cursor.lastrowid,
            "run_id": run_id,
            "at": at,
            "event": str(event),
            **fields,
        }

    def events(self, run_id, after=0, limit=500):
        if not 0 <= int(after) or not 1 <= int(limit) <= 500:
            raise ValueError("invalid_event_page")
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM research_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, int(after), int(limit)),
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "run_id": r["run_id"],
                "at": r["at"],
                "event": r["event"],
                **json.loads(r["data"]),
            }
            for r in rows
        ]

    def tail_events(self, run_id, limit=200):
        if not 1 <= int(limit) <= 500:
            raise ValueError("invalid_event_page")
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM research_events WHERE run_id=? ORDER BY seq DESC LIMIT ?",
                (run_id, int(limit)),
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "run_id": r["run_id"],
                "at": r["at"],
                "event": r["event"],
                **json.loads(r["data"]),
            }
            for r in reversed(rows)
        ]

    def provider_block(self, provider_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM research_provider_blocks WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        if (
            value.get("blocked_until") is not None
            and value["blocked_until"] <= time.time()
        ):
            return None
        return value

    def provider_usage(self, provider_id):
        """Known costs and unknown reservations, only for this credential scope."""
        known = unknown = Decimal(0)
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM research_attempts WHERE historical=0 AND json_extract(data,'$.provider_id')=?",
                (provider_id,),
            ).fetchall()
        for row in rows:
            receipt = json.loads(row["receipt"]) if row["receipt"] else {}
            cost = receipt.get("cost", {})
            if (
                cost.get("usd") is not None
                and cost.get("kind") != "usage_cache_unknown_upper_bound"
            ):
                known += money(cost["usd"])
            elif row["sent_intent"] and row["status"] != "sending":
                unknown += money(row["reservation"])
        return known, unknown

    def block_provider(
        self, provider_id, http_status, capture_dir="", retry_after_seconds=None
    ):
        if http_status not in {401, 402, 403, 429}:
            raise ValueError("unsupported_global_provider_block")
        until = (
            time.time()
            + max(
                0,
                float(retry_after_seconds if retry_after_seconds is not None else 300),
            )
            if http_status == 429
            else None
        )
        value = {
            "provider_id": provider_id,
            "http_status": http_status,
            "reason": f"HTTP{http_status}",
            "blocked_until": until,
            "created_at": now(),
            "capture_dir": str(capture_dir),
        }
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM research_provider_blocks WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
            if row and json.loads(row[0]).get("blocked_until") is None:
                return json.loads(row[0])
            db.execute(
                "INSERT INTO research_provider_blocks VALUES(?,?) ON CONFLICT(provider_id) DO UPDATE SET data=excluded.data",
                (provider_id, _encoded(value)),
            )
        return value

    @staticmethod
    def _artifact_files(path):
        path = Path(path).resolve()
        if path.is_file():
            return {str(path): file_sha(path)}
        if path.is_dir():
            return {str(p): file_sha(p) for p in sorted(path.rglob("*")) if p.is_file()}
        return {}

    def get_artifact(self, key):
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM research_artifacts WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        try:
            valid = all(
                Path(p).is_file() and file_sha(Path(p)) == h
                for p, h in record["files"].items()
            )
        except OSError:
            valid = False
        return record if valid and record["files"] else None

    def put_artifact(self, key, stage, path, status="complete", metadata=None):
        files = self._artifact_files(path)
        if not files:
            raise ValueError("artifact_files_missing")
        digest = (
            file_sha(Path(path))
            if Path(path).is_file()
            else __import__("hashlib").sha256(canonical(files).encode()).hexdigest()
        )
        record = {
            "key": key,
            "stage": stage,
            "path": str(Path(path).resolve()),
            "sha256": digest,
            "status": status,
            "metadata": metadata or {},
            "files": files,
            "created_at": now(),
        }
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT data FROM research_artifacts WHERE key=?", (key,)
            ).fetchone()
            if previous:
                old = json.loads(previous[0])
                valid_old = all(
                    Path(p).is_file() and file_sha(Path(p)) == h
                    for p, h in old["files"].items()
                )
                revision = (
                    stage in {"annual", "context"} and old["path"] != record["path"]
                )
                if valid_old and not revision:
                    return old
                if old["path"] == record["path"] and not valid_old:
                    raise ValueError("artifact_changed_in_place")
                db.execute(
                    "INSERT INTO research_artifact_history(key,data) VALUES(?,?)",
                    (key, previous[0]),
                )
            db.execute(
                "INSERT INTO research_artifacts VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (key, _encoded(record)),
            )
        return record

    def list_artifacts(self, stage=None):
        with self.connection() as db:
            records = [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM research_artifacts ORDER BY key")
            ]
        return [r for r in records if stage is None or r["stage"] == stage]

    @staticmethod
    def _attempt(row):
        if row is None:
            return None
        result = {**dict(row), **json.loads(row["data"])}
        result["receipt"] = json.loads(row["receipt"]) if row["receipt"] else None
        result.pop("data", None)
        return result

    def get_attempt(self, key):
        with self.connection() as db:
            return self._attempt(
                db.execute(
                    "SELECT * FROM research_attempts WHERE key=?", (key,)
                ).fetchone()
            )

    def list_attempts(self, run_id=None, limit=500):
        if not 1 <= int(limit) <= 10000:
            raise ValueError("invalid_attempt_page")
        where, params = (
            (" WHERE producer_run=?", [run_id]) if run_id is not None else ("", [])
        )
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM research_attempts"
                + where
                + " ORDER BY rowid DESC LIMIT ?",
                [*params, int(limit)],
            ).fetchall()
        return [self._attempt(row) for row in reversed(rows)]

    def active_attempts(self, run_id=None):
        where = " WHERE status IN ('queued','sending')"
        params = []
        if run_id is not None:
            where += " AND producer_run=?"
            params.append(run_id)
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM research_attempts" + where + " ORDER BY rowid", params
            ).fetchall()
        return [self._attempt(row) for row in rows]

    def import_attempt(self, key, record):
        receipt = record.get("receipt", record)
        capture = str(record.get("capture_dir", record.get("path", "")))
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO research_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    record.get("producer_run"),
                    receipt.get("status", "unknown"),
                    capture,
                    "0",
                    0,
                    1,
                    1,
                    None,
                    None,
                    _encoded(receipt),
                    _encoded(record),
                ),
            )
        return self.get_attempt(key)

    def claim_task(self, key, run_id, capture_dir, **data):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM research_attempts WHERE key=?", (key,)
            ).fetchone()
            if old and not (old["status"] == "not_sent" and not old["sent_intent"]):
                return False, self._attempt(old)
            run = json.loads(
                db.execute(
                    "SELECT data FROM research_runs WHERE id=?", (run_id,)
                ).fetchone()[0]
            )
            if not self.dispatch_allowed(run, data.get("repair", False)):
                return False, {
                    "status": "not_sent",
                    "receipt": {"status": "not_sent", "reason": "run_paused"},
                    "capture_dir": str(capture_dir),
                }
            if old:
                db.execute("DELETE FROM research_attempts WHERE key=?", (key,))
            birth = psutil.Process(os.getpid()).create_time()
            db.execute(
                "INSERT INTO research_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    run_id,
                    "queued",
                    str(capture_dir),
                    "0",
                    int(data.get("repair", False)),
                    0,
                    0,
                    os.getpid(),
                    birth,
                    None,
                    _encoded({"created_at": now(), **data}),
                ),
            )
        return True, self.get_attempt(key)

    def _budget(self, db, run_id):
        row = db.execute(
            "SELECT spec,data FROM research_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not row:
            raise KeyError("research_run_not_found")
        spec = json.loads(row[0])
        limit = money(spec.get("budget_limit_usd", 0))
        fraction = money(spec.get("repair_fraction", ".1"))
        known = unknown = inflight = repair_known = repair_unknown = repair_inflight = (
            Decimal(0)
        )
        for attempt in db.execute(
            "SELECT * FROM research_attempts WHERE producer_run=? AND historical=0",
            (run_id,),
        ):
            receipt = json.loads(attempt["receipt"]) if attempt["receipt"] else {}
            cost = receipt.get("cost", {})
            amount = cost.get("usd")
            exact = (
                amount is not None
                and cost.get("kind") != "usage_cache_unknown_upper_bound"
            )
            k = money(amount) if exact else Decimal(0)
            i = (
                money(attempt["reservation"])
                if attempt["status"] == "sending"
                else Decimal(0)
            )
            u = (
                money(attempt["reservation"])
                if attempt["sent_intent"]
                and not exact
                and attempt["status"] != "sending"
                else Decimal(0)
            )
            known += k
            inflight += i
            unknown += u
            if attempt["repair"]:
                repair_known += k
                repair_inflight += i
                repair_unknown += u
        repair_limit = limit * fraction
        research_committed = (
            known + unknown + inflight - repair_known - repair_unknown - repair_inflight
        )
        total_remaining = max(Decimal(0), limit - known - unknown - inflight)
        released = bool(json.loads(row["data"]).get("repair_hold_released"))
        research_remaining = (
            total_remaining
            if released
            else max(Decimal(0), limit - repair_limit - research_committed)
        )
        return {
            "authorized_usd": str(limit),
            "known_usage_usd": str(known),
            "unknown_liability_reserved_usd_not_spend": str(unknown),
            "inflight_reserved_usd_not_spend": str(inflight),
            "repair_known_usage_usd": str(repair_known),
            "repair_limit_usd": str(repair_limit),
            "repair_remaining_usd": str(
                min(
                    total_remaining,
                    max(
                        Decimal(0),
                        repair_limit - repair_known - repair_unknown - repair_inflight,
                    ),
                )
            ),
            "research_remaining_usd": str(research_remaining),
            "total_remaining_usd": str(total_remaining),
            "repair_hold_released": released,
            "is_provider_invoice": False,
        }

    def budget(self, run_id):
        with self.connection() as db:
            return self._budget(db, run_id)

    def reserve(
        self, key, amount, hold_usd=0, available_usd=None, expected_capture=None
    ):
        amount, hold = money(amount), money(hold_usd)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute(
                "SELECT * FROM research_attempts WHERE key=?", (key,)
            ).fetchone()
            if not attempt or attempt["status"] != "queued":
                return "attempt_not_queued"
            if expected_capture is not None and attempt["capture_dir"] != str(
                expected_capture
            ):
                return "attempt_not_owned"
            run = json.loads(
                db.execute(
                    "SELECT data FROM research_runs WHERE id=?",
                    (attempt["producer_run"],),
                ).fetchone()[0]
            )
            if not self.dispatch_allowed(run, bool(attempt["repair"])):
                return "run_paused"
            budget = self._budget(db, attempt["producer_run"])
            pool = (
                "repair_remaining_usd"
                if attempt["repair"]
                else "research_remaining_usd"
            )
            if amount + hold > money(budget[pool]) or amount > money(
                budget["total_remaining_usd"]
            ):
                return "repair_budget_stop" if attempt["repair"] else "budget_stop"
            provider = json.loads(attempt["data"]).get("provider_id")
            held = sum(
                (
                    money(r[0])
                    for r in db.execute(
                        "SELECT reservation FROM research_attempts WHERE status='sending' AND historical=0 "
                        "AND (? IS NULL OR json_extract(data,'$.provider_id')=?)",
                        (provider, provider),
                    )
                ),
                Decimal(0),
            )
            if available_usd is not None and held + amount > money(available_usd):
                return "insufficient_account_funds"
            db.execute(
                "UPDATE research_attempts SET status='sending',reservation=? WHERE key=?",
                (str(amount), key),
            )
        return ""

    def mark_sent(self, key, expected_capture=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT capture_dir FROM research_attempts WHERE key=?", (key,)
            ).fetchone()
            if expected_capture is not None and (
                not row or row[0] != str(expected_capture)
            ):
                raise ValueError("attempt_not_owned")
            changed = db.execute(
                "UPDATE research_attempts SET sent_intent=1 WHERE key=? AND status='sending' AND sent_intent=0",
                (key,),
            ).rowcount
            if not changed:
                raise ValueError("attempt_intent_not_owned")

    def settle(self, key, receipt, expected_capture=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM research_attempts WHERE key=?", (key,)
            ).fetchone()
            if not row:
                raise KeyError("attempt_not_found")
            if expected_capture is not None and row["capture_dir"] != str(
                expected_capture
            ):
                raise ValueError("attempt_not_owned")
            if row["receipt"]:
                if row["receipt"] != _encoded(receipt):
                    raise ValueError("immutable_attempt_receipt")
                return
            status = receipt.get("status", "unknown")
            db.execute(
                "UPDATE research_attempts SET status=?,receipt=? WHERE key=?",
                (status, _encoded(receipt), key),
            )

    def reconcile(self):
        paused = set()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM research_attempts WHERE historical=0 AND status IN ('queued','sending')"
            ).fetchall()
            # A controller can die between phases, when all previous attempts
            # are terminal and none of the next nodes has been queued yet.
            # Planned runs are deliberately not owners of paid work.
            for run_row in db.execute("SELECT id,data FROM research_runs"):
                run = json.loads(run_row["data"])
                if run.get("state") == "running" and not _alive(
                    run.get("owner_pid"), run.get("owner_birth")
                ):
                    paused.add(run_row["id"])
            for row in rows:
                if _alive(row["owner_pid"], row["owner_birth"]):
                    continue
                status = "unknown" if row["sent_intent"] else "not_sent"
                receipt = {
                    "status": status,
                    "reason": "owner_offline",
                    "usage": None,
                    "cost": {"usd": None, "kind": "usage_unknown"},
                    "capture_dir": row["capture_dir"],
                    "automatic_retries": 0,
                }
                db.execute(
                    "UPDATE research_attempts SET status=?,receipt=? WHERE key=?",
                    (status, _encoded(receipt), row["key"]),
                )
                if row["producer_run"]:
                    paused.add(row["producer_run"])
            for run_id in paused:
                row = db.execute(
                    "SELECT data FROM research_runs WHERE id=?", (run_id,)
                ).fetchone()
                if row:
                    record = json.loads(row[0])
                    record.update(
                        state="paused",
                        requires_resume=True,
                        pause_reason="owner_offline",
                        updated_at=now(),
                    )
                    db.execute(
                        "UPDATE research_runs SET data=? WHERE id=?",
                        (_encoded(record), run_id),
                    )
        self.execution.recover_offline()
        for run_id in paused:
            self.event(run_id, "resume_required", reason="owner_offline")
        return sorted(paused)
