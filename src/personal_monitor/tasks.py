"""Personal-monitor jobs registered with the shared execution host.

Business facts and the delivery state machine stay in MonitorStore. Timers only
wake the host; an idle reconciliation never creates an execution record.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, time, timedelta, timezone
from time import monotonic

import pytz

from ..core.execution import JobSpec, ModuleSpec, RunResult

logger = logging.getLogger(__name__)


def market_schedule(config: dict) -> tuple[object, list[tuple[time, bool, int]]]:
    """Use report clocks, independently of each report's delivery switch."""
    scheduler = config.get("scheduler", {}) or {}
    zone = pytz.timezone(scheduler.get("timezone", "Asia/Shanghai"))

    def grace(value, default):
        try:
            result = int(value)
            return result if result > 0 else default
        except (TypeError, ValueError):
            return default

    specs = [
        (
            scheduler.get("run_time", "19:00"),
            False,
            grace(scheduler.get("daily_misfire_grace_seconds"), 3600),
        )
    ]
    specs.extend(
        (
            brief.get("run_time", "09:50"),
            brief.get("skip_weekends", True),
            grace(
                brief.get(
                    "misfire_grace_seconds",
                    scheduler.get("brief_misfire_grace_seconds"),
                ),
                900,
            ),
        )
        for brief in scheduler.get("brief_reports", [])
    )
    parsed = []
    for clock, weekends, window in specs:
        selected = time.fromisoformat(str(clock))
        if selected.tzinfo or selected.second or selected.microsecond:
            raise ValueError("personal market clock must be a local HH:MM")
        parsed.append((selected, bool(weekends), window))
    return zone, parsed


def market_deadlines(config: dict, now: datetime):
    """Return latest catch-up slot and next future slot using current quotes."""
    zone, specs = market_schedule(config)
    local = now.astimezone(zone)
    eligible, future = [], []
    for offset in range(-1, 9):
        day = local.date() + timedelta(days=offset)
        for clock, weekends, grace in specs:
            if weekends and day.weekday() >= 5:
                continue
            try:
                scheduled = zone.localize(datetime.combine(day, clock), is_dst=None)
            except pytz.exceptions.NonExistentTimeError:
                continue
            except pytz.exceptions.AmbiguousTimeError:
                # The stable wall-clock slot runs only once on a repeated hour.
                scheduled = zone.localize(datetime.combine(day, clock), is_dst=False)
            if scheduled <= now:
                if (now - scheduled).total_seconds() <= grace:
                    eligible.append(scheduled)
            else:
                future.append(scheduled)
    return max(eligible, default=None), min(future, default=None)


