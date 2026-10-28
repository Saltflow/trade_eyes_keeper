"""Durable authenticated SDK handoff; IPC carries wake hints only.

Conversation admission remains one transaction in ProposalStore. This table
bridges the SDK child/host boundary before any rate limit or dedup cache runs.
Legacy commands cannot safely be replayed after their dispatch boundary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ingress_access_check = ContextVar("ingress_access_check", default=None)


class DurableIngress:
    def __init__(
        self, path, app_id: str, allowed_chat_ids, *, connection_provider=None
    ):
        if connection_provider is not None and not callable(connection_provider):
            raise TypeError("connection provider must be callable")
        self.path = Path(path)
        self._connection_provider = connection_provider
        self.app_id = app_id
        self.allowed = frozenset(str(chat) for chat in allowed_chat_ids)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS execution_ingress (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, app_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL, updated REAL NOT NULL,
                    result TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS execution_ingress_pending
                ON execution_ingress(app_id,state,seq);
            """)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(execution_ingress)")
            }
            if "access_result" not in columns:
                db.execute(
                    "ALTER TABLE execution_ingress ADD COLUMN access_result TEXT NOT NULL DEFAULT ''"
                )

    @contextmanager
    def connection(self):
        provider = self._connection_provider
        if provider is not None:
            with provider(database=self.path, autocommit=False) as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def accept(self, payload: dict) -> str | None:
        header = payload.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        if header.get("app_id") not in (None, "", self.app_id):
            raise ValueError("authenticated event app mismatch")
        event = payload.get("event") or {}
        message = event.get("message") or {}
        chat = message.get("chat_id")
        if (
            not isinstance(chat, str)
            or not chat
            or ("*" not in self.allowed and chat not in self.allowed)
        ):
            return None
        message_id = message.get("message_id") or header.get("event_id")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ValueError("message identity missing")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("event exceeds durable ingress bound")
        identity = hashlib.sha256(f"{self.app_id}\0{message_id}".encode()).hexdigest()
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO execution_ingress "
                "(id,app_id,message_id,payload,created,updated) VALUES(?,?,?,?,?,?)",
                (identity, self.app_id, message_id, encoded, time.time(), time.time()),
            )
        return identity

    def pending(self, limit=64):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM execution_ingress WHERE app_id=? AND state='pending' "
                    "ORDER BY seq LIMIT ?",
                    (self.app_id, limit),
                )
            ]

    def finish(self, identity, result, *, state="done"):
        with self.connection() as db:
            db.execute(
                "UPDATE execution_ingress SET state=?,result=?,updated=? "
                "WHERE id=? AND state='pending'",
                (state, result, time.time(), identity),
            )

    def check_access(self, identity, checker):
        """A retry of the same durable envelope consumes rate capacity once."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT access_result FROM execution_ingress WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown durable envelope")
            if row[0]:
                return row[0]
            result = checker()
            db.execute(
                "UPDATE execution_ingress SET access_result=? WHERE id=?",
                (result, identity),
            )
            return result

    def dispatch(self, app, handler):
        from .feishu_handler import _message_text

        for row in self.pending():
            payload = json.loads(row["payload"])
            message = (payload.get("event") or {}).get("message") or {}
            legacy = _message_text(message).startswith("/")
            if legacy:
                # Persist uncertainty before a command can write or send.
                self.finish(row["id"], "legacy_dispatch_started", state="unknown")
            token = ingress_access_check.set(
                lambda checker, identity=row["id"]: self.check_access(identity, checker)
            )
            try:
                result = handler(app, payload)
            finally:
                ingress_access_check.reset(token)
            if result in {"retry", "unavailable"} and not legacy:
                continue
            if legacy:
                with self.connection() as db:
                    db.execute(
                        "UPDATE execution_ingress SET state='done',result=?,updated=? "
                        "WHERE id=? AND state='unknown'",
                        (result, time.time(), row["id"]),
                    )
            else:
                self.finish(row["id"], result)
