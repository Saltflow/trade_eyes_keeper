"""Task-scoped progress, redacted diagnostics and non-blocking presentation.

SQLite events remain authoritative. A locked HTML page or failed notification
must never turn an already paid model response into a failed research task.
"""

from __future__ import annotations

import html
import json
import logging
import queue
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .common import atomic_write, now

logger = logging.getLogger(__name__)
TERMINAL = {"complete", "completed", "finished", "partial", "failed", "cancelled"}
IMPORTANT = {
    "run_started",
    "run_failed",
    "run_finished",
    "budget_stop",
    "repair_started",
    "repair_candidate_ready",
    "repair_approved",
    "repair_failed",
    "awaiting_approval",
    "source_blocked",
    "storage_blocked",
    "worker_lost",
}
PRIVATE_KEYS = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token|cookie|credential)"
)
SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s\"']+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;\"']+"
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if PRIVATE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_TEXT.sub(
            lambda m: (m.group(1) or m.group(2) or "") + "[REDACTED]", value
        )
    return value


def run_spec(run: dict) -> dict:
    value = run.get("spec", {})
    return json.loads(value) if isinstance(value, str) else value


def progress_snapshot(store, run_id: str) -> dict:
    run = store.get_run(run_id)
    spec = run_spec(run)
    events = (
        store.tail_events(run_id, limit=200)
        if hasattr(store, "tail_events")
        else store.events(run_id, limit=200)
    )
    result = {key: value for key, value in run.items() if key != "spec"}
    result.update(
        run_id=run_id,
        companies=spec.get("companies", []),
        origin=spec.get("origin", {}),
        budget=store.budget(run_id),
        events=events,
        updated_at=now(),
    )
    if spec.get("authority_root"):
        from src.shared_contracts import AuthorityRef

        result["authority"] = AuthorityRef(
            Path(spec["authority_root"]),
            isolated=bool(spec.get("authority_isolated", False)),
        ).to_dict()
        result["execution_store"] = str(store.execution.path)
    if hasattr(store, "active_attempts"):
        attempts = store.active_attempts(run_id)
        result["active"] = [
            {
                key: attempt.get(key)
                for key in ("key", "phase", "company", "status", "sent_intent")
            }
            for attempt in attempts
        ]
        result["activity"] = {
            "queued": sum(item.get("status") == "queued" for item in attempts),
            "sending_intent": sum(
                item.get("status") == "sending" and bool(item.get("sent_intent"))
                for item in attempts
            ),
            "notice": "Sending intent is durable host evidence, not proof of provider receipt; unknown handlers can still hold capacity.",
        }
    progress = {}
    for event in events:
        if event.get("event") == "stream_progress":
            progress[event.get("task_key", "unknown")] = {
                key: event.get(key)
                for key in ("task_key", "phase", "received_bytes", "last_byte_at")
            }
    if progress:
        result["stream_activity"] = list(progress.values())
        result["last_stream_at"] = max(
            (item.get("last_byte_at") or "") for item in progress.values()
        )
    return redact(result)


