"""One durable access policy for every application using a local authority."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from ..shared_contracts import AuthorityRef
from .authority import bind_authority


class RequestPolicyBlocked(RuntimeError):
    """A shared source restriction; new callers cannot clear or bypass it."""


def source_policy_id(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source_identity_required")
    parsed = urlsplit(source)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("public_source_identity_required")
        host = (parsed.hostname or "").lower().rstrip(".")
    else:
        host = source.lower().rstrip(".")
    for domain, name in (
        ("cninfo.com.cn", "cninfo"),
        ("sec.gov", "sec"),
        ("hkexnews.hk", "hkex"),
        ("gtimg.cn", "tencent_quote"),
    ):
        if host == domain or host.endswith("." + domain):
            return name
    if not host or len(host) > 255 or any(c.isspace() for c in host):
        raise ValueError("invalid_source_policy_id")
    return host


class RequestPolicy:
    def __init__(self, authority: AuthorityRef):
        self.authority = authority
        bind_authority(authority)
        self.path = authority.requests_db
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS source_policy ("
                "source TEXT PRIMARY KEY, interval REAL NOT NULL, next_at REAL NOT NULL,"
                "blocked INTEGER NOT NULL, blocked_until REAL, reason TEXT NOT NULL,"
                "requests INTEGER NOT NULL DEFAULT 0)"
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def wait(self, source, *, min_interval=3.0, deadline=None, cancel=None):
        key = source_policy_id(source)
        interval = float(min_interval)
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("invalid_source_interval")
        if deadline is not None and not math.isfinite(float(deadline)):
            raise ValueError("invalid_source_deadline")
        while True:
            if cancel is not None and cancel.is_set():
                raise RequestPolicyBlocked("source_request_cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("shared_source_deadline")
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO source_policy VALUES(?,?,0,0,NULL,'',0)",
                    (key, interval),
                )
                row = db.execute(
                    "SELECT * FROM source_policy WHERE source=?", (key,)
                ).fetchone()
                effective = max(interval, row["interval"])
                now = time.time()
                if row["blocked"] and row["blocked_until"] is None:
                    raise RequestPolicyBlocked("source_access_blocked:" + key)
                # Tighten persistently, even while waiting for an older slot.
                db.execute(
                    "UPDATE source_policy SET interval=? WHERE source=?",
                    (effective, key),
                )
                next_at = max(row["next_at"], row["blocked_until"] or 0)
                delay = max(0, next_at - now)
                if not delay:
                    if cancel is not None and cancel.is_set():
                        raise RequestPolicyBlocked("source_request_cancelled")
                    db.execute(
                        "UPDATE source_policy SET next_at=?,requests=requests+1 "
                        "WHERE source=?",
                        (now + effective, key),
                    )
                    return
            remaining = deadline - time.monotonic() if deadline is not None else delay
            if remaining <= 0:
                raise TimeoutError("shared_source_deadline")
            # Recheck shared blocks and cancellation while another caller waits.
            pause = min(delay, remaining, 0.1)
            if cancel is not None:
                cancel.wait(pause)
            else:
                time.sleep(pause)

    def block(self, source, reason, *, retry_after=None):
        key = source_policy_id(source)
        until = None
        if retry_after is not None:
            seconds = float(retry_after)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("invalid_source_retry_after")
            until = time.time() + seconds
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM source_policy WHERE source=?", (key,)
            ).fetchone()
            # Never replace a permanent block with a temporary cooldown.
            if old and old["blocked"] and old["blocked_until"] is None:
                until = None
            elif old and until is not None:
                until = max(until, old["blocked_until"] or 0)
            db.execute(
                "INSERT INTO source_policy VALUES(?,0,0,1,?,?,0) "
                "ON CONFLICT(source) DO UPDATE SET blocked=1,"
                "blocked_until=excluded.blocked_until,reason=excluded.reason",
                (key, until, str(reason)[:160]),
            )

    def adopt_legacy(self, source, *, next_at=0, blocked=False, reason="legacy"):
        key = source_policy_id(source)
        value = float(next_at)
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid_legacy_request_time")
        with self.connection() as db:
            db.execute(
                "INSERT INTO source_policy VALUES(?,0,?,?,NULL,?,0) "
                "ON CONFLICT(source) DO UPDATE SET "
                "next_at=MAX(source_policy.next_at,excluded.next_at),"
                "blocked=MAX(source_policy.blocked,excluded.blocked),"
                "blocked_until=CASE WHEN excluded.blocked=1 THEN NULL "
                "ELSE source_policy.blocked_until END",
                (key, value, int(bool(blocked)), str(reason)[:160]),
            )

    def snapshot(self):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM source_policy")]
        return {"authority": self.authority.to_dict(), "sources": rows}
