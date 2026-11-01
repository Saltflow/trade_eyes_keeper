"""One durable model-side-effect authority, shared across application adapters.

This is an admission/receipt ledger, not a second task scheduler. Job execution
belongs to task_runtime; a live cancelled handler keeps its admission until it
actually returns. An uncertain POST is never silently retried.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from urllib.parse import urlsplit

import psutil

from ..shared_contracts import AuthorityRef
from ..task_runtime.authority import bind_authority


class GatewayBlocked(ValueError):
    """No provider request was admitted."""


class ExistingAttempt(GatewayBlocked):
    def __init__(self, record):
        self.record = record
        super().__init__("model_attempt_already_exists:" + record["status"])


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_gateway_money")
    return result


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def account_id(endpoint, key):
    """Persist only an account fingerprint, never the key or its header."""
    host = (urlsplit(endpoint).hostname or "").lower()
    if not host:
        raise ValueError("model_endpoint_required")
    return digest(host + ":" + digest(key))


class GatewayLedger:
    def __init__(self, authority: AuthorityRef, *, capacity=20):
        if not isinstance(authority, AuthorityRef):
            raise TypeError("explicit_authority_required")
        if type(capacity) is not int or not 1 <= capacity <= 20:
            raise ValueError("model_capacity_must_be_between_one_and_twenty")
        self.authority, self.capacity = authority, capacity
        bind_authority(authority)
        self.path = authority.models_db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.birth = psutil.Process().create_time()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_authorizations (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    budget TEXT NOT NULL, initial_known TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_attempts (
                    intent TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    authorization TEXT NOT NULL, producer TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL, account TEXT NOT NULL,
                    token TEXT NOT NULL, status TEXT NOT NULL,
                    active INTEGER NOT NULL, sent INTEGER NOT NULL,
                    reserved TEXT NOT NULL, cost TEXT,
                    owner_pid INTEGER NOT NULL, owner_birth REAL NOT NULL,
                    capture TEXT NOT NULL, receipt TEXT, started REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS model_active ON model_attempts(active);
                CREATE TABLE IF NOT EXISTS model_account_blocks (
                    account TEXT PRIMARY KEY, reason TEXT NOT NULL,
                    until REAL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS model_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), capacity INTEGER NOT NULL);
            """)
            db.execute(
                "INSERT INTO model_policy VALUES (1,?) ON CONFLICT(id) DO UPDATE SET capacity=min(capacity,excluded.capacity)",
                (capacity,),
            )
        self.reconcile()

    @contextmanager
    def connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        except sqlite3.Error as error:
            # Preserve an out-of-band fence when the ledger itself is no longer
            # trustworthy. Cache reads remain possible; no later POST is allowed.
            self.fence(type(error).__name__)
            raise
        finally:
            if db is not None:
                db.close()

    @property
    def fence_path(self):
        return self.authority.root / "models_fenced.json"

    def fence(self, reason):
        try:
            with self.fence_path.open("x", encoding="utf-8") as handle:
                json.dump(
                    {"reason": reason, "at": time.time(), "automatic_resume": False},
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass

    def reconcile(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM model_attempts WHERE active=1"):
                try:
                    process = psutil.Process(row["owner_pid"])
                    alive = (
                        abs(process.create_time() - row["owner_birth"]) < 0.01
                        and process.status() != psutil.STATUS_ZOMBIE
                    )
                except psutil.NoSuchProcess:
                    alive = False
                except psutil.AccessDenied:
                    alive = True
                if not alive:
                    db.execute(
                        "UPDATE model_attempts SET active=0,status=? WHERE intent=?",
                        ("unknown" if row["sent"] else "not_sent", row["intent"]),
                    )

    def _intent(self, intent):
        return digest(self.authority.scope + ":" + intent)

    def record(self, intent):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM model_attempts WHERE intent=? AND scope=?",
                (self._intent(intent), self.authority.scope),
            ).fetchone()
        return dict(row) if row else None

    def block(self, account, reason, retry_after=None):
        if retry_after is not None:
            retry_after = float(retry_after)
            if not math.isfinite(retry_after) or retry_after < 0:
                raise ValueError("invalid_model_retry_after")
        until = time.time() + retry_after if retry_after is not None else None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM model_account_blocks WHERE account=?", (account,)
            ).fetchone()
            if old and old["until"] is None:
                return
            if old and until is not None:
                until = max(until, old["until"])
            db.execute(
                "INSERT OR REPLACE INTO model_account_blocks VALUES (?,?,?,?)",
                (account, reason, until, time.time()),
            )

    def authorize(self, identity, budget, *, initial_known=0):
        identity = digest(self.authority.scope + ":" + identity)
        budget, initial_known = money(budget), money(initial_known)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM model_authorizations WHERE id=?", (identity,)
            ).fetchone()
            if old:
                if (
                    money(old["budget"]) != budget
                    or money(old["initial_known"]) != initial_known
                ):
                    raise GatewayBlocked("gateway_authorization_changed")
            else:
                db.execute(
                    "INSERT INTO model_authorizations VALUES (?,?,?,?)",
                    (identity, self.authority.scope, str(budget), str(initial_known)),
                )
        return identity

    def provider_block(self, account):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM model_account_blocks WHERE account=?", (account,)
            ).fetchone()
        if row and (row["until"] is None or row["until"] > time.time()):
            return dict(row)
        return None

    def claim(
        self,
        intent,
        *,
        authorization,
        producer,
        request_sha256,
        account,
        reservation,
        capture,
        deadline=None,
        cancelled=None,
    ):
        token = uuid.uuid4().hex
        reservation = money(reservation)
        key = self._intent(intent)
        reconciled_at = time.monotonic()
        while True:
            if self.fence_path.exists():
                raise GatewayBlocked("gateway_ledger_fenced")
            if time.monotonic() - reconciled_at >= 0.5:
                self.reconcile()
                reconciled_at = time.monotonic()
            if cancelled is not None and cancelled():
                raise GatewayBlocked("gateway_cancelled_before_send")
            if deadline is not None and time.monotonic() >= deadline:
                raise GatewayBlocked("gateway_deadline_before_send")
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                block = db.execute(
                    "SELECT * FROM model_account_blocks WHERE account=?", (account,)
                ).fetchone()
                if block and (block["until"] is None or block["until"] > time.time()):
                    raise GatewayBlocked("gateway_provider_blocked:" + block["reason"])
                old = db.execute(
                    "SELECT * FROM model_attempts WHERE intent=?", (key,)
                ).fetchone()
                if old and old["request_sha256"] != request_sha256:
                    raise GatewayBlocked("gateway_intent_request_changed")
                if old and (old["sent"] or old["active"]):
                    raise ExistingAttempt(dict(old))
                auth = db.execute(
                    "SELECT * FROM model_authorizations WHERE id=? AND scope=?",
                    (authorization, self.authority.scope),
                ).fetchone()
                if auth is None:
                    raise GatewayBlocked("gateway_authorization_missing")
                rows = db.execute(
                    "SELECT * FROM model_attempts WHERE authorization=? AND (active=1 OR sent=1)",
                    (authorization,),
                ).fetchall()
                committed = money(auth["initial_known"]) + sum(
                    (
                        money(
                            row["cost"] if row["cost"] is not None else row["reserved"]
                        )
                        for row in rows
                    ),
                    Decimal(0),
                )
                if committed + reservation > money(auth["budget"]):
                    raise GatewayBlocked("gateway_budget_exhausted")
                active = db.execute(
                    "SELECT count(*) FROM model_attempts WHERE active=1"
                ).fetchone()[0]
                capacity = db.execute(
                    "SELECT capacity FROM model_policy WHERE id=1"
                ).fetchone()[0]
                if active < capacity:
                    db.execute(
                        "INSERT OR REPLACE INTO model_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            self.authority.scope,
                            authorization,
                            producer,
                            request_sha256,
                            account,
                            token,
                            "reserved",
                            1,
                            0,
                            str(reservation),
                            None,
                            os.getpid(),
                            self.birth,
                            str(capture),
                            None,
                            time.time(),
                        ),
                    )
                    return GatewayLease(self, key, token)
            time.sleep(0.02)

    def totals(self, authorization=None):
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM model_attempts WHERE scope=?"
                + (" AND authorization=?" if authorization else ""),
                (self.authority.scope, authorization)
                if authorization
                else (self.authority.scope,),
            ).fetchall()
        known = sum(
            (money(row["cost"]) for row in rows if row["cost"] is not None), Decimal(0)
        )
        unknown = sum(
            (
                money(row["reserved"])
                for row in rows
                if row["sent"] and not row["active"] and row["cost"] is None
            ),
            Decimal(0),
        )
        inflight = sum(
            (money(row["reserved"]) for row in rows if row["active"]), Decimal(0)
        )
        return {
            "known_usage_usd": str(known),
            "unknown_reserved_usd_not_spend": str(unknown),
            "inflight_reserved_usd_not_spend": str(inflight),
            "active": sum(row["active"] for row in rows),
            "sent": sum(row["sent"] for row in rows),
        }


