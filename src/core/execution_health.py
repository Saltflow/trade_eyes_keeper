"""Durable execution notices using the existing control lane and event loop.

The caller owns destination policy and formatting. A sender receives an event
dictionary and returns ``{'outcome': 'sent'|'failed'|'unknown'}``. Only a known
non-delivery may be reported as failed. Missing receipts and exceptions are
unknown and are never automatically retried.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable

from .execution import JobSpec, ModuleDisabled, ModuleSpec, QueueFull, RunResult

MODULE = "execution_health"
JOB = "health.delivery"
TIMER = "execution_health.delivery"


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class HealthReporter:
    """Observe business failures without running a second supervisor loop."""

    def __init__(
        self,
        runtime,
        send_callback: Callable | None = None,
        *,
        exclusive: bool = False,
        retry_seconds: float = 60,
        max_attempts: int = 3,
    ):
        if send_callback is not None and not callable(send_callback):
            raise TypeError("sender must be callable or None")
        if (
            not math.isfinite(retry_seconds)
            or retry_seconds <= 0
            or not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("positive retry delay and attempt count are required")
        self.runtime = runtime
        self.store = runtime.store
        self.send_callback = send_callback
        self.retry_seconds = float(retry_seconds)
        self.max_attempts = int(max_attempts)
        self._stopped = True
        self._lock = threading.RLock()
        with self.store.connection() as db:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS execution_health_outbox (
                    id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, owner_id TEXT, run_id TEXT,
                    token TEXT, submitted REAL, finished REAL,
                    result TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS execution_health_ready
                    ON execution_health_outbox(state,available_at);
                CREATE TABLE IF NOT EXISTS execution_health_issues (
                    id TEXT PRIMARY KEY, origin TEXT NOT NULL,
                    module_id TEXT NOT NULL, kind TEXT NOT NULL,
                    reason TEXT NOT NULL, state TEXT NOT NULL,
                    episode INTEGER NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_health_meta (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
                COMMIT;
            """)
        self._recover_submitted(exclusive)
        runtime.register(
            ModuleSpec(
                MODULE,
                (
                    JobSpec(
                        JOB,
                        self._deliver,
                        lane="control",
                        concurrency_key=JOB,
                        timeout_seconds=120,
                    ),
                ),
            )
        )
        runtime.bind_lifecycle(MODULE, self._enabled)
        self.set_sender(send_callback)
        if runtime.module_status(MODULE)["enabled"]:
            self._enabled(True)

    def _recover_submitted(self, exclusive: bool) -> None:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT o.id,o.owner_id,o.run_id,r.released_at,r.claim_token,"
                "p.id AS known_owner FROM execution_health_outbox o "
                "LEFT JOIN execution_runs r ON r.id=o.run_id "
                "LEFT JOIN execution_owners p ON p.id=o.owner_id "
                "WHERE o.state='submitted'"
            ).fetchall()
        if not rows:
            return
        live = {owner["id"] for owner in self.store.active_owners()}
        with self.store.connection() as db:
            for row in rows:
                held = row["claim_token"] is not None and row["released_at"] is None
                if row["owner_id"] in live and held:
                    continue
                proven_offline = row["known_owner"] and row["owner_id"] not in live
                completed_owner_run = row["run_id"] and row["released_at"] is not None
                if not (exclusive or proven_offline or completed_owner_run):
                    continue
                db.execute(
                    "UPDATE execution_health_outbox SET state='unknown',updated=?,"
                    "finished=?,result=? WHERE id=? AND state='submitted'",
                    (
                        time.time(),
                        time.time(),
                        _json({"outcome": "unknown", "reason": "delivery_interrupted"}),
                        row["id"],
                    ),
                )

    def start(self) -> None:
        """Enable delivery without starting the owning execution runtime."""
        self.runtime.enable(MODULE)

    def stop(self, *, disable: bool = False) -> None:
        """Clean up locally; only explicit disable changes the persistent switch."""
        if disable:
            self.runtime.disable(MODULE)
        else:
            self._enabled(False)

    def _enabled(self, enabled: bool) -> None:
        with self._lock:
            self._stopped = not enabled
        if enabled:
            self._arm()
        else:
            self.runtime.cancel_timer(TIMER)

    def set_sender(self, callback: Callable | None) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("sender must be callable or None")
        with self._lock:
            self.send_callback = callback
        if callback is not None:
            with self.store.connection() as db:
                db.execute(
                    "UPDATE execution_health_outbox SET state='queued',updated=? "
                    "WHERE state='route_unconfigured'",
                    (time.time(),),
                )
        self._arm()

    def _insert(self, db, key: str, payload: dict) -> None:
        event_id = uuid.uuid4().hex
        now = time.time()
        payload = {**payload, "event_id": event_id}
        db.execute(
            "INSERT OR IGNORE INTO execution_health_outbox "
            "(id,event_key,payload,state,available_at,created,updated) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                event_id,
                key,
                _json(payload),
                "queued" if self.send_callback else "route_unconfigured",
                now,
                now,
                now,
            ),
        )

    def record_host_recovery(self, session_key: str, metadata: dict) -> None:
        """Queue one recovery summary without resolving any existing issue.

        A previous host session is immutable evidence, not an active liveness
        probe. Repeating it cannot replace a sent/submitted/unknown receipt.
        """
        if not isinstance(session_key, str) or not 1 <= len(session_key) <= 256:
            raise ValueError("host recovery requires a stable session key")
        allowed = {"pid", "birth", "started_at"}
        if not isinstance(metadata, dict) or set(metadata) != allowed:
            raise ValueError("host recovery requires exact previous-session evidence")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._insert(
                db,
                "host-recovery:" + session_key,
                {
                    "kind": "host_recovered",
                    "module_id": "host",
                    "previous_session": dict(metadata),
                    "reason": "上一宿主会话非计划中断；现已由原操作系统监督重新启动。"
                    "没有外部观察者时，整进程离线或主线程卡死期间不能实时通知。",
                },
            )
        self._arm()

    @staticmethod
    def _issue(value: dict, origin: str = "external") -> dict:
        if not isinstance(value, dict) or not value.get("id"):
            raise ValueError("each health issue requires a stable id")
        return {
            "id": str(value["id"])[:256],
            "origin": origin,
            "module_id": str(value.get("module_id", ""))[:160],
            "kind": str(value.get("kind", "unavailable"))[:80],
            "reason": str(value.get("reason", ""))[:2000],
        }

    def observe(
        self, execution_snapshot: dict, issues: Iterable[dict] | None = None
    ) -> dict:
        """Observe a snapshot. None preserves external issues; [] resolves them.

        Failed results are looked up by terminal state and unique event key,
        rather than a seq-only cursor: an older in-flight run may become
        unknown after newer runs finish, especially following a restart.
        """
        with self._lock:
            if self._stopped:
                return self.health()
        self._recover_submitted(exclusive=False)
        desired = (
            None
            if issues is None
            else {item["id"]: item for item in (self._issue(i) for i in issues)}
        )
        runtime_issue = None
        if execution_snapshot.get("failure"):
            runtime_issue = self._issue(
                {
                    "id": "execution.dispatcher",
                    "module_id": "execution",
                    "kind": "runtime_failure",
                    "reason": execution_snapshot["failure"],
                },
                "runtime",
            )
        with self.store.connection() as db:
            failures = db.execute(
                "SELECT r.id,r.seq,r.job_id,r.module_id,r.status,r.reason,r.stage,"
                "r.finished FROM execution_runs r LEFT JOIN execution_health_outbox o "
                "ON o.event_key='run:'||r.id WHERE r.status IN ('failed','partial','unknown') "
                "AND r.job_id<>? AND o.id IS NULL ORDER BY r.seq",
                (JOB,),
            ).fetchall()
            max_result = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM execution_runs "
                "WHERE status IN ('succeeded','skipped','failed','partial','unknown') "
                "AND job_id<>?",
                (JOB,),
            ).fetchone()[0]
            cursor = db.execute(
                "SELECT value FROM execution_health_meta WHERE key='last_result_seq'"
            ).fetchone()
            existing = {
                r["id"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM execution_health_issues WHERE state='active'"
                )
            }
            target = (
                dict(existing)
                if desired is None
                else {
                    key: item
                    for key, item in existing.items()
                    if item["origin"] != "external"
                }
            )
            if desired is not None:
                target.update(desired)
            target.pop("execution.dispatcher", None)
            if runtime_issue:
                target[runtime_issue["id"]] = runtime_issue
            changed_issues = set(existing) != set(target) or any(
                any(
                    existing[key].get(field) != value.get(field)
                    for field in ("module_id", "kind", "reason", "origin")
                )
                for key, value in target.items()
                if key in existing
            )
            if failures or changed_issues or max_result > (cursor[0] if cursor else 0):
                db.execute("BEGIN IMMEDIATE")
                for row in failures:
                    self._insert(
                        db,
                        "run:" + row["id"],
                        {
                            "kind": "execution_result",
                            "run_id": row["id"],
                            "job_id": row["job_id"],
                            "module_id": row["module_id"],
                            "status": row["status"],
                            "reason": row["reason"],
                            "stage": row["stage"],
                            "finished": row["finished"],
                        },
                    )
                self._update_issues(db, target, preserve_external=issues is None)
                if max_result:
                    db.execute(
                        "INSERT INTO execution_health_meta VALUES ('last_result_seq',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                        (max_result,),
                    )
        self._arm()
        return self.health()

    def _update_issues(
        self, db, desired: dict, preserve_external: bool = False
    ) -> None:
        rows = {
            r["id"]: dict(r)
            for r in db.execute("SELECT * FROM execution_health_issues")
        }
        if preserve_external:
            desired = {
                **{
                    key: row
                    for key, row in rows.items()
                    if row["origin"] == "external" and row["state"] == "active"
                },
                **desired,
            }
        now = time.time()
        for key, value in desired.items():
            old = rows.get(key)
            new_episode = old is None or old["state"] != "active"
            episode = (old["episode"] if old else 0) + int(new_episode)
            if new_episode:
                self._insert(
                    db,
                    f"issue:{key}:{episode}:down",
                    {
                        **value,
                        "kind": "issue_active",
                        "issue_kind": value["kind"],
                        "episode": episode,
                    },
                )
            if new_episode or any(old.get(k) != value.get(k) for k in value):
                db.execute(
                    "INSERT INTO execution_health_issues VALUES (?,?,?,?,?,'active',?,?) "
                    "ON CONFLICT(id) DO UPDATE SET origin=excluded.origin,"
                    "module_id=excluded.module_id,kind=excluded.kind,reason=excluded.reason,"
                    "state='active',episode=excluded.episode,updated=excluded.updated",
                    (
                        key,
                        value["origin"],
                        value["module_id"],
                        value["kind"],
                        value["reason"],
                        episode,
                        now,
                    ),
                )
        for key, old in rows.items():
            if old["state"] != "active" or key in desired:
                continue
            down = db.execute(
                "SELECT state FROM execution_health_outbox WHERE event_key=?",
                (f"issue:{key}:{old['episode']}:down",),
            ).fetchone()
            db.execute(
                "UPDATE execution_health_outbox SET state='superseded',updated=? "
                "WHERE event_key=? AND state IN ('queued','failed','route_unconfigured')",
                (now, f"issue:{key}:{old['episode']}:down"),
            )
            self._insert(
                db,
                f"issue:{key}:{old['episode']}:up",
                {
                    "kind": "issue_recovered",
                    "issue_kind": old["kind"],
                    "id": key,
                    "module_id": old["module_id"],
                    "reason": old["reason"],
                    "episode": old["episode"],
                    "previous_delivery": down[0] if down else None,
                },
            )
            db.execute(
                "UPDATE execution_health_issues SET state='resolved',updated=? WHERE id=?",
                (now, key),
            )

    def _arm(self) -> None:
        with self._lock:
            stopped, sender = self._stopped, self.send_callback
        if stopped or sender is None:
            self.runtime.cancel_timer(TIMER)
            return
        with self.store.connection() as db:
            due = db.execute(
                "SELECT MIN(available_at) FROM execution_health_outbox "
                "WHERE state IN ('queued','failed') AND attempts<?",
                (self.max_attempts,),
            ).fetchone()[0]
        if due is None:
            self.runtime.cancel_timer(TIMER)
        else:
            self.runtime.add_timer(TIMER, due, self._submit_delivery, module_id=MODULE)

    def _submit_delivery(self) -> None:
        with self._lock:
            if self._stopped or self.send_callback is None:
                return
        try:
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                ready = db.execute(
                    "SELECT 1 FROM execution_health_outbox "
                    "WHERE state IN ('queued','failed') AND available_at<=? "
                    "AND attempts<? LIMIT 1",
                    (time.time(), self.max_attempts),
                ).fetchone()
                if ready is None:
                    return
                if self.store.count_pending(JOB, db=db):
                    return
                self.store.submit(JOB, {}, source="health", db=db, max_pending=1)
        except (QueueFull, ModuleDisabled):
            return
        self.runtime.wake()

    def _claim(self, run_id: str) -> dict | None:
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM execution_health_outbox WHERE state IN ('queued','failed') "
                "AND available_at<=? AND attempts<? ORDER BY created,id LIMIT 1",
                (now, self.max_attempts),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE execution_health_outbox SET state='submitted',attempts=attempts+1,"
                "owner_id=?,run_id=?,token=?,submitted=?,updated=? WHERE id=?",
                (self.runtime.owner_id, run_id, token, now, now, row["id"]),
            )
            return {**dict(row), "token": token, "attempts": row["attempts"] + 1}

    @staticmethod
    def _outcome(value) -> dict:
        if isinstance(value, str):
            value = {"outcome": value}
        if not isinstance(value, dict) or value.get("outcome") not in {
            "sent",
            "failed",
            "unknown",
        }:
            return {"outcome": "unknown", "reason": "delivery_receipt_missing"}
        result = {"outcome": value["outcome"]}
        for key in ("message_id", "platform_message_id", "reason", "code"):
            if key in value and isinstance(value[key], (str, int, float, bool)):
                result[key] = str(value[key])[:1000]
        if "message_id" not in result and "platform_message_id" in result:
            result["message_id"] = result["platform_message_id"]
        return result

    def _finish(self, claim: dict, outcome: dict) -> None:
        now = time.time()
        retry = now + self.retry_seconds * 2 ** (claim["attempts"] - 1)
        with self.store.connection() as db:
            db.execute(
                "UPDATE execution_health_outbox SET state=?,result=?,updated=?,finished=?,"
                "available_at=? WHERE id=? AND token=? AND state='submitted'",
                (
                    outcome["outcome"],
                    _json(outcome),
                    now,
                    now,
                    retry if outcome["outcome"] == "failed" else now,
                    claim["id"],
                    claim["token"],
                ),
            )

    def _deliver(self, payload, context) -> RunResult:
        delivered, incomplete = 0, 0
        try:
            for _ in range(16):
                with self._lock:
                    stopped, sender = self._stopped, self.send_callback
                if stopped or sender is None or context.cancel_event.is_set():
                    break
                claim = self._claim(context.run_id)
                if claim is None:
                    break
                if (
                    not context.mark_side_effect("health_delivery_intent")
                    or context.cancel_event.is_set()
                ):
                    outcome = {"outcome": "failed", "reason": "cancelled_before_send"}
                else:
                    try:
                        outcome = self._outcome(sender(json.loads(claim["payload"])))
                    except Exception as exc:  # noqa: BLE001 - HTTP may have committed
                        outcome = {"outcome": "unknown", "reason": type(exc).__name__}
                self._finish(claim, outcome)
                delivered += outcome["outcome"] == "sent"
                incomplete += outcome["outcome"] != "sent"
        finally:
            self._arm()
        return RunResult(
            "partial" if incomplete else "succeeded" if delivered else "skipped",
            "health_delivery",
            deliveries={"sent": delivered, "incomplete": incomplete},
        )

    def health(self) -> dict:
        with self.store.connection() as db:
            counts = dict(
                db.execute(
                    "SELECT state,COUNT(*) FROM execution_health_outbox GROUP BY state"
                )
            )
            exhausted = db.execute(
                "SELECT COUNT(*) FROM execution_health_outbox WHERE state='failed' "
                "AND attempts>=?",
                (self.max_attempts,),
            ).fetchone()[0]
            issues = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM execution_health_issues WHERE state='active' ORDER BY id"
                )
            ]
        pending = (
            sum(
                counts.get(state, 0)
                for state in ("queued", "failed", "route_unconfigured")
            )
            - exhausted
        )
        return {
            "pending": pending,
            "unknown": counts.get("unknown", 0),
            "sending": counts.get("submitted", 0),
            "failed": counts.get("failed", 0),
            "exhausted": exhausted,
            "sent": counts.get("sent", 0),
            "issues": issues,
            "data_not_ready": [
                issue for issue in issues if issue["kind"] == "data_not_ready"
            ],
            "route_unconfigured": self.send_callback is None and pending > 0,
            "stopped": self._stopped,
        }
