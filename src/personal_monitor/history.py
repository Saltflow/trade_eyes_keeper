"""Transactional, chat-scoped history for position facts only.

All functions use the caller's existing SQLite transaction. Business rule state
and delivery receipts deliberately never enter a restorable history image.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    """Add history storage without rewriting any existing business rows."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS position_history_entries (
          history_id TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL,
          chat_scope TEXT NOT NULL, parent_id TEXT, source TEXT NOT NULL,
          created_at TEXT NOT NULL, before_revision INTEGER NOT NULL,
          after_revision INTEGER NOT NULL, before_state TEXT NOT NULL,
          after_state TEXT NOT NULL, watermark_before TEXT, watermark_after TEXT,
          state TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS position_history_scope
          ON position_history_entries(owner,account,chat_scope,after_revision);
        CREATE TABLE IF NOT EXISTS position_history_cursors (
          owner TEXT NOT NULL, account TEXT NOT NULL, chat_scope TEXT NOT NULL,
          head_id TEXT, redo_ids TEXT NOT NULL, revision INTEGER NOT NULL,
          PRIMARY KEY(owner,account,chat_scope));
        CREATE TABLE IF NOT EXISTS position_history_requests (
          owner TEXT NOT NULL, account TEXT NOT NULL, chat_scope TEXT NOT NULL,
          request_id TEXT NOT NULL, action TEXT NOT NULL, fingerprint TEXT NOT NULL,
          result TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(owner,account,chat_scope,request_id));
        CREATE TABLE IF NOT EXISTS position_history_barriers (
          owner TEXT NOT NULL, account TEXT NOT NULL, as_of TEXT NOT NULL,
          PRIMARY KEY(owner,account));
        """
    )


def fingerprint(action, payload):
    return hashlib.sha256(_json([action, payload]).encode("utf-8")).hexdigest()


def receipt(db, owner, account, scope, request_id, action, digest):
    row = db.execute(
        "SELECT action,fingerprint,result FROM position_history_requests "
        "WHERE owner=? AND account=? AND chat_scope=? AND request_id=?",
        (owner, account, scope, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["action"] != action or row["fingerprint"] != digest:
        raise ValueError("history_request_id_reused_with_different_content")
    return dict(json.loads(row["result"]), duplicate=True)


def save_receipt(db, owner, account, scope, request_id, action, digest, result):
    db.execute(
        "INSERT INTO position_history_requests VALUES(?,?,?,?,?,?,?,?)",
        (owner, account, scope, request_id, action, digest, _json(result), _now()),
    )


def capture(db, owner, account, instruments):
    result = {}
    for code in sorted(set(instruments)):
        position = db.execute(
            "SELECT as_of,source,data FROM positions "
            "WHERE owner=? AND account=? AND instrument=?",
            (owner, account, code),
        ).fetchone()
        pending = db.execute(
            "SELECT as_of,reason FROM pending_issues "
            "WHERE owner=? AND account=? AND instrument=?",
            (owner, account, code),
        ).fetchone()
        result[code] = {
            "position": dict(position) if position else None,
            "pending": dict(pending) if pending else None,
        }
    return result


def delta(before, after):
    changed = sorted(code for code in before if before[code] != after[code])
    return (
        {code: before[code] for code in changed},
        {code: after[code] for code in changed},
    )


def changes(before, after):
    def quantity(state):
        position = state["position"]
        return json.loads(position["data"]).get("quantity") if position else None

    return [
        {
            "instrument": code,
            "before_quantity": quantity(before[code]),
            "after_quantity": quantity(after[code]),
        }
        for code in sorted(before)
        if before[code]["position"] != after[code]["position"]
    ]


def clear_redo(db, owner, account, *, except_scope=None):
    """Retain abandoned nodes for audit, but make them impossible to replay."""
    rows = db.execute(
        "SELECT chat_scope,redo_ids FROM position_history_cursors "
        "WHERE owner=? AND account=?",
        (owner, account),
    ).fetchall()
    for row in rows:
        if row["chat_scope"] == except_scope:
            continue
        for history_id in json.loads(row["redo_ids"]):
            db.execute(
                "UPDATE position_history_entries SET state='branched' "
                "WHERE history_id=? AND owner=? AND account=? AND state='undone'",
                (history_id, owner, account),
            )
        db.execute(
            "UPDATE position_history_cursors SET redo_ids='[]' "
            "WHERE owner=? AND account=? AND chat_scope=?",
            (owner, account, row["chat_scope"]),
        )


def record_import(
    db,
    owner,
    account,
    scope,
    before_revision,
    after_revision,
    before,
    after,
    source,
    watermark_before,
    watermark_after,
):
    cursor = db.execute(
        "SELECT * FROM position_history_cursors "
        "WHERE owner=? AND account=? AND chat_scope=?",
        (owner, account, scope),
    ).fetchone()
    parent = (
        cursor["head_id"] if cursor and cursor["revision"] == before_revision else None
    )
    history_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO position_history_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            history_id,
            owner,
            account,
            scope,
            parent,
            source,
            _now(),
            before_revision,
            after_revision,
            _json(before),
            _json(after),
            watermark_before,
            watermark_after,
            "active",
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO position_history_cursors VALUES(?,?,?,?,?,?)",
        (owner, account, scope, history_id, "[]", after_revision),
    )
    return history_id


def _entry(db, owner, account, scope, history_id):
    return db.execute(
        "SELECT * FROM position_history_entries WHERE history_id=? "
        "AND owner=? AND account=? AND chat_scope=?",
        (history_id, owner, account, scope),
    ).fetchone()


def apply(db, owner, account, scope, revision, action, cancel_thresholds):
    """Restore a single delta; account CAS is checked by the store first."""
    result = {
        "action": action,
        "history_id": None,
        "revision": revision,
        "applied": [],
        "changes": [],
        "cancelled_thresholds": 0,
        "duplicate": False,
    }
    cursor = db.execute(
        "SELECT * FROM position_history_cursors "
        "WHERE owner=? AND account=? AND chat_scope=?",
        (owner, account, scope),
    ).fetchone()
    redo = json.loads(cursor["redo_ids"]) if cursor else []
    history_id = (
        cursor["head_id"] if action == "undo" and cursor else redo[-1] if redo else None
    )
    if history_id is None:
        result["reason"] = f"no_position_change_to_{action}"
        return result
    if cursor["revision"] != revision:
        result["reason"] = "history_conflict"
        return result
    entry = _entry(db, owner, account, scope, history_id)
    if entry is None or entry["state"] != ("active" if action == "undo" else "undone"):
        result["reason"] = "history_conflict"
        return result
    before, after = json.loads(entry["before_state"]), json.loads(entry["after_state"])
    expected, restored = (after, before) if action == "undo" else (before, after)
    if capture(db, owner, account, expected) != expected:
        result["reason"] = "history_conflict"
        return result
    clear_redo(db, owner, account, except_scope=scope)
    for code, state in restored.items():
        position, pending = state["position"], state["pending"]
        if position is None:
            db.execute(
                "DELETE FROM positions WHERE owner=? AND account=? AND instrument=?",
                (owner, account, code),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?)",
                (
                    owner,
                    account,
                    code,
                    position["as_of"],
                    position["source"],
                    position["data"],
                ),
            )
        if pending is None:
            db.execute(
                "DELETE FROM pending_issues "
                "WHERE owner=? AND account=? AND instrument=?",
                (owner, account, code),
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO pending_issues VALUES(?,?,?,?,?)",
                (owner, account, code, pending["as_of"], pending["reason"]),
            )
        result["cancelled_thresholds"] += cancel_thresholds(db, owner, account, code)
    revision += 1
    db.execute(
        "UPDATE accounts SET revision=? WHERE owner=? AND account=?",
        (revision, owner, account),
    )
    if action == "undo":
        redo.append(history_id)
        head, state = entry["parent_id"], "undone"
    else:
        redo.pop()
        head, state = history_id, "active"
    db.execute(
        "UPDATE position_history_entries SET state=? WHERE history_id=?",
        (state, history_id),
    )
    db.execute(
        "UPDATE position_history_cursors SET head_id=?,redo_ids=?,revision=? "
        "WHERE owner=? AND account=? AND chat_scope=?",
        (head, _json(redo), revision, owner, account, scope),
    )
    watermark = db.execute(
        "SELECT as_of FROM snapshot_watermarks WHERE owner=? AND account=?",
        (owner, account),
    ).fetchone()
    if watermark:
        db.execute(
            "INSERT INTO position_history_barriers VALUES(?,?,?) "
            "ON CONFLICT(owner,account) DO UPDATE SET "
            "as_of=MAX(position_history_barriers.as_of,excluded.as_of)",
            (owner, account, watermark["as_of"]),
        )
    result.update(
        history_id=history_id,
        revision=revision,
        applied=sorted(restored),
        changes=changes(expected, restored),
        audit_source={
            "source": f"history:{action}",
            "original_source": entry["source"],
            "original_revision": entry["after_revision"],
            "watermark_preserved": watermark["as_of"] if watermark else None,
        },
    )
    return result


def summary(db, owner, account, scope, revision):
    cursor = db.execute(
        "SELECT * FROM position_history_cursors "
        "WHERE owner=? AND account=? AND chat_scope=?",
        (owner, account, scope),
    ).fetchone()
    guarded = cursor is not None and cursor["revision"] == revision
    redo = json.loads(cursor["redo_ids"]) if cursor else []
    head = _entry(db, owner, account, scope, cursor["head_id"]) if cursor else None
    next_redo = _entry(db, owner, account, scope, redo[-1]) if redo else None
    latest = db.execute(
        "SELECT * FROM position_history_entries "
        "WHERE owner=? AND account=? AND chat_scope=? "
        "ORDER BY after_revision DESC LIMIT 1",
        (owner, account, scope),
    ).fetchone()
    return {
        "revision": revision,
        "undo_available": bool(guarded and head and head["state"] == "active"),
        "redo_available": bool(
            guarded and next_redo and next_redo["state"] == "undone"
        ),
        "blocked_reason": "account_changed_since_history"
        if cursor and not guarded
        else None,
        "last_change": {
            "history_id": latest["history_id"],
            "source": latest["source"],
            "created_at": latest["created_at"],
            "state": latest["state"],
            "changes": changes(
                json.loads(latest["before_state"]), json.loads(latest["after_state"])
            ),
        }
        if latest
        else None,
    }
