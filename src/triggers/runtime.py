"""Finite trigger jobs on the shared executor; no additional daemon or pool."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..core.config_store import DEFAULT_CONFIG_PATH
from ..core.execution import TERMINAL, JobSpec, ModuleSpec, RunResult
from ..core.report_execution import (
    REPORT_JOB_IDS,
    _settings_snapshot,
    build_report_payload,
    register_report_jobs,
    runtime_path,
)
from . import TriggerEvaluator, TriggerStore, collect_dependencies
from .evaluator import event_occurrences
from .facts import FactSnapshot, load_trigger_facts, monitored_symbols
from .schedule import due_times

logger = logging.getLogger(__name__)


class TriggerRuntime:
    MODULE = "triggers"
    POLL_SECONDS = 30
    SLOW_SECONDS = 300

    def __init__(
        self,
        config,
        execution,
        *,
        config_path=DEFAULT_CONFIG_PATH,
        config_provider=None,
        fact_loader=None,
        notifier_factory=None,
        clock=None,
    ):
        if runtime_path(config).resolve() != execution.store.path.resolve():
            raise ValueError("triggers and executor must share configured SQLite")
        self.execution = execution
        self.store = TriggerStore(execution.store.path)
        self.evaluator = TriggerEvaluator(self.store)
        self.config = copy.deepcopy(config)
        self.config_path = config_path
        self.config_provider = config_provider or (lambda: self.config)
        self.fact_loader = fact_loader or load_trigger_facts
        self.notifier_factory = notifier_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.started = False
        self.last_evaluated_at = None
        self.last_error = ""
        self._pending_run = None
        self._dirty = True
        self._last_data_poll = None
        self._pending_due = None
        self._snapshot = None
        self._slow_key = None
        self._slow_at = None
        self._slow_snapshot = None
        self._slow_loaded_key = None
        self._refresh_pending = None
        self.execution.bind_lifecycle(self.MODULE, self._lifecycle)
        self.execution.register(
            ModuleSpec(
                id=self.MODULE,
                version="1",
                jobs=(
                    JobSpec(
                        id="triggers.evaluate",
                        handler=self._evaluate,
                        lane="trigger_evaluation",
                        concurrency_key="triggers.evaluate",
                        timeout_seconds=600,
                        recovery="unknown",
                    ),
                    JobSpec(
                        id="triggers.refresh",
                        handler=self._refresh_slow,
                        lane="trigger_sources",
                        concurrency_key="triggers.refresh",
                        timeout_seconds=600,
                        recovery="unknown",
                    ),
                    JobSpec(
                        id="triggers.due",
                        handler=self._due,
                        lane="trigger_clock",
                        concurrency_key="triggers.due",
                        timeout_seconds=60,
                        recovery="unknown",
                    ),
                    JobSpec(
                        id="triggers.reminder",
                        handler=self._reminder,
                        lane="trigger_delivery",
                        timeout_seconds=60,
                        recovery="unknown",
                    ),
                ),
            )
        )

    def start(self):
        state = self.execution.module_status(self.MODULE)
        if state and state["enabled"]:
            self.execution.enable(self.MODULE)

    def _lifecycle(self, enabled):
        with self._lock:
            self.started = bool(enabled)
            if not enabled:
                self.execution.cancel_timer("triggers:poll")
                return
        # RuntimeService calls start only after exclusive process/host admission.
        self.store.recover_submitting(now=self.clock())
        self.kick()

    def kick(self):
        """A cheap wake hint from CRUD; tables remain the source of truth."""
        with self._lock:
            self._dirty = True
            if not self.started:
                return
            self.execution.add_timer(
                "triggers:poll",
                self.clock(),
                self._timer,
                module_id=self.MODULE,
            )

    def _arm(self):
        with self._lock:
            if not self.started:
                return
            now = self.clock()
            deadline = now + timedelta(seconds=self.POLL_SECONDS)
            due = self.evaluator.next_deadline(now, self._snapshot)
            if due is not None:
                deadline = min(deadline, due)
            self.execution.add_timer(
                "triggers:poll", deadline, self._timer, module_id=self.MODULE
            )

    def _timer(self):
        try:
            self.reconcile()
            active = self.store.active()
            now = self.clock()
            due_work = self._has_unseen_due(active, now)
            if due_work or self._has_pending():
                with self._lock:
                    old_due = (
                        self.execution.store.get(self._pending_due)
                        if self._pending_due
                        else None
                    )
                    if old_due is None or old_due["status"] in TERMINAL:
                        self._pending_due = self.execution.submit(
                            "triggers.due",
                            {},
                            request_id="due:" + uuid4().hex,
                            source="trigger_timer",
                        )
            dependencies = set().union(
                *(collect_dependencies(item["spec"]) for item in active)
            )
            interval = (
                self.POLL_SECONDS
                if any(path.startswith("quotes.") for path in dependencies)
                else self.SLOW_SECONDS
            )
            work = bool(dependencies) and (
                self._dirty
                or self._last_data_poll is None
                or (now - self._last_data_poll).total_seconds() >= interval
            )
            # Do not create durable execution rows for empty/future-only polls.
            if work:
                with self._lock:
                    old = (
                        self.execution.store.get(self._pending_run)
                        if self._pending_run
                        else None
                    )
                    if old is None or old["status"] in TERMINAL:
                        self._pending_run = self.execution.submit(
                            "triggers.evaluate",
                            {},
                            request_id="poll:" + uuid4().hex,
                            source="trigger_timer",
                        )
                        self._last_data_poll, self._dirty = now, False
        except Exception as exc:  # noqa: BLE001 - one provider cannot kill the host
            self.last_error = type(exc).__name__
            logger.warning("Trigger timer unavailable: %s", self.last_error)
        finally:
            self._arm()

    def _has_pending(self):
        with self.store._connection() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM trigger_actions WHERE state='pending' LIMIT 1"
                ).fetchone()
            )

    def _has_unseen_due(self, active, now):
        with self.store._connection() as db:
            for item in active:
                spec = item["spec"]
                if "schedule" in spec:
                    keys = [
                        "schedule:" + due.isoformat()
                        for due in due_times(spec["schedule"], now)
                    ]
                elif "event" in spec and self._snapshot is not None:
                    keys = [
                        event["key"]
                        for event in event_occurrences(
                            spec["event"], self._snapshot, now
                        )
                    ]
                else:
                    continue
                for key in keys:
                    if not db.execute(
                        "SELECT 1 FROM trigger_occurrences WHERE owner=? AND number=? AND occurrence_key=?",
                        (item["owner"], item["number"], key),
                    ).fetchone():
                        return True
        return False

    def _load_facts(self, config, dependencies, *, cancelled=None):
        from .snapshots import load_report_snapshot

        now = self.clock()
        session = load_report_snapshot(config)
        fast = {path for path in dependencies if path.startswith("quotes.")}
        slow = dependencies - fast
        key = self._facts_key(slow, config, session)
        if slow:
            with self._lock:
                current = (
                    self.execution.store.get(self._refresh_pending)
                    if self._refresh_pending
                    else None
                )
                if (
                    self._slow_key != key
                    or self._slow_at is None
                    or (now - self._slow_at).total_seconds() >= self.SLOW_SECONDS
                ) and (current is None or current["status"] in TERMINAL):
                    self._slow_key, self._slow_at = key, now
                    self._refresh_pending = self.execution.submit(
                        "triggers.refresh",
                        {"dependencies": sorted(slow), "key": key},
                        request_id="refresh:" + uuid4().hex,
                        source="trigger_sources",
                    )
        snapshot = (
            self.fact_loader(
                fast,
                config,
                as_of=self.clock(),
                session=session,
                clock=self.clock,
                budget_seconds=30,
                cancelled=cancelled,
            )
            if fast
            else FactSnapshot(as_of=self.clock(), observed_at=self.clock())
        )
        with self._lock:
            slow_snapshot = (
                self._slow_snapshot if self._slow_loaded_key == key else None
            )
        if slow and slow_snapshot:
            # Fresh realtime paths win; source clocks of cached facts stay intact.
            snapshot.facts = {
                **slow_snapshot.facts,
                **{
                    key: value
                    for key, value in snapshot.facts.items()
                    if key.startswith("quotes.")
                },
            }
            snapshot.events = list(slow_snapshot.events)
        snapshot.wildcard_symbols = tuple(monitored_symbols(config))
        return snapshot

    @staticmethod
    def _facts_key(dependencies, config, session):
        alert_path = Path(
            (config.get("alerts") or {}).get("config_path")
            or Path(__file__).resolve().parents[2] / "config" / "alerts.yaml"
        )
        try:
            alerts = (
                hashlib.sha256(alert_path.read_bytes()).hexdigest()
                if alert_path.stat().st_size <= 1024 * 1024
                else str(alert_path.stat().st_mtime_ns)
            )
        except OSError:
            alerts = "unavailable"
        return hashlib.sha256(
            json.dumps(
                [
                    sorted(dependencies),
                    monitored_symbols(config),
                    session.get("_fact_metadata", {}),
                    _settings_snapshot(config),
                    alerts,
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    def _refresh_slow(self, payload, ctx):
        from .snapshots import load_report_snapshot

        try:
            if ctx.cancel_event.is_set():
                return RunResult("skipped", "trigger_refresh_cancelled")
            config = self.config_provider()
            if runtime_path(config).resolve() != self.execution.store.path.resolve():
                raise ValueError("trigger runtime database path changed")
            session = load_report_snapshot(config)
            dependencies = set(payload["dependencies"])
            if self._facts_key(dependencies, config, session) != payload["key"]:
                return RunResult("skipped", "trigger_snapshot_changed")
            snapshot = self.fact_loader(
                dependencies,
                config,
                as_of=self.clock(),
                session=session,
                clock=self.clock,
                budget_seconds=120,
                cancelled=ctx.cancel_event.is_set,
            )
            if ctx.cancel_event.is_set():
                return RunResult("skipped", "trigger_refresh_cancelled")
            with self._lock:
                if self._slow_key != payload["key"]:
                    return RunResult("skipped", "trigger_dependencies_changed")
                self._slow_snapshot = snapshot
                self._slow_loaded_key = payload["key"]
            return RunResult("succeeded", "trigger_sources_refreshed")
        finally:
            self.kick()

    def _due(self, payload, ctx):
        """A clock lane remains available while free data sources are slow."""
        del payload
        try:
            if ctx.cancel_event.is_set():
                return RunResult("skipped", "trigger_due_cancelled")
            config = self.config_provider()
            if runtime_path(config).resolve() != self.execution.store.path.resolve():
                raise ValueError("trigger runtime database path changed")
            now = self.clock()
            snapshot = FactSnapshot(as_of=now, observed_at=now)
            snapshot.wildcard_symbols = tuple(monitored_symbols(config))
            if self._snapshot is not None:
                monitored = set(monitored_symbols(config))
                snapshot.events = [
                    event
                    for event in self._snapshot.events
                    if (
                        event.get("symbol") if isinstance(event, dict) else event.symbol
                    )
                    in monitored
                ]
            self.evaluator.evaluate(snapshot, now=now)
            self._submit_pending(config)
            self.reconcile()
            return RunResult("succeeded", "trigger_due_complete")
        finally:
            self._arm()

    def _evaluate(self, payload, ctx):
        del payload
        try:
            if ctx.cancel_event.is_set():
                return RunResult("skipped", "trigger_evaluation_cancelled")
            config = self.config_provider()
            if runtime_path(config).resolve() != self.execution.store.path.resolve():
                raise ValueError("trigger runtime database path changed")
            self.config = copy.deepcopy(config)
            # Manual dates must not wait behind slow market/disclosure sources.
            self.evaluator.evaluate(None, now=self.clock())
            self._submit_pending(config)
            dependencies = set().union(
                *(collect_dependencies(item["spec"]) for item in self.store.active())
            )
            if dependencies:
                snapshot = self._load_facts(
                    config, dependencies, cancelled=ctx.cancel_event.is_set
                )
                if ctx.cancel_event.is_set():
                    return RunResult("skipped", "trigger_evaluation_cancelled")
                self.evaluator.evaluate(snapshot, now=self.clock())
                self._snapshot = snapshot
                self._submit_pending(config)
            self.reconcile()
            self.last_evaluated_at = self.clock().isoformat()
            self.last_error = ""
            return RunResult("succeeded", "trigger_evaluation_complete")
        except Exception as exc:  # noqa: BLE001 - preserve pending actions on admission failure
            self.last_error = type(exc).__name__
            logger.warning("Trigger evaluation unavailable: %s", self.last_error)
            return RunResult("failed", "trigger_evaluation:" + self.last_error)
        finally:
            self._arm()

    def _binding(self, action, label, grouped=None):
        context = action.get("context", {})
        details = []
        if context.get("symbol"):
            details.append(str(context["symbol"]))
        if context.get("report_period"):
            details.append(str(context["report_period"]))
        if "day_offset" in context:
            details.append(
                "公告当天"
                if context["day_offset"] == 0
                else f"公告后第{context['day_offset']}天"
            )
        result = {
            "action_id": action["id"],
            "owner": action["owner"],
            "number": action["number"],
            "revision": action["revision"],
            "kind": action["kind"],
            "chat_id": action["payload"].get("chat_id"),
            "config_path": str(Path(self.config_path).resolve()),
            "reason": f"触发器 #{action['number']}：{label}"
            + (" · " + " · ".join(details) if details else ""),
        }
        if grouped and len(grouped) > 1:
            result["action_ids"] = sorted(item["id"] for item in grouped)
            symbols = sorted(
                {
                    item.get("context", {}).get("symbol")
                    for item in grouped
                    if item.get("context", {}).get("symbol")
                }
            )
            result["reason"] = (
                f"触发器 #{action['number']}：{label} · 本轮合并 {len(grouped)} 项公告事件 · "
                + "、".join(symbols)
            )
        return result

    def _submit_pending(self, config):
        # Register outside the shared transaction: registration owns its writes.
        register_report_jobs(self.execution, config=config)
        reports_enabled = bool(self.execution.module_status("reports")["enabled"])
        with self.store.transaction() as db:
            actions = self.store.claim_pending(
                limit=100,
                kinds=None if reports_enabled else ["reminder"],
                db=db,
                now=self.clock(),
            )
            groups = {}
            for action in actions:
                context = action.get("context", {})
                key = (
                    (
                        action["owner"],
                        action["number"],
                        action["revision"],
                        action["action_index"],
                        action["kind"],
                        action["payload"].get("chat_id"),
                        context.get("date"),
                    )
                    if action["kind"] in {"brief", "daily"} and context.get("event_id")
                    else (action["id"],)
                )
                groups.setdefault(key, []).append(action)
            for grouped in groups.values():
                action = grouped[0]
                trigger = self.store.get(action["owner"], action["number"], db=db)
                binding = self._binding(action, trigger["label"], grouped)
                if not isinstance(binding["chat_id"], str) or not binding["chat_id"]:
                    for member in grouped:
                        self.store.mark_failed(
                            member["id"], "authenticated_chat_missing", db=db
                        )
                    continue
                if action["kind"] == "reminder":
                    job = "triggers.reminder"
                    payload = {
                        "binding": binding,
                        "text": action["payload"].get("text") or trigger["label"],
                    }
                else:
                    job = REPORT_JOB_IDS[action["kind"]]
                    routed = copy.deepcopy(config)
                    routed["_trigger_context"] = binding
                    # No stock subset override: report config remains the full pool.
                    payload = build_report_payload(
                        routed,
                        self.execution,
                        kind=action["kind"],
                        force=True,
                        report_id=action["payload"].get(
                            "report_id", "morning_snapshot"
                        ),
                        config_path=self.config_path,
                    )
                identity = (
                    hashlib.sha256(
                        "|".join(sorted(item["id"] for item in grouped)).encode()
                    ).hexdigest()
                    if len(grouped) > 1
                    else action["id"]
                )
                run_id = self.execution.store.submit(
                    job,
                    payload,
                    request_id="trigger:" + identity,
                    source="trigger",
                    db=db,
                )
                for member in grouped:
                    self.store.mark_submitted(
                        member["id"], run_id, db=db, now=self.clock()
                    )
        self.execution.wake()

    def _reminder(self, payload, ctx):
        from .delivery import TriggerChatNotifier, binding_status

        config = copy.deepcopy(self.config_provider())
        config["_trigger_context"] = payload["binding"]
        state = binding_status(config, run_id=ctx.run_id)
        if state != "allowed":
            return RunResult(
                "skipped" if state == "revoked" else "failed",
                "trigger_revoked" if state == "revoked" else "trigger_binding_invalid",
            )
        if not ctx.mark_side_effect("trigger_message_starting"):
            return RunResult("skipped", "trigger_message_cancelled")
        notifier = (self.notifier_factory or TriggerChatNotifier)(config)
        result = notifier.send_reminder(str(payload["text"]))
        return RunResult(
            {
                "sent": "succeeded",
                "failed": "failed",
                "skipped": "skipped",
                "unknown": "unknown",
                "partial": "partial",
            }.get(result.get("status"), "unknown"),
            result.get("reason", "delivery_receipt_missing"),
            deliveries={"feishu": result},
        )

    def reconcile(self):
        """Settle from durable run receipts, including recovery after a restart."""
        for action in self.store.list_inflight_actions():
            run = (
                self.execution.store.get(action["run_id"])
                if action.get("run_id")
                else None
            )
            if run is None:
                self.store.mark_uncertain(action["id"], "execution_receipt_missing")
            elif run["status"] in TERMINAL:
                result = run.get("result") or {}
                self.store.mark_completed(
                    action["id"],
                    status=run["status"],
                    reason=result.get("reason") or run.get("reason", ""),
                    deliveries=result.get("deliveries"),
                    now=self.clock(),
                )
            elif (
                not self.store.action_allowed(action["id"])
                and run["status"] == "queued"
            ):
                self.execution.cancel(run["id"])

    def health(self):
        return {
            "started": self.started,
            "active_count": len(self.store.active()),
            "last_evaluated_at": self.last_evaluated_at,
            "error": self.last_error or None,
        }