class PersonalMonitorTasks:
    """Register finite business jobs; own no threads, pools or polling loop."""

    MODULE = "personal_monitor"
    RECONCILE_SECONDS = 60
    RETRY_SECONDS = 60
    MAX_MARKET_ATTEMPTS = 3
    DELIVERY_BATCH_LIMIT = 8
    DELIVERY_BATCH_SECONDS = 5

    def __init__(self, monitor, execution):
        self.monitor, self.execution = monitor, execution
        self._lock = threading.RLock()
        self.started = False
        self._pending = {}
        self._retry_after = {}
        self._config = None
        self._version = ""
        self._last_refresh = None
        self._attempts = {}
        self.last_error = ""
        self._persistent = isinstance(getattr(monitor.store, "path", None), str)
        if self._persistent:
            bind_connections = getattr(monitor.store, "set_connection_provider", None)
            if callable(bind_connections):
                bind_connections(self.execution.store.connection)
            with monitor.store.connection(write=True) as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS personal_market_slots (
                      slot TEXT PRIMARY KEY, completed_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS personal_market_attempts (
                      slot TEXT PRIMARY KEY, attempts INTEGER NOT NULL,
                      last_attempt_at TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS personal_reminder_due
                      ON reminders(state,due_at);
                    CREATE INDEX IF NOT EXISTS personal_outbox_due
                      ON outbox(state,next_attempt_at,created_at);
                    CREATE INDEX IF NOT EXISTS personal_outbox_lease
                      ON outbox(state,claimed_at);
                    """
                )
        self.execution.bind_lifecycle(self.MODULE, self._lifecycle)

    def _module(self, version, *, enabled=True):
        common = {"timeout_seconds": 300, "recovery": "unknown"}
        return ModuleSpec(
            id=self.MODULE,
            version=version,
            enabled=enabled,
            jobs=(
                JobSpec(
                    id="personal.market",
                    handler=self._market,
                    lane="personal_market",
                    concurrency_key="personal.market",
                    **common,
                ),
                JobSpec(
                    id="personal.due",
                    handler=self._due,
                    lane="personal_due",
                    concurrency_key="personal.due",
                    **common,
                ),
                JobSpec(
                    id="personal.delivery",
                    handler=self._delivery,
                    lane="personal_delivery",
                    concurrency_key="personal.delivery",
                    **common,
                ),
            ),
        )

    def apply_config(self, config):
        # Only clocks belong in the durable task snapshot. Provider credentials
        # remain in the business adapter's in-memory configuration.
        candidate = {"scheduler": copy.deepcopy(config.get("scheduler", {}))}
        market_schedule(candidate)  # Reject an invalid replacement before mutation.
        encoded = json.dumps(candidate.get("scheduler", {}), sort_keys=True)
        version = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        with self._lock:
            registered = self.execution.is_registered(self.MODULE)
            if self._version != version or not registered:
                state = self.execution.module_status(self.MODULE)
                module = self._module(
                    version, enabled=bool(state["enabled"]) if state else True
                )
                previous = self._config, self._version
                # apply can synchronously notify lifecycle outside the host
                # lock. Publish the validated snapshot before that callback.
                self._config, self._version = candidate, version
                try:
                    if registered:
                        self.execution.apply(module)
                    else:
                        self.execution.register(module)
                except BaseException:
                    self._config, self._version = previous
                    raise
                self._refresh_pending()
            self._last_refresh = datetime.now(timezone.utc)
            self.last_error = ""

    def slot_completed(self, slot):
        if not self._persistent:
            return self.monitor._market_slot == slot
        with self.monitor.store.connection() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM personal_market_slots WHERE slot=?", (slot,)
                ).fetchone()
            )

    def complete_slot(self, slot, now):
        if self._persistent:
            with self.monitor.store.connection(write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO personal_market_slots VALUES(?,?)",
                    (slot, now.isoformat()),
                )
        self.monitor._market_slot = slot

    def _market_attempt(self, slot, now):
        """Bound retries across host restarts; evaluation itself is idempotent."""
        if self._persistent:
            with self.monitor.store.connection(write=True) as db:
                row = db.execute(
                    "SELECT attempts,last_attempt_at FROM personal_market_attempts "
                    "WHERE slot=?",
                    (slot,),
                ).fetchone()
                previous = (row[0], datetime.fromisoformat(row[1])) if row else None
                attempt = self._next_attempt(previous, now)
                if attempt:
                    db.execute(
                        "INSERT OR REPLACE INTO personal_market_attempts VALUES(?,?,?)",
                        (slot, attempt, now.isoformat()),
                    )
                return attempt
        previous = self._attempts.get(slot)
        attempt = self._next_attempt(previous, now)
        if attempt:
            self._attempts[slot] = attempt, now
        return attempt

    def _next_attempt(self, previous, now):
        if previous:
            count, last_at = previous
            if count >= self.MAX_MARKET_ATTEMPTS:
                self.last_error = "market_attempts_exhausted"
                return None
            retry_at = last_at + timedelta(seconds=self.RETRY_SECONDS)
            if now < retry_at:
                self._timer("market_retry", retry_at, self.reconcile)
                return None
            return count + 1
        return 1

    def _timer(self, kind, at, callback):
        key = f"{self.MODULE}:{kind}"
        if at is None:
            self.execution.cancel_timer(key)
        else:
            self.execution.add_timer(key, at, callback, module_id=self.MODULE)

    def next_deadlines(self, now):
        """Only indexed state/deadline queries; never enumerate account facts."""
        if not self._persistent:
            return None, None
        with self.monitor.store.connection() as db:
            due = db.execute(
                "SELECT MIN(due_at) FROM reminders WHERE state='pending'"
            ).fetchone()[0]
            queued = db.execute(
                "SELECT 1 FROM outbox WHERE state='queued' LIMIT 1"
            ).fetchone()
            failed = db.execute(
                "SELECT MIN(COALESCE(next_attempt_at,created_at)) "
                "FROM outbox WHERE state='failed'"
            ).fetchone()[0]
            claimed = db.execute(
                "SELECT MIN(claimed_at) FROM outbox "
                "WHERE state IN ('claimed','submitted')"
            ).fetchone()[0]
        candidates = [now] if queued else []
        if failed:
            candidates.append(datetime.fromisoformat(failed))
        if claimed:
            candidates.append(datetime.fromisoformat(claimed) + timedelta(seconds=300))
        return (
            datetime.fromisoformat(due) if due else None,
            min(candidates, default=None),
        )

    def _submit(self, kind, payload=None, request_id=None):
        with self._lock:
            if not self.started or kind in self._pending:
                return
            self._pending[kind] = None
        try:
            run_id = self.execution.submit(
                f"personal.{kind}",
                payload or {},
                request_id=request_id or uuid.uuid4().hex,
                module_id=self.MODULE,
                config_version=self._version,
                source="timer",
            )
            with self._lock:
                self._pending[kind] = run_id
        except BaseException:
            with self._lock:
                self._pending.pop(kind, None)
            raise

    def _refresh_pending(self):
        for kind, run_id in list(self._pending.items()):
            if run_id is None:
                continue
            row = self.execution.get_run(run_id)
            if (
                row
                and row["status"] not in {"queued", "running"}
                and not (row.get("claim_token") and row.get("released_at") is None)
            ):
                self._pending.pop(kind, None)

    def reconcile(self, *, refresh=False, read_config=True):
        with self._lock:
            if not self.started:
                return
            now = datetime.now(timezone.utc)
            try:
                self._refresh_pending()
                if read_config and (
                    refresh
                    or self._last_refresh is None
                    or (now - self._last_refresh).total_seconds()
                    >= self.RECONCILE_SECONDS
                ):
                    self.apply_config(self.monitor.config_provider())
                catchup, future = market_deadlines(self._config, now)
                if catchup is not None:
                    slot = catchup.strftime("%Y-%m-%dT%H:%M%z")
                    if "market" not in self._pending and not self.slot_completed(slot):
                        attempt = self._market_attempt(slot, now)
                        if attempt:
                            self._submit(
                                "market",
                                {
                                    "slot": slot,
                                    "scheduled_for": catchup.isoformat(),
                                    "config": self._config,
                                    "attempt": attempt,
                                },
                                f"personal.market:{slot}:{attempt}",
                            )
                self._timer("market", future, lambda: self.reconcile(refresh=True))
                due, delivery = self.next_deadlines(now)
                if self._persistent and due is None and delivery is None:
                    # Keep no business SQLite cache resident between deadlines.
                    # A concurrent borrower remains owned until it returns.
                    self.execution.store.close_idle_connections(
                        database=self.monitor.store.path, autocommit=True
                    )
                for kind, deadline in (("due", due), ("delivery", delivery)):
                    if kind in self._pending:
                        deadline = None
                    elif deadline is not None and kind in self._retry_after:
                        deadline = max(deadline, self._retry_after[kind])
                    self._timer(
                        kind,
                        deadline,
                        lambda selected=kind: self._submit(selected),
                    )
                self._timer(
                    "reconcile",
                    now + timedelta(seconds=self.RECONCILE_SECONDS),
                    self.reconcile,
                )
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Personal module reconciliation failed")
                self._timer(
                    "reconcile",
                    now + timedelta(seconds=self.RECONCILE_SECONDS),
                    lambda: self.reconcile(refresh=True),
                )

    def _finished(self, kind):
        with self._lock:
            self._pending.pop(kind, None)
        self.reconcile()

    def _market(self, payload, context):
        try:
            now = datetime.now(timezone.utc)
            scheduled = datetime.fromisoformat(payload["scheduled_for"])
            zone, specs = market_schedule(payload["config"])
            local = scheduled.astimezone(zone)
            elapsed = (now - scheduled).total_seconds()
            if not any(
                local.time().replace(tzinfo=None) == clock
                and not (weekends and local.weekday() >= 5)
                and 0 <= elapsed <= grace
                for clock, weekends, grace in specs
            ):
                return RunResult(status="skipped", reason="market_slot_expired")
            result = self.monitor.check_market(
                now,
                slot=payload["slot"],
                config=payload["config"],
                context=context,
            )
            return RunResult(
                reason="market_data_not_ready" if result["not_ready"] else "",
                artifacts=result,
            )
        finally:
            self._finished("market")

    def _due(self, _payload, context):
        try:
            context.mark_side_effect("personal_due_enqueue")
            ids = self.monitor.engine.enqueue_due(datetime.now(timezone.utc))
            self._retry_after.pop("due", None)
            return RunResult(artifacts={"enqueued": ids})
        except Exception:
            self._retry_after["due"] = datetime.now(timezone.utc) + timedelta(
                seconds=self.RETRY_SECONDS
            )
            raise
        finally:
            self._finished("due")

    def _delivery(self, _payload, context):
        try:
            receipts = []
            deadline = monotonic() + self.DELIVERY_BATCH_SECONDS
            for _ in range(self.DELIVERY_BATCH_LIMIT):
                if not self.started or context.cancel_event.is_set():
                    break
                if receipts and monotonic() >= deadline:
                    break
                result = self.monitor.deliver_one(
                    datetime.now(timezone.utc), context=context
                )
                if result is None:
                    break
                receipts.append(result)
                if result["outcome"] != "sent":
                    break
            self._retry_after.pop("delivery", None)
            if not receipts:
                return RunResult(status="skipped", reason="no_deliverable_message")
            result = receipts[-1]
            outcome = result["outcome"]
            return RunResult(
                status={
                    "sent": "succeeded",
                    "failed": "failed",
                    "unknown": "unknown",
                    "not_sent": "partial" if len(receipts) > 1 else "skipped",
                }[outcome],
                reason=result.get("error", ""),
                deliveries=receipts if len(receipts) > 1 else result,
            )
        except Exception:
            self._retry_after["delivery"] = datetime.now(timezone.utc) + timedelta(
                seconds=self.RETRY_SECONDS
            )
            raise
        finally:
            self._finished("delivery")

    def start(self):
        with self._lock:
            if self.started:
                return
            self.apply_config(self.monitor.config_provider())
            self.execution.bind_lifecycle(self.MODULE, self._lifecycle)
            enabled = bool(self.execution.module_status(self.MODULE)["enabled"])
        # Registration preserves an explicit hot-disable across process restarts.
        # Only the public enable operation can revoke that user decision.
        if enabled:
            self.execution.enable(self.MODULE)

    def stop(self, *, disable=True):
        if disable and self.execution.is_registered(self.MODULE):
            self.execution.disable(self.MODULE)
        else:
            self._lifecycle(False)

    def _lifecycle(self, enabled):
        """Called outside the host lock; never re-enter a lifecycle operation."""
        with self._lock:
            self.started = bool(enabled)
            if not enabled:
                for kind in ("market", "market_retry", "due", "delivery", "reconcile"):
                    self.execution.cancel_timer(f"{self.MODULE}:{kind}")
        if enabled:
            self.reconcile(read_config=False)
