"""Durable submission boundary for reminders carried in the assistant reply.

An immediate reminder is sent in the current reply only when its route matches
that conversation. Other routes use the ordinary delivery outbox. Reservations
and actual submissions have separate states so cancellation and restart recovery
cannot manufacture a successful notification.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from ...personal_monitor import MonitorEngine


class ImmediateDelivery:
    def __init__(self, store):
        self.store = store
        self.engine = MonitorEngine(store)
        with store.connection(write=True) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS personal_immediate_receipts (
                owner TEXT NOT NULL, reminder_id TEXT NOT NULL, revision INTEGER NOT NULL,
                outcome TEXT NOT NULL, detail TEXT NOT NULL,
                PRIMARY KEY(owner,reminder_id,revision))"""
            )

    @staticmethod
    def _current(db, item, state="receipt_pending"):
        row = db.execute(
            "SELECT * FROM reminders WHERE owner=? AND reminder_id=? AND revision=? AND state=?",
            (item["owner"], item["reminder_id"], item["revision"], state),
        ).fetchone()
        return dict(row, immediate=True) if row else None

    @staticmethod
    def _key(item):
        return f"immediate:{item['owner']}:{item['reminder_id']}:{item['revision']}"

    @staticmethod
    def _receipt(db, item, outcome, metadata=None):
        db.execute(
            "INSERT OR REPLACE INTO personal_immediate_receipts VALUES(?,?,?,?,?)",
            (
                item["owner"],
                item["reminder_id"],
                item["revision"],
                outcome,
                json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False),
            ),
        )

    def _outbox(self, db, item, state):
        """Insert once; an existing submitted/final result is never restaged."""
        entry_id = self.store.enqueue(
            db,
            owner=item["owner"],
            account=None,
            kind="time",
            route=item["route"],
            text=f"已到期提醒：{item['text']}。原定时间 {item['due_at']}。",
            payload={
                "reminder_id": item["reminder_id"],
                "revision": item["revision"],
                "due_at": item["due_at"],
            },
            business_key=self._key(item),
            now=datetime.now(timezone.utc),
        )
        if entry_id:
            db.execute("UPDATE outbox SET state=? WHERE id=?", (state, entry_id))
        return db.execute(
            "SELECT * FROM outbox WHERE business_key=?", (self._key(item),)
        ).fetchone(), bool(entry_id)

    def stage(self, item):
        """Reserve the current reply without exposing it to the normal sender."""
        with self.store.connection(write=True) as db:
            current = self._current(db, item)
            if current is None:
                return None
            entry, created = self._outbox(db, current, "unknown")
            if created:
                self._receipt(db, current, "pending")
            return {
                "outbox_id": entry["id"],
                "state": entry["state"],
                "reminder_id": current["reminder_id"],
                "revision": current["revision"],
            }

    def defer(self, item):
        """Queue an immediate reminder to its explicitly selected other route."""
        with self.store.connection(write=True) as db:
            current = self._current(db, item)
            if current is None:
                return None
            entry, created = self._outbox(db, current, "queued")
            receipt = db.execute(
                "SELECT outcome FROM personal_immediate_receipts WHERE owner=? AND reminder_id=? AND revision=?",
                (current["owner"], current["reminder_id"], current["revision"]),
            ).fetchone()
            # Switching an unsent staged reply into ordinary route delivery is
            # safe. An actually uncertain submitted reply must never auto-retry.
            if created or (
                entry["state"] == "unknown" and receipt and receipt[0] == "pending"
            ):
                db.execute(
                    "UPDATE outbox SET state='queued' WHERE id=?", (entry["id"],)
                )
                db.execute(
                    "UPDATE reminders SET state='queued' WHERE owner=? AND reminder_id=? AND revision=? AND state='receipt_pending'",
                    (current["owner"], current["reminder_id"], current["revision"]),
                )
                self._receipt(db, current, "deferred")
                state = "queued"
            else:
                state = entry["state"]
            return {
                "outbox_id": entry["id"],
                "state": state,
                "reminder_id": current["reminder_id"],
                "revision": current["revision"],
            }

    def active(self, candidates):
        """Select each still-current version once; canceled/edited items vanish."""
        result, seen = [], set()
        with self.store.connection() as db:
            for item in candidates:
                key = self._key(item)
                if key in seen:
                    continue
                seen.add(key)
                current = self._current(db, item)
                if current:
                    result.append(current)
        return result

    def claim(self, candidates, chat):
        """Atomically cross the final cancelable boundary before sending HTTP."""
        result, seen = [], set()
        with self.store.connection(write=True) as db:
            for item in candidates:
                key = self._key(item)
                if key in seen:
                    continue
                seen.add(key)
                current = self._current(db, item)
                if current is None or current["route"] != chat:
                    continue
                entry, created = self._outbox(db, current, "unknown")
                if created:
                    self._receipt(db, current, "pending")
                receipt = db.execute(
                    "SELECT outcome FROM personal_immediate_receipts WHERE owner=? AND reminder_id=? AND revision=?",
                    (current["owner"], current["reminder_id"], current["revision"]),
                ).fetchone()
                if (
                    entry["state"] != "unknown"
                    or not receipt
                    or receipt[0] != "pending"
                ):
                    continue
                token = "immediate:" + uuid.uuid4().hex
                db.execute(
                    "UPDATE outbox SET state='submitted',token=?,claimed_at=?,attempts=attempts+1 WHERE id=?",
                    (token, datetime.now(timezone.utc).isoformat(), entry["id"]),
                )
                db.execute(
                    "UPDATE reminders SET state='queued' WHERE owner=? AND reminder_id=? AND revision=? AND state='receipt_pending'",
                    (current["owner"], current["reminder_id"], current["revision"]),
                )
                self._receipt(db, current, "submitted")
                result.append(
                    {"item": current, "outbox_id": entry["id"], "token": token}
                )
        return result

    def finish(self, claims, outcome, metadata=None):
        """Record actual results while preserving a concurrent cancel or edit."""
        if outcome not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid_delivery_outcome")
        metadata = metadata or {}
        completed = []
        for claim in claims:
            if self.engine.finish_delivery(
                claim["outbox_id"],
                claim["token"],
                outcome,
                error=str(metadata.get("error", metadata.get("detail", ""))),
                metadata=metadata,
            ):
                with self.store.connection(write=True) as db:
                    self._receipt(db, claim["item"], outcome, metadata)
                completed.append(claim["outbox_id"])
        return completed

    def recover(self):
        """A startup cannot prove an interrupted reply was absent: retain unknown."""
        recovered = []
        with self.store.connection(write=True) as db:
            pending = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM reminders WHERE state='receipt_pending'"
                )
            ]
            for item in pending:
                self._outbox(db, item, "unknown")
            rows = db.execute(
                "SELECT * FROM outbox WHERE kind='time' AND business_key LIKE 'immediate:%' AND state IN ('unknown','submitted')"
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                receipt = db.execute(
                    "SELECT outcome FROM personal_immediate_receipts WHERE owner=? AND reminder_id=? AND revision=?",
                    (row["owner"], payload["reminder_id"], payload["revision"]),
                ).fetchone()
                if row["state"] != "submitted" and receipt and receipt[0] != "pending":
                    continue
                item = {
                    "owner": row["owner"],
                    "reminder_id": payload["reminder_id"],
                    "revision": payload["revision"],
                }
                db.execute(
                    "UPDATE outbox SET state='unknown',error='service_restarted_before_reply_confirmation' WHERE id=?",
                    (row["id"],),
                )
                db.execute(
                    "UPDATE reminders SET state='unknown' WHERE owner=? AND reminder_id=? AND revision=? AND state IN ('receipt_pending','queued')",
                    (item["owner"], item["reminder_id"], item["revision"]),
                )
                self._receipt(
                    db, item, "unknown", {"detail": "reply_confirmation_interrupted"}
                )
                recovered.append(row["id"])
        return recovered
