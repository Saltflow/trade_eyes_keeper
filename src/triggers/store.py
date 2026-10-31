"""Trigger definitions and an action outbox in the existing executor SQLite."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import ACTION_KINDS, normalize_spec, now_utc


def _encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _owner(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("authenticated owner is required")
    return value


def _number(value):
    if isinstance(value, str):
        match = re.fullmatch(r"(?:trigger)?([1-9]\d*)", value.strip(), re.IGNORECASE)
        value = int(match[1]) if match else value
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 2**63 - 1
    ):
        raise ValueError("a positive owner-local trigger number is required")
    return value


class TriggerStore:
    """Use an explicit database path; never find a real profile implicitly.

    Methods accepting ``db`` neither commit nor roll back that connection.
    ``transaction`` can coordinate action admission with ExecutionStore.submit
    using its existing ``db=`` argument, without any network call in the lock.
    """

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("use an explicit temporary file for durable triggers")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as db:
            for sql in (
                "CREATE TABLE IF NOT EXISTS trigger_owners (owner TEXT PRIMARY KEY,next_number INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS trigger_definitions (owner TEXT NOT NULL,number INTEGER NOT NULL,label TEXT NOT NULL,spec TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,request_key TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(owner,number),UNIQUE(owner,request_key))",
                "CREATE TABLE IF NOT EXISTS trigger_requests (owner TEXT NOT NULL,request_key TEXT NOT NULL,number INTEGER NOT NULL,spec TEXT NOT NULL,PRIMARY KEY(owner,request_key))",
                "CREATE TABLE IF NOT EXISTS trigger_state (owner TEXT NOT NULL,number INTEGER NOT NULL,last_truth TEXT NOT NULL DEFAULT 'unknown',episode INTEGER NOT NULL DEFAULT 0,fired_once INTEGER NOT NULL DEFAULT 0,last_evaluated_at TEXT,last_fired_at TEXT,diagnostic TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(owner,number))",
                "CREATE TABLE IF NOT EXISTS trigger_occurrences (owner TEXT NOT NULL,number INTEGER NOT NULL,occurrence_key TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL,context TEXT NOT NULL,PRIMARY KEY(owner,number,occurrence_key))",
                "CREATE TABLE IF NOT EXISTS trigger_actions (id TEXT PRIMARY KEY,owner TEXT NOT NULL,number INTEGER NOT NULL,occurrence_key TEXT NOT NULL,action_index INTEGER NOT NULL,revision INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,run_id TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner,number,occurrence_key,action_index))",
                "CREATE INDEX IF NOT EXISTS trigger_action_pending ON trigger_actions(state,created_at)",
                "CREATE INDEX IF NOT EXISTS trigger_definition_active ON trigger_definitions(state,owner,number)",
            ):
                db.execute(sql)
            db.execute(
                "INSERT OR IGNORE INTO trigger_requests SELECT owner,request_key,number,spec FROM trigger_definitions"
            )
            if "result" not in {
                row[1] for row in db.execute("PRAGMA table_info(trigger_actions)")
            }:
                db.execute(
                    "ALTER TABLE trigger_actions ADD COLUMN result TEXT NOT NULL DEFAULT '{}'"
                )

    @contextmanager
    def _connection(self, db=None, *, write=False):
        if db is not None:
            if write and not db.in_transaction:
                raise ValueError(
                    "external writes require the caller's active transaction"
                )
            yield db
            return
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def transaction(self):
        return self._connection(write=True)

    @staticmethod
    def _definition(row):
        if row is None:
            return None
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["id"] = f"trigger{result['number']}"
        result["display_number"] = result["number"]
        return result

    @staticmethod
    def _action(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        if "context" in result:
            result["context"] = json.loads(result["context"])
        if "result" in result:
            result["result"] = json.loads(result["result"])
        result["action_id"] = result["id"]
        result["trigger_number"] = result["number"]
        result["trigger_id"] = f"trigger{result['number']}"
        return result

    def create(
        self, owner: str, spec: dict, request_key: str, now: datetime | None = None
    ):
        owner = _owner(owner)
        if (
            not isinstance(request_key, str)
            or not request_key
            or len(request_key) > 256
        ):
            raise ValueError("a bounded idempotency request key is required")
        spec = normalize_spec(spec)
        encoded, stamp = _encode(spec), now_utc(now).isoformat()
        with self.transaction() as db:
            old = db.execute(
                "SELECT * FROM trigger_requests WHERE owner=? AND request_key=?",
                (owner, request_key),
            ).fetchone()
            if old:
                if old["spec"] != encoded:
                    raise ValueError(
                        "request key already belongs to a different trigger"
                    )
                return self.get(owner, old["number"], db=db)
            db.execute(
                "INSERT OR IGNORE INTO trigger_owners(owner,next_number) VALUES(?,1)",
                (owner,),
            )
            number = db.execute(
                "SELECT next_number FROM trigger_owners WHERE owner=?", (owner,)
            ).fetchone()[0]
            db.execute(
                "UPDATE trigger_owners SET next_number=next_number+1 WHERE owner=?",
                (owner,),
            )
            db.execute(
                "INSERT INTO trigger_definitions VALUES(?,?,?,?,?,1,?,?,?)",
                (
                    owner,
                    number,
                    spec["label"],
                    encoded,
                    "active",
                    request_key,
                    stamp,
                    stamp,
                ),
            )
            db.execute(
                "INSERT INTO trigger_requests VALUES(?,?,?,?)",
                (owner, request_key, number, encoded),
            )
            db.execute(
                "INSERT INTO trigger_state(owner,number) VALUES(?,?)", (owner, number)
            )
            return self.get(owner, number, db=db)

    def get(self, owner, number, *, db=None):
        with self._connection(db) as connection:
            item = self._definition(
                connection.execute(
                    "SELECT * FROM trigger_definitions WHERE owner=? AND number=?",
                    (_owner(owner), _number(number)),
                ).fetchone()
            )
            return self._last_result(item, connection)

    @staticmethod
    def _last_result(item, db):
        if item is not None:
            row = db.execute(
                "SELECT state,kind,run_id,reason,result,updated_at FROM trigger_actions WHERE owner=? AND number=? ORDER BY updated_at DESC,action_index DESC LIMIT 1",
                (item["owner"], item["number"]),
            ).fetchone()
            item["last_result"] = (
                {**dict(row), "result": json.loads(row["result"])} if row else None
            )
        return item

    def list(self, owner, include_inactive=False, *, db=None):
        with self._connection(db) as connection:
            sql = (
                "SELECT * FROM trigger_definitions WHERE owner=?"
                + ("" if include_inactive else " AND state='active'")
                + " ORDER BY number"
            )
            return [
                self._last_result(self._definition(row), connection)
                for row in connection.execute(sql, (_owner(owner),))
            ]

    def active(self, owner=None, *, db=None):
        with self._connection(db) as connection:
            sql = "SELECT * FROM trigger_definitions WHERE state='active'"
            args = ()
            if owner is not None:
                sql += " AND owner=?"
                args = (_owner(owner),)
            return [
                self._definition(row)
                for row in connection.execute(sql + " ORDER BY owner,number", args)
            ]

    def list_all_active(self):
        return self.active()

    @staticmethod
    def _finish_if_settled(db, owner, number, stamp):
        definition = db.execute(
            "SELECT spec,state FROM trigger_definitions WHERE owner=? AND number=?",
            (owner, number),
        ).fetchone()
        if not definition or definition["state"] != "active":
            return False
        outstanding = db.execute(
            "SELECT COUNT(*) FROM trigger_actions WHERE owner=? AND number=? AND state IN ('pending','submitting','submitted')",
            (owner, number),
        ).fetchone()[0]
        if outstanding:
            return False
        spec = json.loads(definition["spec"])
        state = db.execute(
            "SELECT fired_once FROM trigger_state WHERE owner=? AND number=?",
            (owner, number),
        ).fetchone()
        finished = spec["match_policy"] == "once" and bool(state[0])
        schedule = spec.get("schedule", {})
        if schedule.get("kind") == "dates":
            seen = {
                row[0]
                for row in db.execute(
                    "SELECT occurrence_key FROM trigger_occurrences WHERE owner=? AND number=?",
                    (owner, number),
                )
            }
            finished = finished or all(
                "schedule:" + value in seen for value in schedule["times"]
            )
        if finished:
            db.execute(
                "UPDATE trigger_definitions SET state='done',updated_at=? WHERE owner=? AND number=?",
                (stamp, owner, number),
            )
        return finished

    @staticmethod
    def _cancel(db, owner, number, stamp):
        pending = db.execute(
            "UPDATE trigger_actions SET state='cancelled',cancel_requested=1,reason='trigger_inactive_or_changed',updated_at=? WHERE owner=? AND number=? AND state='pending'",
            (stamp, owner, number),
        ).rowcount
        uncertain = db.execute(
            "UPDATE trigger_actions SET cancel_requested=1,reason='revoked_during_submission',updated_at=? WHERE owner=? AND number=? AND state IN ('submitting','uncertain')",
            (stamp, owner, number),
        ).rowcount
        submitted = db.execute(
            "SELECT COUNT(*) FROM trigger_actions WHERE owner=? AND number=? AND state IN ('submitted','completed','succeeded')",
            (owner, number),
        ).fetchone()[0]
        return {
            "cancelled_actions": pending,
            "uncertain_actions": uncertain,
            "submitted_actions": submitted,
        }

    def set_state(self, owner, number, state, now=None):
        if state not in {"active", "paused"}:
            raise ValueError("state must be active or paused")
        owner, number, stamp = _owner(owner), _number(number), now_utc(now).isoformat()
        with self.transaction() as db:
            item = self.get(owner, number, db=db)
            if item is None or item["state"] == "deleted":
                raise KeyError(number)
            if item["state"] == "done":
                raise ValueError(
                    "completed trigger requires editing its future schedule"
                )
            if item["state"] != state:
                db.execute(
                    "UPDATE trigger_definitions SET state=?,revision=revision+1,updated_at=? WHERE owner=? AND number=?",
                    (state, stamp, owner, number),
                )
                if state == "paused":
                    self._cancel(db, owner, number, stamp)
                else:
                    self._finish_if_settled(db, owner, number, stamp)
            return self.get(owner, number, db=db)

    def pause(self, owner, number, **kwargs):
        return self.set_state(owner, number, "paused", **kwargs)

    def resume(self, owner, number, **kwargs):
        return self.set_state(owner, number, "active", **kwargs)

    def update(self, owner, number, changes, expected_revision, now=None):
        owner, number, stamp = _owner(owner), _number(number), now_utc(now).isoformat()
        if (
            not isinstance(changes, dict)
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
        ):
            raise TypeError("changes and an expected revision are required")
        with self.transaction() as db:
            item = self.get(owner, number, db=db)
            if item is None or item["state"] == "deleted":
                raise KeyError(number)
            if item["revision"] != expected_revision:
                raise ValueError("trigger revision changed; reload before editing")
            spec = dict(item["spec"])
            spec.update(changes)
            spec = normalize_spec(spec)
            condition_changed = spec.get("condition") != item["spec"].get("condition")
            self._cancel(db, owner, number, stamp)
            db.execute(
                "UPDATE trigger_definitions SET label=?,spec=?,revision=revision+1,updated_at=? WHERE owner=? AND number=?",
                (spec["label"], _encode(spec), stamp, owner, number),
            )
            if condition_changed:
                db.execute(
                    "UPDATE trigger_state SET last_truth='unknown',fired_once=0,last_fired_at=NULL,last_evaluated_at=NULL,diagnostic=? WHERE owner=? AND number=?",
                    (
                        _encode({"truth": "unknown", "reasons": ["condition_changed"]}),
                        owner,
                        number,
                    ),
                )
            if item["state"] == "done" and ("schedule" in changes or condition_changed):
                db.execute(
                    "UPDATE trigger_definitions SET state='active' WHERE owner=? AND number=?",
                    (owner, number),
                )
                db.execute(
                    "UPDATE trigger_state SET fired_once=0 WHERE owner=? AND number=?",
                    (owner, number),
                )
                self._finish_if_settled(db, owner, number, stamp)
            return self.get(owner, number, db=db)

    def remove(self, owner, numbers, now=None):
        owner, stamp = _owner(owner), now_utc(now).isoformat()
        if not isinstance(numbers, (list, tuple)) or not 1 <= len(numbers) <= 200:
            raise ValueError("a bounded list of trigger numbers is required")
        numbers = list(dict.fromkeys(_number(number) for number in numbers))
        result = {
            "removed": [],
            "already_deleted": [],
            "not_found": [],
            "cancelled_actions": 0,
            "uncertain_actions": 0,
            "submitted_actions": 0,
        }
        with self.transaction() as db:
            for number in numbers:
                item = self.get(owner, number, db=db)
                if item is None:
                    result["not_found"].append(number)
                elif item["state"] == "deleted":
                    result["already_deleted"].append(number)
                else:
                    db.execute(
                        "UPDATE trigger_definitions SET state='deleted',revision=revision+1,updated_at=? WHERE owner=? AND number=?",
                        (stamp, owner, number),
                    )
                    result["removed"].append(number)
                    for key, count in self._cancel(db, owner, number, stamp).items():
                        result[key] += count
        return result

    def evaluation_state(self, owner, number, *, db=None):
        with self._connection(db) as connection:
            row = connection.execute(
                "SELECT * FROM trigger_state WHERE owner=? AND number=?",
                (_owner(owner), _number(number)),
            ).fetchone()
            if row is None:
                raise KeyError(number)
            return dict(row)

    def record_state(self, owner, number, values, *, db):
        allowed = {
            "last_truth",
            "episode",
            "fired_once",
            "last_evaluated_at",
            "last_fired_at",
            "diagnostic",
        }
        if not values or set(values) - allowed:
            raise ValueError("invalid evaluator state")
        columns = list(values)
        db.execute(
            "UPDATE trigger_state SET "
            + ",".join(key + "=?" for key in columns)
            + " WHERE owner=? AND number=?",
            (*[values[key] for key in columns], _owner(owner), _number(number)),
        )

    def record_occurrence(self, trigger, key, context, now, *, db):
        stamp = now_utc(now).isoformat()
        row = db.execute(
            "SELECT state,revision FROM trigger_definitions WHERE owner=? AND number=?",
            (trigger["owner"], trigger["number"]),
        ).fetchone()
        if (
            row is None
            or row["state"] != "active"
            or row["revision"] != trigger["revision"]
        ):
            return []
        inserted = db.execute(
            "INSERT OR IGNORE INTO trigger_occurrences VALUES(?,?,?,?,?,?)",
            (
                trigger["owner"],
                trigger["number"],
                key,
                trigger["revision"],
                stamp,
                _encode(context),
            ),
        ).rowcount
        if not inserted:
            return []
        result = []
        for index, action in enumerate(trigger["spec"]["actions"]):
            identity = hashlib.sha256(
                _encode([trigger["owner"], trigger["number"], key, index]).encode()
            ).hexdigest()
            db.execute(
                "INSERT INTO trigger_actions(id,owner,number,occurrence_key,action_index,revision,kind,payload,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    identity,
                    trigger["owner"],
                    trigger["number"],
                    key,
                    index,
                    trigger["revision"],
                    action["kind"],
                    _encode(action["payload"]),
                    stamp,
                    stamp,
                ),
            )
            result.append(
                {
                    **self._action(
                        db.execute(
                            "SELECT * FROM trigger_actions WHERE id=?", (identity,)
                        ).fetchone()
                    ),
                    "context": context,
                }
            )
        return result

    def actions(self, owner, number=None, *, db=None):
        with self._connection(db) as connection:
            sql, args = "SELECT * FROM trigger_actions WHERE owner=?", [_owner(owner)]
            if number is not None:
                sql += " AND number=?"
                args.append(_number(number))
            return [
                self._action(row)
                for row in connection.execute(sql + " ORDER BY created_at,id", args)
            ]

    def action_allowed(self, action_id, *, db=None):
        with self._connection(db) as connection:
            row = connection.execute(
                "SELECT a.state,a.cancel_requested,a.revision,d.state AS trigger_state,d.revision AS current_revision FROM trigger_actions a JOIN trigger_definitions d USING(owner,number) WHERE a.id=?",
                (action_id,),
            ).fetchone()
            return bool(
                row
                and row["trigger_state"] == "active"
                and row["revision"] == row["current_revision"]
                and not row["cancel_requested"]
                and row["state"] in {"pending", "submitting", "submitted"}
            )

    def get_action(self, action_id, *, db=None):
        with self._connection(db) as connection:
            return self._action(
                connection.execute(
                    "SELECT * FROM trigger_actions WHERE id=?", (action_id,)
                ).fetchone()
            )

    def list_inflight_actions(self, limit=1000):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10000
        ):
            raise ValueError("invalid inflight limit")
        with self._connection() as db:
            has_execution = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_runs'"
            ).fetchone()
            sql = "SELECT a.* FROM trigger_actions a "
            if has_execution:
                sql += "LEFT JOIN execution_runs r ON r.id=a.run_id "
            sql += (
                "WHERE a.state IN ('submitted','uncertain') AND a.run_id IS NOT NULL "
            )
            if has_execution:
                # Equal unknown/partial receipts are already reconciled. A late
                # durable status change must become visible even behind years
                # of uncertain history, without scanning it through a LIMIT.
                sql += (
                    "AND (a.state='submitted' OR (r.id IS NOT NULL AND "
                    "(json_extract(a.result,'$.status') IS NULL OR "
                    "r.status!=json_extract(a.result,'$.status')))) "
                )
            sql += "ORDER BY CASE WHEN a.state='submitted' THEN 0 ELSE 1 END,a.updated_at,a.id LIMIT ?"
            return [self._action(row) for row in db.execute(sql, (limit,))]

    def claim_pending(self, owner=None, limit=100, *, db=None, now=None, kinds=None):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid pending batch size")
        if kinds is not None:
            if not isinstance(kinds, (list, tuple, set, frozenset)) or any(
                kind not in ACTION_KINDS for kind in kinds
            ):
                raise ValueError("invalid action kind filter")
            if not kinds:
                return []
            kinds = sorted(set(kinds))
        with self._connection(db, write=True) as connection:
            sql = "SELECT a.*,o.context FROM trigger_actions a JOIN trigger_definitions d USING(owner,number) JOIN trigger_occurrences o USING(owner,number,occurrence_key) WHERE a.state='pending' AND a.cancel_requested=0 AND d.state='active' AND d.revision=a.revision"
            args = []
            if owner is not None:
                sql += " AND a.owner=?"
                args.append(_owner(owner))
            if kinds is not None:
                sql += " AND a.kind IN (" + ",".join("?" for _kind in kinds) + ")"
                args.extend(kinds)
            rows = connection.execute(
                sql
                + " ORDER BY a.created_at,a.owner,a.number,a.occurrence_key,a.action_index LIMIT ?",
                (*args, limit),
            ).fetchall()
            stamp = now_utc(now).isoformat()
            for row in rows:
                connection.execute(
                    "UPDATE trigger_actions SET state='submitting',updated_at=? WHERE id=? AND state='pending'",
                    (stamp, row["id"]),
                )
            return [{**self._action(row), "state": "submitting"} for row in rows]

    def _transition(
        self, action_id, state, *, run_id=None, reason="", db=None, now=None
    ):
        with self._connection(db, write=True) as connection:
            row = connection.execute(
                "SELECT * FROM trigger_actions WHERE id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise KeyError(action_id)
            allowed = {
                "submitted": {"submitting"},
                "uncertain": {"submitting", "submitted"},
                "failed": {"pending", "submitting", "submitted", "uncertain"},
                "completed": {"submitted", "uncertain"},
                "cancelled": {"pending", "submitting", "submitted", "uncertain"},
            }
            if row["state"] == state:
                if run_id is not None and row["run_id"] != run_id:
                    raise ValueError("action is already bound to a different run")
                return self._action(row)
            if row["state"] not in allowed.get(state, set()):
                raise ValueError(
                    "action transition would replay or overwrite a settled result"
                )
            connection.execute(
                "UPDATE trigger_actions SET state=?,run_id=COALESCE(?,run_id),reason=?,updated_at=? WHERE id=?",
                (
                    state,
                    run_id,
                    str(reason)[:2000],
                    now_utc(now).isoformat(),
                    action_id,
                ),
            )
            if state in {"uncertain", "failed", "completed", "cancelled"}:
                self._finish_if_settled(
                    connection, row["owner"], row["number"], now_utc(now).isoformat()
                )
            return self._action(
                connection.execute(
                    "SELECT * FROM trigger_actions WHERE id=?", (action_id,)
                ).fetchone()
            )

    def mark_submitted(self, action_id, run_id, **kwargs):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("execution run id is required")
        return self._transition(action_id, "submitted", run_id=run_id, **kwargs)

    def mark_uncertain(self, action_id, reason="submission outcome unknown", **kwargs):
        return self._transition(action_id, "uncertain", reason=reason, **kwargs)

    def mark_failed(self, action_id, reason="failed", **kwargs):
        return self._transition(action_id, "failed", reason=reason, **kwargs)

    def mark_completed(
        self,
        action_id,
        status="completed",
        reason="",
        deliveries=None,
        *,
        db=None,
        now=None,
    ):
        states = {
            "completed": "completed",
            "succeeded": "completed",
            "unknown": "uncertain",
            "partial": "uncertain",
            "failed": "failed",
            "cancelled": "cancelled",
            "skipped": "cancelled",
        }
        if status not in states:
            raise ValueError("unsupported execution outcome")
        result = {
            "status": status,
            "reason": str(reason)[:2000],
            "deliveries": deliveries,
        }
        encoded = _encode(result)
        if len(encoded.encode()) > 65536:
            raise ValueError("execution receipt is too large")
        with self._connection(db, write=True) as connection:
            current = self.get_action(action_id, db=connection)
            if current is None:
                raise KeyError(action_id)
            if current["state"] == states[status] and current["result"] == result:
                return current
            if (
                current["state"] in {"completed", "failed", "cancelled"}
                and current["result"]
                and current["result"] != result
            ):
                raise ValueError("settled action history cannot be overwritten")
            self._transition(
                action_id, states[status], reason=reason, db=connection, now=now
            )
            connection.execute(
                "UPDATE trigger_actions SET result=? WHERE id=?", (encoded, action_id)
            )
            return self.get_action(action_id, db=connection)

    def cancel_pending(self, action_id, **kwargs):
        return self._transition(action_id, "cancelled", **kwargs)

    def recover_submitting(self, now=None):
        """Call only after the service's exclusive execution-owner admission."""
        with self.transaction() as db:
            stamp = now_utc(now).isoformat()
            count = db.execute(
                "UPDATE trigger_actions SET state='uncertain',reason='submission_interrupted',updated_at=? WHERE state='submitting'",
                (stamp,),
            ).rowcount
            for trigger in self.active(db=db):
                self._finish_if_settled(db, trigger["owner"], trigger["number"], stamp)
            return count
