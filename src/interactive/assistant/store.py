"""Durable, owner-bound proposals with compare-and-set execution claims."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

CONVERSATION_TEXT_LIMIT = 16000
CONVERSATION_ERROR_LIMIT = 4000
CONVERSATION_EVENT_LIMIT = 8000
CONVERSATION_MAX_EVENTS = 100


def _bounded_text(value: str, limit: int) -> str:
    """Keep persisted values bounded and make truncation visible to operators."""
    if not isinstance(value, str):
        raise TypeError("Conversation text must be a string")
    marker = "\n[truncated]"
    return value if len(value) <= limit else value[: limit - len(marker)] + marker


def canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def payload_hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ProposalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._connection_provider = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, sender TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    updated REAL NOT NULL, note TEXT NOT NULL DEFAULT '', result TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, action_id TEXT, at REAL NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY, chat TEXT NOT NULL, sender TEXT NOT NULL,
                    message_id TEXT NOT NULL, question TEXT NOT NULL,
                    status TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS conversation_message_id
                    ON conversation_turns(message_id) WHERE message_id <> '';
                CREATE INDEX IF NOT EXISTS conversation_owner
                    ON conversation_turns(chat, sender, created DESC);
                CREATE INDEX IF NOT EXISTS conversation_state
                    ON conversation_turns(status);
                CREATE TABLE IF NOT EXISTS conversation_events (
                    id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL,
                    at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES conversation_turns(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS conversation_event_turn
                    ON conversation_events(turn_id, id);
                CREATE TABLE IF NOT EXISTS conversation_deliveries (
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, piece TEXT NOT NULL,
                    route TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
                    metadata TEXT NOT NULL, updated REAL NOT NULL,
                    UNIQUE(turn_id,piece));
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    def set_connection_provider(self, provider):
        """Borrow host-owned contexts without taking ownership of their handles."""
        if not callable(provider):
            raise TypeError("connection provider must be callable")
        self._connection_provider = provider

    @contextmanager
    def connection(self):
        provider = self._connection_provider
        if provider is not None:
            with provider(database=self.path, autocommit=False) as db:
                # The same cache also serves consumers without foreign keys.
                # Apply this contract on every borrow, before any transaction.
                db.execute("PRAGMA foreign_keys=ON")
                yield db
            return
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin_turn(self, chat: str, sender: str, message_id: str, question: str) -> str:
        """Persist an incoming turn. Callers must redact all text before storage.

        A repeated nonempty message ID returns its original turn for the same
        owner. Empty IDs create distinct turns, useful for direct invocations.
        This API records deduplication; callers still control execution claims.
        """
        question = _bounded_text(question, CONVERSATION_TEXT_LIMIT)
        for label, value in (
            ("chat", chat),
            ("sender", sender),
            ("message_id", message_id),
        ):
            if not isinstance(value, str) or len(value) > 512:
                raise ValueError(f"Invalid conversation {label}")
        turn_id = uuid.uuid4().hex
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if message_id:
                existing = db.execute(
                    "SELECT id,chat,sender FROM conversation_turns WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if existing:
                    if existing["chat"] != chat or existing["sender"] != sender:
                        raise ValueError("消息编号已属于其他会话。")
                    return existing["id"]
            db.execute(
                "INSERT INTO conversation_turns "
                "(id,chat,sender,message_id,question,status,created,updated) "
                "VALUES (?,?,?,?,?,'queued',?,?)",
                (turn_id, chat, sender, message_id, question, now, now),
            )
        return turn_id

    def admit_turn(self, envelope: dict, execution_store, *, source="feishu"):
        """Persist the inbound receipt, turn and executable work atomically.

        The runtime tables must live in this same SQLite file. A caller must
        never mark the message received in another database before this commit.
        """
        if Path(execution_store.path).resolve() != self.path.resolve():
            raise ValueError("conversation admission requires one SQLite file")
        chat, sender = envelope["chat"], envelope["sender"]
        message_id = envelope["message_id"]
        if not all(
            isinstance(v, str) and v and len(v) <= 512
            for v in (chat, sender, message_id)
        ):
            raise ValueError("invalid authenticated message identity")
        text = _bounded_text(envelope["text"], CONVERSATION_TEXT_LIMIT)
        current, turn_id = time.time(), uuid.uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id,chat,sender FROM conversation_turns WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if previous:
                if (previous["chat"], previous["sender"]) != (chat, sender):
                    raise ValueError("message already belongs to another owner")
                return {"accepted": False, "turn_id": previous["id"], "duplicate": True}
            # Preserve legacy completed/rejected receipts. Missing historical
            # envelopes cannot safely become new intent after the cutover.
            if db.execute(
                "SELECT 1 FROM receipts WHERE id=?", (message_id,)
            ).fetchone():
                return {"accepted": False, "duplicate": True, "turn_id": None}
            db.execute("INSERT INTO receipts VALUES (?,?)", (message_id, current))
            db.execute(
                "INSERT INTO conversation_turns "
                "(id,chat,sender,message_id,question,status,created,updated) "
                "VALUES (?,?,?,?,?,'queued',?,?)",
                (turn_id, chat, sender, message_id, text, current, current),
            )
            payload = dict(envelope, text=text, turn_id=turn_id)
            session = canonical_json(
                [
                    envelope.get("platform", "feishu"),
                    envelope.get("app_id", ""),
                    chat,
                    sender,
                ]
            )
            run_id = execution_store.submit(
                "conversation.turn",
                payload,
                request_id="inbound:" + message_id,
                session_key=session,
                module_id="conversation",
                source=source,
                max_pending=32,
                db=db,
            )
        return {"accepted": True, "turn_id": turn_id, "run_id": run_id}

    def turn(self, turn_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM conversation_turns WHERE id=?", (turn_id,)
            ).fetchone()
            return dict(row) if row else None

    def reconcile_execution(self):
        """Reflect proved terminal execution facts, never reset another live turn."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE conversation_turns SET status='interrupted',"
                "error='execution_result_unknown',updated=? "
                "WHERE status IN ('queued','running') AND EXISTS ("
                "SELECT 1 FROM execution_runs r WHERE r.job_id='conversation.turn' "
                "AND r.request_id='inbound:'||conversation_turns.message_id "
                "AND r.status IN ('unknown','failed','skipped'))",
                (time.time(),),
            )
            db.execute(
                "UPDATE conversation_deliveries SET state='unknown',updated=? "
                "WHERE state='submitted' AND turn_id IN (SELECT id FROM "
                "conversation_turns WHERE status IN ('interrupted','delivery_failed','failed'))",
                (time.time(),),
            )

    def prepare_reply(self, turn_id, piece, route, body):
        """Commit a send intent; a repeated submitted/unknown reply is not resent."""
        identity = payload_hash([turn_id, piece])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO conversation_deliveries VALUES "
                "(?,?,?,?,?,'prepared','{}',?)",
                (identity, turn_id, piece, route, body, time.time()),
            )
            row = db.execute(
                "SELECT * FROM conversation_deliveries WHERE id=?", (identity,)
            ).fetchone()
            if row["route"] != route or row["body"] != body:
                raise ValueError("reply identity cannot be rebound")
            changed = db.execute(
                "UPDATE conversation_deliveries SET state='submitted',updated=? "
                "WHERE id=? AND state='prepared'",
                (time.time(), identity),
            ).rowcount
        return identity, bool(changed), row["state"]

    def finish_reply(self, identity, outcome, metadata):
        if outcome not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid delivery outcome")
        with self.connection() as db:
            db.execute(
                "UPDATE conversation_deliveries SET state=?,metadata=?,updated=? "
                "WHERE id=? AND state='submitted'",
                (outcome, canonical_json(metadata), time.time(), identity),
            )

    def record_turn_event(self, turn_id: str, kind: str, data_dict: dict) -> None:
        """Store bounded, already-redacted diagnostics without reading credentials."""
        kind = _bounded_text(kind, 80)
        if not isinstance(data_dict, dict):
            raise TypeError("Conversation event data must be a dict")
        data = canonical_json(data_dict)
        if len(data) > CONVERSATION_EVENT_LIMIT:
            # Serialized JSON contains escaped controls; escaping it again can
            # double its length. Leave space for the explicit wrapper.
            data = canonical_json(
                {
                    "truncated": True,
                    "preview": data[: (CONVERSATION_EVENT_LIMIT - 100) // 2],
                }
            )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM conversation_turns WHERE id=?", (turn_id,)
            ).fetchone():
                raise ValueError("未找到会话记录。")
            count = db.execute(
                "SELECT COUNT(*) FROM conversation_events WHERE turn_id=?", (turn_id,)
            ).fetchone()[0]
            if count >= CONVERSATION_MAX_EVENTS:
                db.execute(
                    "UPDATE conversation_events SET kind='events_truncated',data=? "
                    "WHERE id=(SELECT MAX(id) FROM conversation_events WHERE turn_id=?)",
                    (canonical_json({"truncated": True}), turn_id),
                )
                return
            now = time.time()
            db.execute(
                "INSERT INTO conversation_events (turn_id,at,kind,data) VALUES (?,?,?,?)",
                (turn_id, now, kind, data),
            )
            db.execute(
                "UPDATE conversation_turns SET updated=? WHERE id=?", (now, turn_id)
            )

    def finish_turn(
        self, turn_id: str, status: str, answer: str = "", error: str = ""
    ) -> None:
        """Update state; empty answer/error preserve any previously saved text.

        Also accepts intermediate states such as running. A delivery failure can
        therefore preserve the generated answer for later diagnosis or retrieval.
        """
        status = _bounded_text(status, 80)
        if not status:
            raise ValueError("Conversation status cannot be empty")
        answer = _bounded_text(answer, CONVERSATION_TEXT_LIMIT)
        error = _bounded_text(error, CONVERSATION_ERROR_LIMIT)
        with self.connection() as db:
            changed = db.execute(
                "UPDATE conversation_turns SET status=?,answer=CASE WHEN ?='' "
                "THEN answer ELSE ? END,error=CASE WHEN ?='' THEN error ELSE ? END,"
                "updated=? WHERE id=?",
                (status, answer, answer, error, error, time.time(), turn_id),
            ).rowcount
            if not changed:
                raise ValueError("未找到会话记录。")

    def recent_turns(self, chat: str, sender: str, limit: int = 10) -> list[dict]:
        """Return newest first, including ordered events, scoped to one owner."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("Conversation limit must be an integer between 1 and 50")
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM conversation_turns WHERE chat=? AND sender=? "
                "ORDER BY created DESC,rowid DESC LIMIT ?",
                (chat, sender, limit),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                events = db.execute(
                    "SELECT id,at,kind,data FROM conversation_events "
                    "WHERE turn_id=? ORDER BY id",
                    (row["id"],),
                ).fetchall()
                item["events"] = [
                    {**dict(event), "data": json.loads(event["data"])}
                    for event in events
                ]
                result.append(item)
        return result

    def recover_turns(self) -> int:
        """Mark abandoned startup work interrupted; never replay a question."""
        with self.connection() as db:
            return db.execute(
                "UPDATE conversation_turns SET status='interrupted',updated=?,"
                "error=CASE WHEN error='' THEN ? ELSE error END "
                "WHERE status IN ('queued','running')",
                (time.time(), "服务重启；本次问答已中断，不会自动重放。"),
            ).rowcount

    def prune_conversations(self, retention_days: int = 30) -> int:
        """Expire terminal conversation bodies; keep dedup and execution summaries."""
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise ValueError("Conversation retention must be a positive number of days")
        cutoff = time.time() - retention_days * 86400
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM conversation_deliveries WHERE turn_id IN "
                "(SELECT id FROM conversation_turns WHERE updated<? "
                "AND status NOT IN ('queued','running'))",
                (cutoff,),
            )
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "execution_runs" in tables:
                db.execute(
                    "UPDATE execution_runs SET payload=?,result=NULL WHERE job_id='conversation.turn' "
                    "AND status NOT IN ('queued','running') AND finished<? AND payload<>?",
                    ('{"redacted":true}', cutoff, '{"redacted":true}'),
                )
            if "execution_ingress" in tables:
                db.execute(
                    "UPDATE execution_ingress SET payload='{}' WHERE state<>'pending' AND created<? AND payload<>'{}'",
                    (cutoff,),
                )
            return db.execute(
                "DELETE FROM conversation_turns WHERE updated<? "
                "AND status NOT IN ('queued','running')",
                (cutoff,),
            ).rowcount

    def receipt(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self.connection() as db:
            changed = db.execute(
                "INSERT OR IGNORE INTO receipts VALUES (?, ?)",
                (message_id, time.time()),
            ).rowcount
            db.execute(
                "DELETE FROM receipts WHERE created < ?", (time.time() - 604800,)
            )
            return bool(changed)

    def create(
        self,
        kind: str,
        chat: str,
        sender: str,
        payload: dict,
        ttl: int = 900,
        action_id: str | None = None,
    ) -> dict:
        action_id = action_id or uuid.uuid4().hex[:12]
        now = time.time()
        with self.connection() as db:
            db.execute(
                "INSERT INTO actions (id,chat,sender,kind,payload,digest,status,created,expires,updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    chat,
                    sender,
                    kind,
                    canonical_json(payload),
                    payload_hash(payload),
                    "previewing",
                    now,
                    now + ttl,
                    now,
                ),
            )
            self._audit(db, action_id, "previewing", "created")
        return self.get(action_id, chat, sender)

    @staticmethod
    def _audit(db, action_id: str, status: str, detail: str) -> None:
        db.execute(
            "INSERT INTO audit (action_id,at,status,detail) VALUES (?,?,?,?)",
            (action_id, time.time(), status, detail[:2000]),
        )

    @staticmethod
    def _decode(row) -> dict:
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def get(self, action_id: str, chat: str, sender: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM actions WHERE id=? AND chat=? AND sender=?",
                (action_id, chat, sender),
            ).fetchone()
        if row is None:
            raise ValueError("未找到属于你当前会话的提案或任务。")
        item = self._decode(row)
        if payload_hash(item["payload"]) != item["digest"]:
            raise ValueError("提案内容已变化，必须重新生成预览。")
        return item

    def activate(self, action_id: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE actions SET status='pending',updated=? WHERE id=? "
                "AND status IN ('previewing','preview_failed') AND expires>?",
                (time.time(), action_id, time.time()),
            )

    def claim(self, action_id: str, chat: str, sender: str) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM actions WHERE id=? AND chat=? AND sender=?",
                (action_id, chat, sender),
            ).fetchone()
            if row is None:
                raise ValueError("只能确认自己在当前会话发起的提案。")
            item = self._decode(row)
            if payload_hash(item["payload"]) != item["digest"]:
                raise ValueError("提案内容已变化，必须重新生成预览。")
            if item["status"] != "pending":
                raise ValueError(f"提案状态为 {item['status']}，不能重复执行。")
            if item["expires"] < time.time():
                raise ValueError("提案已过期，请重新提出要求。")
            db.execute(
                "UPDATE actions SET status='queued',updated=? WHERE id=?",
                (time.time(), action_id),
            )
            self._audit(db, action_id, "queued", "confirmed by initiating sender")
            item["status"] = "queued"
            return item

    def update(self, action_id: str, status: str, note: str = "", result=None) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE actions SET status=?,note=?,result=COALESCE(?,result),updated=? WHERE id=?",
                (
                    status,
                    note[:2000],
                    canonical_json(result) if result is not None else None,
                    time.time(),
                    action_id,
                ),
            )
            self._audit(db, action_id, status, note)

    def transition(
        self,
        action_id: str,
        status: str,
        expected: tuple[str, ...],
        note: str = "",
        result=None,
    ) -> bool:
        """Compare-and-set state so cancel/progress cannot overwrite completion."""
        with self.connection() as db:
            placeholders = ",".join("?" for _ in expected)
            changed = db.execute(
                "UPDATE actions SET status=?,note=?,result=COALESCE(?,result),updated=? "
                f"WHERE id=? AND status IN ({placeholders})",
                (
                    status,
                    note[:2000],
                    canonical_json(result) if result is not None else None,
                    time.time(),
                    action_id,
                    *expected,
                ),
            ).rowcount
            if changed:
                self._audit(db, action_id, status, note)
            return bool(changed)

    def recent(self, chat: str, sender: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,kind,status,note,created FROM actions "
                "WHERE chat=? AND sender=? ORDER BY created DESC LIMIT 10",
                (chat, sender),
            ).fetchall()
        return [dict(row) for row in rows]

    def recover(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM actions WHERE status IN "
                "('queued','preparing','running','cancelling','applying') "
                "OR (kind='research' AND status='interrupted' AND result IS NULL)"
            ).fetchall()
            db.execute(
                "UPDATE actions SET status='interrupted',note=?,updated=? WHERE status IN "
                "('queued','preparing','running','cancelling','applying')",
                ("服务中断；不会自动重跑，请查询结果后重新提出请求。", time.time()),
            )
            db.execute(
                "UPDATE actions SET status='expired' WHERE status IN "
                "('pending','previewing','preview_failed') AND expires<?",
                (time.time(),),
            )
            for row in rows:
                self._audit(db, row["id"], "interrupted", "startup recovery")
        return [self._decode(row) for row in rows]