class GatewayLease:
    def __init__(self, ledger, intent, token):
        self.ledger, self.intent, self.token = ledger, intent, token
        self.closed = False
        self.sent = False

    def mark_sent(self):
        if self.ledger.fence_path.exists():
            raise GatewayBlocked("gateway_ledger_fenced")
        with self.ledger.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT account FROM model_attempts WHERE intent=? AND token=? AND active=1",
                (self.intent, self.token),
            ).fetchone()
            if row is None:
                raise GatewayBlocked("gateway_lease_not_current")
            blocked = db.execute(
                "SELECT * FROM model_account_blocks WHERE account=?", (row["account"],)
            ).fetchone()
            if blocked and (blocked["until"] is None or blocked["until"] > time.time()):
                raise GatewayBlocked("gateway_provider_blocked_before_send")
            cursor = db.execute(
                "UPDATE model_attempts SET sent=1,status='sending' WHERE intent=? AND token=? AND active=1 AND sent=0",
                (self.intent, self.token),
            )
            if cursor.rowcount != 1:
                raise GatewayBlocked("gateway_lease_not_current")
        self.sent = True

    def finish(self, receipt=None):
        receipt = receipt or {}
        cost = (receipt.get("cost") or {}).get("usd")
        with self.ledger.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM model_attempts WHERE intent=? AND token=?",
                (self.intent, self.token),
            ).fetchone()
            if old is None:
                raise GatewayBlocked("gateway_lease_not_current")
            if cost is not None:
                cost = str(money(cost))
            if old["cost"] is not None and cost != old["cost"]:
                raise GatewayBlocked("gateway_settlement_changed")
            status = receipt.get("status") or ("unknown" if old["sent"] else "not_sent")
            db.execute(
                "UPDATE model_attempts SET active=0,status=?,cost=?,receipt=? WHERE intent=? AND token=?",
                (
                    status,
                    cost,
                    json.dumps(receipt, ensure_ascii=False),
                    self.intent,
                    self.token,
                ),
            )
        self.closed = True

    def close(self):
        if not self.closed:
            self.finish()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