class ProgressPublisher:
    """Progress derived from the durable store; no public HTTP listener.

    ``sender(origin, payload)`` is a host-owned allowlisted route. It must return
    ``sent``/``failed``/``unknown`` (or a dict with ``state``); unknown is never
    retried. With no explicit Feishu origin, publication remains local only.
    """

    def __init__(self, store, run_id: str, output: Path, sender=None):
        self.store, self.run_id = store, run_id
        self.output = Path(output).resolve()
        self.sender = sender
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._log = None
        self._notification_queue = queue.Queue()
        self._notification_stop = threading.Event()
        self._notification_thread = None
        self._emergency_thread = None
        self._emergency_active = False

    @staticmethod
    def _emergency_stderr(record: dict) -> None:
        try:
            print(
                json.dumps(redact(record), ensure_ascii=False)[:4096],
                file=sys.stderr,
                flush=True,
            )
        except (OSError, ValueError):
            pass

    def emergency(self, error, *, spec) -> dict:
        """Alert a ledger outage using trusted in-memory context, never SQLite.

        This host-only fallback cannot send model requests, repair or resume.
        A writable output marker is required for cross-process delivery dedupe;
        if it is unavailable, stderr is the remaining local evidence channel.
        """
        from .models import ResearchSpec
        from .parsing import hash_lock

        if (
            not isinstance(spec, ResearchSpec)
            or Path(spec.output).resolve() != self.output
        ):
            raise ValueError("emergency_requires_trusted_run_spec")
        self._emergency_active = True
        self._stop.set()
        origin = json.loads(json.dumps(spec.origin))
        payload = redact(
            {
                "run_id": self.run_id,
                "event": "ledger_unavailable",
                "state": "ledger_unavailable",
                "error": str(error),
                "at": now(),
                "requests_not_restarted": True,
                "active": "unknown",
                "requires_explicit_resume": True,
            }
        )
        marker = self.output / "emergency/ledger_unavailable.json"
        allowed_origin = (
            origin.get("channel") == "feishu"
            and bool(origin.get("chat"))
            and bool(origin.get("sender"))
        )
        receipt = {
            "state": "queued" if allowed_origin else "local_only",
            "origin": origin,
            "payload": payload,
            "at": now(),
        }
        try:
            with hash_lock(marker.with_suffix(".lock"), timeout=0.2):
                if marker.exists():
                    receipt = json.loads(marker.read_text("utf-8"))
                    if receipt.get("origin") != origin:
                        self._emergency_stderr(
                            {**payload, "reason": "emergency_origin_mismatch"}
                        )
                        return {
                            "state": "failed",
                            "reason": "emergency_origin_mismatch",
                        }
                else:
                    atomic_write(marker, receipt, exclusive=True)
        except (OSError, ValueError) as failure:
            receipt.update(
                state="local_error",
                reason=redact(str(failure)),
                outbound_not_attempted=True,
            )
            self._emergency_stderr(receipt)
            return receipt
        snapshot = {
            "run_id": self.run_id,
            "state": "ledger_unavailable",
            "phase": "halted",
            "companies": list(spec.companies),
            "active": "unknown",
            "budget": "ledger unavailable; costs and active requests are unknown",
            "error": payload["error"],
            "requires_resume": True,
            "requests_not_restarted": True,
            "updated_at": now(),
            "events": [payload],
        }
        try:
            atomic_write(self.output / "progress.json", snapshot)
            atomic_write(self.output / "index.html", self._html(snapshot))
        except (OSError, ValueError) as failure:
            self._emergency_stderr({**payload, "presentation_error": str(failure)})
        if not allowed_origin or receipt.get("state") != "queued":
            return receipt
        delivery = getattr(self.sender, "emergency", None)
        if not callable(delivery):
            self._emergency_stderr(
                {**payload, "reason": "emergency_sender_unavailable"}
            )
            return receipt
        trusted_run = {
            "id": self.run_id,
            "run_id": self.run_id,
            "spec": {"origin": origin},
        }

        def deliver():
            try:
                with hash_lock(marker.with_suffix(".lock"), timeout=0.2):
                    current = json.loads(marker.read_text("utf-8"))
                    if current.get("state") != "queued":
                        return
                    current.update(state="unknown", send_intent_at=now())
                    atomic_write(marker, current)
                result = delivery(origin, current["payload"], trusted_run)
                state = (
                    result.get("state", "unknown")
                    if isinstance(result, dict)
                    else "unknown"
                )
                current.update(
                    state=state
                    if state in {"sent", "failed", "unknown"}
                    else "unknown",
                    finished_at=now(),
                )
                atomic_write(marker, current)
            except Exception as failure:  # noqa: BLE001 - no ledger is available
                try:
                    current = json.loads(marker.read_text("utf-8"))
                    if current.get("state") == "unknown":
                        current["delivery_error"] = redact(str(failure))
                        atomic_write(marker, current)
                except (OSError, ValueError):
                    pass
                self._emergency_stderr(
                    {
                        **payload,
                        "delivery_error": str(failure),
                        "delivery_state": "unknown",
                    }
                )

        with self._lock:
            if self._emergency_thread is None:
                self._emergency_thread = threading.Thread(
                    target=deliver,
                    name=f"research-emergency-{self.run_id[:8]}",
                    daemon=True,
                )
                self._emergency_thread.start()
        return receipt

    def _log_record(self, record: dict) -> None:
        try:
            if self._log is None:
                self.output.mkdir(parents=True, exist_ok=True)
                self._log = RotatingFileHandler(
                    self.output / "events.log",
                    maxBytes=2 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                    delay=True,
                )
                self._log.setFormatter(logging.Formatter("%(message)s"))
            text = json.dumps(redact(record), ensure_ascii=False)
            if len(text) > 8192:
                from .common import fingerprint

                text = json.dumps(
                    {
                        "event": record.get("event"),
                        "truncated": True,
                        "sha256": fingerprint(record),
                        "preview": text[:6000],
                    },
                    ensure_ascii=False,
                )
            self._log.emit(
                logging.LogRecord("research", logging.INFO, "", 0, text, (), None)
            )
        except (OSError, ValueError, TypeError) as exc:
            self.store.event(
                self.run_id,
                "log_sink_failed",
                error=redact(str(exc)),
                core_queue_continues=True,
            )

    def start(self) -> ProgressPublisher:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._heartbeat,
                name=f"research-progress-{self.run_id[:8]}",
                daemon=True,
            )
            self._thread.start()
        self.retry_notifications()
        return self

    def _heartbeat(self) -> None:
        while not self._stop.wait(5):
            try:
                self.store.update_run(self.run_id, heartbeat_at=now())
                self.publish()
                self.retry_notifications()
            except Exception:
                logger.exception(
                    "Research heartbeat could not persist for %s", self.run_id
                )
                self._stop.set()

    def event(self, event: str, **fields) -> dict:
        # Deliberately outside a catch: inability to persist is a safety fault.
        record = self.store.event(self.run_id, event, **redact(fields))
        self._log_record(record)
        self.publish()
        if event in IMPORTANT:
            try:
                self.enqueue_notification(event, fields)
            except (OSError, ValueError, TypeError) as exc:
                self.store.event(
                    self.run_id,
                    "notification_failed",
                    error=redact(str(exc)),
                    reason="delivery_receipt_storage_unavailable",
                    core_queue_continues=True,
                )
        return record

    def observe(self, record: dict) -> None:
        """Present an already-persisted runtime event without inserting twice."""
        self._log_record(record)
        self.publish()
        event = record.get("event")
        if event in IMPORTANT:
            try:
                self.enqueue_notification(
                    event,
                    {
                        k: v
                        for k, v in record.items()
                        if k not in {"event", "run_id", "seq", "at"}
                    },
                )
            except (OSError, ValueError, TypeError) as exc:
                self.store.event(
                    self.run_id, "notification_failed", error=redact(str(exc))
                )

    def publish(self) -> dict:
        with self._lock:
            if self._emergency_active:
                path = self.output / "progress.json"
                return (
                    json.loads(path.read_text("utf-8"))
                    if path.exists()
                    else {
                        "run_id": self.run_id,
                        "state": "ledger_unavailable",
                        "active": "unknown",
                    }
                )
            snapshot = progress_snapshot(self.store, self.run_id)
            try:
                self.output.mkdir(parents=True, exist_ok=True)
                atomic_write(self.output / "progress.json", snapshot)
                atomic_write(self.output / "index.html", self._html(snapshot))
            except (OSError, ValueError, TypeError) as exc:
                self.store.event(
                    self.run_id,
                    "presentation_failed",
                    error=redact(str(exc)),
                    scope="derived_progress_page",
                    core_queue_continues=True,
                )
            return snapshot

    def _notification_plan(self, event: str, fields: dict):
        run = self.store.get_run(self.run_id)
        origin = run_spec(run).get("origin", {})
        if (
            origin.get("channel") != "feishu"
            or not origin.get("chat")
            or not origin.get("sender")
        ):
            return None
        notification_dir = self.output / "notifications"
        notification_dir.mkdir(parents=True, exist_ok=True)
        # One event/cause is a stable delivery unit. No request id or time noise.
        from .common import fingerprint

        key = fingerprint(
            {
                "event": event,
                "cause": fields.get("cause") or fields.get("incident_id"),
                "task": fields.get("task_key"),
                "run": self.run_id,
            }
        )
        path = notification_dir / (key + ".json")
        payload = redact(
            {
                "run_id": self.run_id,
                "event": event,
                **fields,
                "status": run.get("state"),
                "at": now(),
            }
        )
        return origin, key, path, payload

    def enqueue_notification(self, event: str, fields: dict) -> dict:
        """Persist a queued intent; the core thread never calls a channel."""
        from .parsing import hash_lock

        planned = self._notification_plan(event, fields)
        if planned is None:
            return {"state": "local_only"}
        origin, _, path, payload = planned
        with hash_lock(path.with_suffix(".lock"), timeout=0.2):
            existing = json.loads(path.read_text("utf-8")) if path.exists() else None
            if existing and existing.get("state") in {"sent", "unknown"}:
                return existing
            if not existing:
                existing = {
                    "state": "queued",
                    "origin": origin,
                    "payload": payload,
                    "at": now(),
                    "attempts": 0,
                }
                atomic_write(path, existing, exclusive=True)
        self._notification_queue.put((event, fields))
        with self._lock:
            if self.sender is not None and self._notification_thread is None:
                self._notification_thread = threading.Thread(
                    target=self._notification_worker,
                    name=f"research-notify-{self.run_id[:8]}",
                    daemon=True,
                )
                self._notification_thread.start()
        return existing

    def _notification_worker(self) -> None:
        while (
            not self._notification_stop.is_set() or not self._notification_queue.empty()
        ):
            try:
                event, fields = self._notification_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.notify(event, fields)
            except Exception as exc:  # noqa: BLE001 - isolate outbound transport failures
                # Ordinary channel faults cannot break or block paid execution.
                logger.warning(
                    "Research notification unavailable: %s", redact(str(exc))
                )
                try:
                    self.store.event(
                        self.run_id,
                        "notification_failed",
                        error=redact(str(exc)),
                        core_queue_continues=True,
                    )
                except Exception:
                    logger.exception("Notification diagnostic could not persist")
            finally:
                self._notification_queue.task_done()

    def notify(self, event: str, fields: dict) -> dict:
        """Synchronous delivery boundary; callers on core paths must enqueue."""
        import time

        from .parsing import hash_lock

        planned = self._notification_plan(event, fields)
        if planned is None:
            return {"state": "local_only"}
        origin, key, path, payload = planned
        # Intent is persisted before contacting the channel. An interrupted send
        # stays unknown, rather than becoming an unbounded duplicate alert.
        with hash_lock(path.with_suffix(".lock"), timeout=0.2):
            existing = json.loads(path.read_text("utf-8")) if path.exists() else None
            if existing and (
                existing.get("state") not in {"queued", "failed"}
                or existing.get("attempts", 0) >= 3
                or existing.get("next_retry_at", 0) > time.time()
            ):
                return existing
            payload = (existing or {}).get("payload", payload)
            receipt = {
                "state": "unknown",
                "origin": origin,
                "payload": payload,
                "at": now(),
                "attempts": (existing or {}).get("attempts", 0) + 1,
            }
            atomic_write(path, receipt, exclusive=not existing)
        if self.sender is None:
            receipt.update(state="failed", reason="feishu_sender_not_attached")
        else:
            try:
                result = self.sender(origin, payload)
                state = (
                    result.get("state", "unknown")
                    if isinstance(result, dict)
                    else result
                )
                receipt.update(
                    state=state if state in {"sent", "failed", "unknown"} else "unknown"
                )
            except Exception as exc:  # noqa: BLE001 - uncertain delivery is never replayed
                receipt.update(state="unknown", reason=redact(str(exc)))
        if receipt["state"] == "failed":
            receipt["next_retry_at"] = time.time() + 10 * receipt["attempts"]
        atomic_write(path, receipt)
        self.store.event(
            self.run_id,
            "notification_receipt",
            state=receipt["state"],
            notification_id=key,
            reason=receipt.get("reason"),
        )
        return receipt

    def retry_notifications(self) -> None:
        for path in sorted((self.output / "notifications").glob("*.json"))[:100]:
            try:
                receipt = json.loads(path.read_text("utf-8"))
                if (
                    receipt.get("state") in {"queued", "failed"}
                    and receipt.get("attempts", 0) < 3
                ):
                    payload = receipt["payload"]
                    self.enqueue_notification(
                        payload["event"],
                        {
                            k: v
                            for k, v in payload.items()
                            if k not in {"event", "run_id", "at", "status"}
                        },
                    )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.store.event(
                    self.run_id,
                    "notification_receipt_unreadable",
                    error=redact(str(exc)),
                )

    def close(self) -> None:
        import time

        deadline = time.monotonic() + 2
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, deadline - time.monotonic()))
        self._notification_stop.set()
        if self._notification_thread is not None:
            self._notification_thread.join(timeout=max(0, deadline - time.monotonic()))
        if self._emergency_thread is not None:
            self._emergency_thread.join(timeout=max(0, deadline - time.monotonic()))
        try:
            self.publish()
        except Exception as failure:  # noqa: BLE001 - preserve the original core error
            self._emergency_stderr(
                {
                    "run_id": self.run_id,
                    "event": "close_publish_failed",
                    "error": str(failure),
                }
            )
        if self._log is not None:
            self._log.close()

    @staticmethod
    def _html(value: dict) -> str:
        escape = lambda item: html.escape(str(item), quote=True)
        run_id, state = value["run_id"], value.get("state", "unknown")
        rows = []
        for key in [
            "phase",
            "company",
            "active",
            "activity",
            "stream_activity",
            "tasks",
            "last_stream_at",
            "heartbeat_at",
            "host_heartbeat_at",
            "host_owner",
            "worker_exit",
            "waiting_reason",
            "repair_state",
            "error",
            "gaps",
            "budget",
        ]:
            if key in value:
                rows.append(
                    f"<tr><th>{escape(key)}</th><td><pre>{escape(json.dumps(value[key], ensure_ascii=False, indent=2))}</pre></td></tr>"
                )
        events = "".join(
            f"<li><time>{escape(event.get('at', event.get('created_at', '')))}</time> "
            f"{escape(event.get('event', event.get('kind', 'event')))} "
            f"<pre>{escape(json.dumps(event, ensure_ascii=False))}</pre></li>"
            for event in value.get("events", [])[-100:]
        )
        refresh = "" if state in TERMINAL else "setTimeout(()=>location.reload(),5000);"
        return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>研究进度 {escape(run_id)}</title>
<style>body{{font:15px/1.6 system-ui,'Microsoft YaHei',sans-serif;max-width:1300px;margin:28px auto;padding:0 20px;color:#183046;background:#f5f8fc}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{border:1px solid #dce3ec;padding:10px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}}.state{{padding:14px;background:#e5edf7;border-left:5px solid #3576b3}}li{{margin:12px 0}}time{{color:#667}}</style>
<h1>研究任务</h1><p><code>{escape(run_id)}</code></p><p class="state">状态：<strong>{escape(state)}</strong>　最后页面快照：{escape(value["updated_at"])}</p>
<p>本页只呈现持久化事实。任务数不代表字段提取率或财务正确性。无新字节不等于进程死亡。</p>
<p><a href="progress.json">完整进度JSON</a> · <a href="delivery/index.html">交付导航（生成后可用）</a></p>
<table>{"".join(rows)}</table><h2>任务时间线</h2><ol>{events}</ol>
<script>const k='research-scroll-{escape(run_id)}';addEventListener('beforeunload',()=>sessionStorage.setItem(k,String(scrollY)));scrollTo(0,Number(sessionStorage.getItem(k)||0));{refresh}</script></html>"""


def bounded_log_tail(store, run_id: str, *, after: int = 0, limit: int = 100) -> dict:
    if (
        type(limit) is not int
        or not 1 <= limit <= 200
        or type(after) is not int
        or after < 0
    ):
        raise ValueError("invalid_log_window")
    store.get_run(run_id)
    events = (
        store.tail_events(run_id, limit=limit)
        if after == 0 and hasattr(store, "tail_events")
        else store.events(run_id, after=after, limit=limit)
    )
    return {
        "run_id": run_id,
        "events": redact(events),
        "bounded": True,
        "notice": "Only task-scoped structured events; raw credentials and arbitrary paths are unavailable.",
    }


class FeishuOriginSender:
    """Existing outbound Bot transport with an explicit owner-bound origin.

    Construction is offline. Credentials/config are loaded only when a real
    origin notification is sent; a CLI run never guesses a destination chat.
    """

    def __init__(self, repo: Path, store, app=None):
        self.repo, self.store, self.app = Path(repo).resolve(), store, app

    def emergency(self, origin: dict, payload: dict, trusted_run: dict) -> dict:
        """Narrow host fallback: exact trusted origin + existing Bot allowlist.

        Never consult the broken ledger, upload arbitrary files, repair or resume.
        This method is absent from model/CLI tool schemas.
        """
        if (
            payload.get("run_id") != trusted_run.get("id")
            or payload.get("event") != "ledger_unavailable"
            or origin != run_spec(trusted_run).get("origin")
            or origin.get("channel") != "feishu"
        ):
            return {"state": "failed", "reason": "origin_mismatch"}
        if self.app is None:
            from src.core.config_store import load_config
            from src.interactive.feishu_app import FeishuApp

            self.app = FeishuApp(load_config(self.repo / "config/config.yaml"))
        chat = origin.get("chat")
        if not chat or not origin.get("sender") or not self.app.gate.is_allowed(chat):
            return {"state": "failed", "reason": "origin_not_allowlisted"}
        text = (
            f"【研究任务紧急告警】{payload['run_id']}\n账本不可用，已停止新的付费派发。"
            "在途状态与费用未知；未自动恢复、重发或启动维修。\n"
            + json.dumps(redact(payload), ensure_ascii=False)[:1800]
        )
        result = self.app.send_personal_message(chat, text)
        state = (
            result.get("outcome", "unknown") if isinstance(result, dict) else "unknown"
        )
        return {"state": state if state in {"sent", "failed", "unknown"} else "unknown"}

    def __call__(self, origin: dict, payload: dict) -> dict:
        run = self.store.get_run(payload["run_id"])
        if origin != run_spec(run).get("origin") or origin.get("channel") != "feishu":
            return {"state": "failed", "reason": "origin_mismatch"}
        if self.app is None:
            from src.core.config_store import load_config
            from src.interactive.feishu_app import FeishuApp

            self.app = FeishuApp(load_config(self.repo / "config/config.yaml"))
        chat = origin.get("chat")
        if not chat or not origin.get("sender") or not self.app.gate.is_allowed(chat):
            return {"state": "failed", "reason": "origin_not_allowlisted"}
        text = (
            f"【研究任务】{payload['run_id']}\n事件：{payload['event']}\n"
            f"状态：{payload.get('status', run.get('state'))}\n"
            + json.dumps(
                redact(
                    {
                        k: v
                        for k, v in payload.items()
                        if k not in {"run_id", "event", "status"}
                    }
                ),
                ensure_ascii=False,
            )[:1800]
            + "\n可直接询问本任务进度、日志或结果，无需SSH。"
        )
        result = self.app.send_personal_message(chat, text)
        outcome = (
            result.get("outcome", "unknown") if isinstance(result, dict) else "unknown"
        )
        response = {
            "state": outcome if outcome in {"sent", "failed", "unknown"} else "unknown"
        }
        if payload["event"] in {"run_finished", "run_complete", "run_partial"}:
            response["files"] = self.deliver_files(origin, run)
        return response

    def deliver_files(self, origin: dict, run: dict) -> list[dict]:
        """Send safe, bounded public artifacts using the Bot's existing upload."""
        from .common import fingerprint

        output = Path(run_spec(run)["output"]).resolve()
        targets = [
            output / "delivery/index.html",
            output / "research/最终报告.md",
            output / "research/最终报告.html",
            output / "peer/完整分析.md",
        ]
        import zipfile

        public_json = {
            "delivery/accounting.json",
            "delivery/hashes.json",
            "delivery/manifest.json",
            "delivery/source_coverage.json",
            "source_coverage.json",
        }
        public = [output / name for name in public_json if (output / name).is_file()]
        for folder in ("delivery", "research", "peer", "summaries", "contexts"):
            for path in (output / folder).rglob("*"):
                relative = path.relative_to(output)
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or not path.resolve().is_relative_to(output)
                    or any(
                        part in {"jobs", "requests", "views", "repairs"}
                        for part in relative.parts
                    )
                    or (
                        path.suffix.lower() not in {".md", ".html", ".png", ".csv"}
                        and relative.as_posix() not in public_json
                    )
                ):
                    continue
                if path not in public:
                    public.append(path)
        if public and sum(p.stat().st_size for p in public) <= 100 * 1024 * 1024:
            bundle = output / "delivery/research_public_bundle.zip"
            if not bundle.exists():
                bundle.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(
                    bundle, "x", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for path in public:
                        if path.is_symlink() or not path.resolve().is_relative_to(
                            output
                        ):
                            continue
                        if path.suffix.lower() == ".json":
                            archive.writestr(
                                path.relative_to(output).as_posix(),
                                json.dumps(
                                    redact(json.loads(path.read_text("utf-8"))),
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                            )
                        else:
                            archive.write(path, path.relative_to(output).as_posix())
            targets.insert(0, bundle)
        receipts = []
        for path in targets:
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(output)
            ):
                continue
            if path.stat().st_size > 20 * 1024 * 1024:
                receipts.append(
                    {
                        "name": path.name,
                        "state": "failed",
                        "reason": "file_above_bot_delivery_bound",
                    }
                )
                continue
            marker = (
                output
                / "notifications/files"
                / (fingerprint(str(path.relative_to(output))) + ".json")
            )
            if marker.exists():
                receipts.append(json.loads(marker.read_text("utf-8")))
                continue
            receipt = {
                "name": path.relative_to(output).as_posix(),
                "state": "unknown",
                "at": now(),
            }
            atomic_write(marker, receipt, exclusive=True)
            try:
                result = self.app.send_file(origin["chat"], path)
                receipt["state"] = "sent" if result and result[0] else "unknown"
            except Exception as exc:  # noqa: BLE001 - ambiguous upload must not be retried
                receipt["reason"] = redact(str(exc))
            atomic_write(marker, receipt)
            receipts.append(receipt)
        return receipts
