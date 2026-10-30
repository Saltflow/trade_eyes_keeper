"""SQLite facts, rule state and delivery outbox for personal monitoring.

Each mutation uses a separate connection and an immediate transaction. This is
deliberate: callback threads and report subprocesses share the same database.
The database contains personal facts only; no strategy portfolio is imported.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

from . import history

SHANGHAI = ZoneInfo("Asia/Shanghai")


class RevisionConflict(ValueError):
    """The user or another recognition job changed the object meanwhile."""


def parse_time(value=None) -> datetime:
    """Require an explicit offset; never interpret server-local naive times."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timezone_aware_time_required")
    return value.astimezone(timezone.utc)


def reminder_time(*, now=None, days=None, on_date=None, at="09:50") -> datetime:
    """Resolve calendar days or a date in Beijing; caller supplies ambiguity."""
    current = parse_time(now).astimezone(SHANGHAI)
    if (days is None) == (on_date is None):
        raise ValueError("provide_days_or_date")
    if days is not None:
        if type(days) is not int or days < 0:
            raise ValueError("invalid_calendar_days")
        return (current + timedelta(days=days)).astimezone(timezone.utc)
    selected = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
    clock = time.fromisoformat(at)
    if not isinstance(selected, date) or clock.tzinfo is not None:
        raise ValueError("invalid_date_or_default_time")
    return datetime.combine(selected, clock, SHANGHAI).astimezone(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identity(*values):
    if any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in values):
        raise ValueError("invalid_identity")


def canonical_instrument(value):
    """One security identity across quote providers, screenshots and rules."""
    _identity(value)
    value = value.strip().upper()
    if value.endswith(".HK"):
        stem = value[:-3]
        if stem.isdigit() and 1 <= len(stem) <= 5:
            return stem.zfill(5)
        raise ValueError("invalid_hong_kong_instrument")
    for suffix in (".SH", ".SS", ".SZ", ".BJ"):
        if value.endswith(suffix):
            stem = value[:-3]
            if re.fullmatch(r"\d{6}", stem):
                return stem
            raise ValueError("invalid_mainland_instrument")
    if value.endswith(".US"):
        value = value[:-3]
    if re.fullmatch(r"\d{5,6}", value) or re.fullmatch(
        r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", value
    ):
        return value
    raise ValueError("ambiguous_or_invalid_instrument")


class MonitorStore:
    """Owner is the authenticated platform identity, never a model-selected ID."""

    def __init__(self, path):
        self._connection_provider = None
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            os.chmod(self.path, 0o600)
        with self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts (
                  owner TEXT NOT NULL, account TEXT NOT NULL, revision INTEGER NOT NULL,
                  PRIMARY KEY(owner, account));
                CREATE TABLE IF NOT EXISTS routes (
                  owner TEXT NOT NULL, route TEXT NOT NULL, PRIMARY KEY(owner, route));
                CREATE TABLE IF NOT EXISTS snapshot_watermarks (
                  owner TEXT NOT NULL, account TEXT NOT NULL, as_of TEXT NOT NULL,
                  PRIMARY KEY(owner, account));
                CREATE TABLE IF NOT EXISTS pending_issues (
                  owner TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
                  as_of TEXT NOT NULL, reason TEXT NOT NULL,
                  PRIMARY KEY(owner, account, instrument));
                CREATE TABLE IF NOT EXISTS positions (
                  owner TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
                  as_of TEXT NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL,
                  PRIMARY KEY(owner, account, instrument));
                CREATE TABLE IF NOT EXISTS snapshots (
                  owner TEXT NOT NULL, account TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                  result TEXT NOT NULL, PRIMARY KEY(owner, account, snapshot_id));
                CREATE TABLE IF NOT EXISTS rules (
                  owner TEXT NOT NULL, account TEXT NOT NULL, rule_id TEXT NOT NULL,
                  definition TEXT NOT NULL, PRIMARY KEY(owner, account, rule_id));
                CREATE TABLE IF NOT EXISTS exceptions (
                  owner TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
                  PRIMARY KEY(owner, account, instrument));
                CREATE TABLE IF NOT EXISTS tiers (
                  owner TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
                  rule_id TEXT NOT NULL, tier_id TEXT NOT NULL, state TEXT NOT NULL,
                  outbox_id TEXT NOT NULL,
                  PRIMARY KEY(owner, account, instrument, rule_id, tier_id));
                CREATE TABLE IF NOT EXISTS evaluations (
                  owner TEXT NOT NULL, account TEXT NOT NULL, instrument TEXT NOT NULL,
                  rule_id TEXT NOT NULL, checked_at TEXT NOT NULL, reason TEXT,
                  PRIMARY KEY(owner, account, instrument, rule_id));
                CREATE TABLE IF NOT EXISTS reminders (
                  owner TEXT NOT NULL, reminder_id TEXT NOT NULL, due_at TEXT NOT NULL,
                  text TEXT NOT NULL, route TEXT NOT NULL, revision INTEGER NOT NULL,
                  state TEXT NOT NULL, request_id TEXT,
                  PRIMARY KEY(owner, reminder_id), UNIQUE(owner, request_id));
                CREATE TABLE IF NOT EXISTS outbox (
                  id TEXT PRIMARY KEY, business_key TEXT NOT NULL UNIQUE,
                  owner TEXT NOT NULL, account TEXT, kind TEXT NOT NULL,
                  route TEXT NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL,
                  state TEXT NOT NULL, token TEXT, claimed_at TEXT, attempts INTEGER
                  NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL, next_attempt_at TEXT, metadata TEXT);
                """
            )
            columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
            if "next_attempt_at" not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT")
            if "metadata" not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN metadata TEXT")
            history.initialize(db)

    @contextmanager
    def _raw_connection(self):
        provider = self._connection_provider
        if provider is not None:
            with provider(database=self.path, autocommit=True) as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    def set_connection_provider(self, provider):
        """Accept a host-owned SQLite context provider without a core import."""
        if not callable(provider):
            raise TypeError("connection provider must be callable")
        self._connection_provider = provider

    @contextmanager
    def connection(self, write=False):
        with self._raw_connection() as db:
            try:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
                if write:
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise

    @staticmethod
    def _account(db, owner, account, expected_revision=None):
        _identity(owner, account)
        row = db.execute(
            "SELECT revision FROM accounts WHERE owner=? AND account=?",
            (owner, account),
        ).fetchone()
        if row is None:
            raise KeyError("account_not_found")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise RevisionConflict("account_revision_changed")
        return row["revision"]

    def ensure_account(self, owner: str, account: str) -> dict:
        _identity(owner, account)
        with self.connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,0)", (owner, account))
            return {"account": account, "revision": self._account(db, owner, account)}

    def register_route(self, owner, route):
        """Called by the authenticated event handler, never exposed to the model."""
        _identity(owner, route)
        with self.connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO routes VALUES(?,?)", (owner, route))

    def allowed_routes(self, owner):
        with self.connection() as db:
            return [
                r[0]
                for r in db.execute("SELECT route FROM routes WHERE owner=?", (owner,))
            ]

    @staticmethod
    def _route(db, owner, route):
        if not db.execute(
            "SELECT 1 FROM routes WHERE owner=? AND route=?", (owner, route)
        ).fetchone():
            raise PermissionError("route_not_registered_for_owner")

    def list_scopes(self):
        """Runtime enumeration; never expose this method as an assistant tool."""
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT owner,account FROM accounts")]

    list_accounts = list_scopes

    @staticmethod
    def cancel_pending_thresholds(db, owner, account, instrument=None, rule_id=None):
        """Invalidate facts that have not crossed the HTTP submission boundary.

        A reservation is not a delivered tier. Release all undelivered tiers in
        an invalidated combined message so the next check can rebuild its group.
        Submitted, unknown and sent messages retain their consumed identities.
        """
        rows = db.execute(
            "SELECT id,payload FROM outbox WHERE owner=? AND account=? "
            "AND kind='threshold' AND state IN ('queued','failed','claimed')",
            (owner, account),
        ).fetchall()
        count = 0
        for row in rows:
            payload = json.loads(row["payload"])
            if instrument is not None and payload["instrument"] != instrument:
                continue
            if rule_id is not None and payload["rule_id"] != rule_id:
                continue
            db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
            db.execute(
                "DELETE FROM tiers WHERE outbox_id=? AND state IN ('pending','failed','claimed')",
                (row["id"],),
            )
            count += 1
        return count

    def import_snapshot(
        self,
        owner: str,
        account: str,
        rows: list,
        as_of,
        expected_revision: int,
        source: str = "screenshot",
        snapshot_id: str | None = None,
        *,
        history_scope: str | None = None,
        history_request_id: str | None = None,
    ) -> dict:
        """Merge facts; optional chat history commits atomically with the merge.

        Request identity excludes the CAS precondition: a completed request can
        be read back after later revisions without executing its effects again.
        Legacy callers remain untracked and cannot be undone from another chat.
        """
        observed = parse_time(as_of).isoformat()
        if source not in {"screenshot", "manual"}:
            raise ValueError("invalid_snapshot_source")
        if not isinstance(rows, list):
            raise TypeError("rows_must_be_list")
        if len(rows) > 1000:
            raise ValueError("snapshot_too_large")
        tracked = history_scope is not None or history_request_id is not None
        digest = None
        if tracked:
            _identity(history_scope, history_request_id)
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError("expected_revision_required")
            digest = history.fingerprint(
                "import", [rows, observed, source, snapshot_id]
            )
        with self.connection(write=True) as db:
            self._account(db, owner, account)
            if tracked:
                previous = history.receipt(
                    db,
                    owner,
                    account,
                    history_scope,
                    history_request_id,
                    "import",
                    digest,
                )
                if previous is not None:
                    return previous
            if snapshot_id:
                previous = db.execute(
                    "SELECT result FROM snapshots WHERE owner=? AND account=? "
                    "AND snapshot_id=?",
                    (owner, account, snapshot_id),
                ).fetchone()
                if previous:
                    result = dict(json.loads(previous[0]), duplicate=True)
                    if tracked:
                        result.update(
                            action="import",
                            history_id=None,
                            changes=[],
                            cancelled_thresholds=0,
                        )
                        history.save_receipt(
                            db,
                            owner,
                            account,
                            history_scope,
                            history_request_id,
                            "import",
                            digest,
                            result,
                        )
                    return result
            revision = self._account(db, owner, account, expected_revision)
            before_revision = revision
            result = {
                "revision": revision,
                "applied": [],
                "rejected": [],
                "pending_issues": [],
            }
            cancelled = 0
            watermark = db.execute(
                "SELECT as_of FROM snapshot_watermarks WHERE owner=? AND account=?",
                (owner, account),
            ).fetchone()
            obsolete = watermark is not None and observed < watermark[0]
            barrier = db.execute(
                "SELECT as_of FROM position_history_barriers "
                "WHERE owner=? AND account=?",
                (owner, account),
            ).fetchone()
            reverted = (
                source == "screenshot"
                and barrier is not None
                and observed <= barrier[0]
            )
            obsolete = obsolete or reverted
            counts = {}
            for row in rows:
                try:
                    code = canonical_instrument(row.get("instrument", row.get("code")))
                    counts[code] = counts.get(code, 0) + 1
                except (ValueError, AttributeError):
                    pass
            before_state = (
                history.capture(db, owner, account, counts) if tracked else {}
            )
            for candidate in rows:
                row = dict(candidate) if isinstance(candidate, dict) else {}
                instrument, quantity = (
                    row.get("instrument", row.get("code")),
                    row.get("quantity"),
                )
                reason = None
                canonical_valid = True
                try:
                    instrument = canonical_instrument(instrument)
                except ValueError:
                    canonical_valid = False
                    reason = "instrument_required"
                old = db.execute(
                    "SELECT * FROM positions WHERE owner=? AND account=? AND instrument=?",
                    (owner, account, instrument if canonical_valid else ""),
                ).fetchone()
                if reason is None and counts.get(instrument, 0) > 1:
                    reason = "duplicate_instrument_in_snapshot"
                elif reason is None and row.get("ready") is False:
                    reason = row.get("reason", "ambiguous_row")
                elif reason is None and (not _finite(quantity) or quantity < 0):
                    reason = "invalid_quantity"
                if obsolete:
                    reason = (
                        "older_or_reverted_account_snapshot"
                        if reverted
                        else "older_account_snapshot"
                    )
                if reason:
                    if (
                        canonical_valid
                        and not obsolete
                        and (
                            row.get("ready") is False
                            or reason
                            in {"invalid_quantity", "duplicate_instrument_in_snapshot"}
                        )
                        and not (
                            old
                            and observed == old["as_of"]
                            and old["source"] == "manual"
                            and source != "manual"
                        )
                    ):
                        db.execute(
                            "INSERT OR REPLACE INTO pending_issues VALUES(?,?,?,?,?)",
                            (owner, account, instrument, observed, str(reason)),
                        )
                        if instrument not in result["pending_issues"]:
                            result["pending_issues"].append(instrument)
                        cancelled += self.cancel_pending_thresholds(
                            db, owner, account, instrument
                        )
                    result["rejected"].append(
                        {"instrument": instrument, "reason": reason}
                    )
                    continue
                if old and (
                    observed < old["as_of"]
                    or (observed == old["as_of"] and source != "manual")
                ):
                    result["rejected"].append(
                        {"instrument": instrument, "reason": "older_or_same_snapshot"}
                    )
                    continue
                row.pop("code", None)
                row.update(instrument=instrument, as_of=observed, source=source)
                db.execute(
                    "INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?)",
                    (owner, account, instrument, observed, source, _json(row)),
                )
                result["applied"].append(instrument)
                cancelled += self.cancel_pending_thresholds(
                    db, owner, account, instrument
                )
                db.execute(
                    "DELETE FROM pending_issues WHERE owner=? AND account=? AND instrument=? AND as_of<=?",
                    (owner, account, instrument, observed),
                )
            if result["applied"] or result["pending_issues"]:
                revision += 1
                db.execute(
                    "UPDATE accounts SET revision=? WHERE owner=? AND account=?",
                    (revision, owner, account),
                )
                result["revision"] = revision
                db.execute(
                    "INSERT OR REPLACE INTO snapshot_watermarks VALUES(?,?,?)",
                    (owner, account, observed),
                )
                history.clear_redo(db, owner, account)
            if snapshot_id:
                db.execute(
                    "INSERT INTO snapshots VALUES(?,?,?,?)",
                    (owner, account, snapshot_id, _json(result)),
                )
            if tracked:
                after_state = history.capture(db, owner, account, counts)
                before_state, after_state = history.delta(before_state, after_state)
                actual_changes = history.changes(before_state, after_state)
                history_id = None
                if result["applied"] and actual_changes:
                    history_id = history.record_import(
                        db,
                        owner,
                        account,
                        history_scope,
                        before_revision,
                        revision,
                        before_state,
                        after_state,
                        source,
                        watermark[0] if watermark else None,
                        observed,
                    )
                result.update(
                    action="import",
                    history_id=history_id,
                    changes=actual_changes,
                    cancelled_thresholds=cancelled,
                    duplicate=False,
                )
                history.save_receipt(
                    db,
                    owner,
                    account,
                    history_scope,
                    history_request_id,
                    "import",
                    digest,
                    result,
                )
            return result

    def _position_history_action(
        self, owner, account, *, chat_scope, expected_revision, request_id, action
    ):
        _identity(owner, account, chat_scope, request_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision_required")
        digest = history.fingerprint(action, {})
        with self.connection(write=True) as db:
            self._account(db, owner, account)
            previous = history.receipt(
                db, owner, account, chat_scope, request_id, action, digest
            )
            if previous is not None:
                return previous
            revision = self._account(db, owner, account, expected_revision)
            result = history.apply(
                db,
                owner,
                account,
                chat_scope,
                revision,
                action,
                self.cancel_pending_thresholds,
            )
            history.save_receipt(
                db, owner, account, chat_scope, request_id, action, digest, result
            )
            return result

    def undo_positions(
        self,
        owner: str,
        account: str,
        *,
        chat_scope: str,
        expected_revision: int,
        request_id: str,
    ) -> dict:
        """Undo the latest still-current position change from this exact chat."""
        return self._position_history_action(
            owner,
            account,
            chat_scope=chat_scope,
            expected_revision=expected_revision,
            request_id=request_id,
            action="undo",
        )

    def redo_positions(
        self,
        owner: str,
        account: str,
        *,
        chat_scope: str,
        expected_revision: int,
        request_id: str,
    ) -> dict:
        """Reapply an undone delta while its account branch remains current."""
        return self._position_history_action(
            owner,
            account,
            chat_scope=chat_scope,
            expected_revision=expected_revision,
            request_id=request_id,
            action="redo",
        )

    def position_history(self, owner: str, account: str, *, chat_scope: str) -> dict:
        """Small authenticated-chat summary; no raw screenshot metadata."""
        _identity(owner, account, chat_scope)
        with self.connection() as db:
            db.execute("BEGIN")
            revision = self._account(db, owner, account)
            return history.summary(db, owner, account, chat_scope, revision)

    @staticmethod
    def validate_rule(definition):
        if not isinstance(definition, dict):
            raise TypeError("invalid_rule")
        _identity(definition.get("route"), definition.get("metric", "expected_return"))
        tiers = definition.get("tiers")
        if not isinstance(tiers, list) or not 0 < len(tiers) <= 20:
            raise ValueError("invalid_rule_tiers")
        names = set()
        for tier in tiers:
            _identity(tier.get("id"))
            if tier["id"] in names:
                raise ValueError("duplicate_tier_id")
            names.add(tier["id"])
            if tier.get("operator") not in {"lt", "lte", "gt", "gte", "equals"}:
                raise ValueError("invalid_operator")
            if not _finite(tier.get("value")):
                raise ValueError("nonfinite_threshold")
            if definition.get(
                "metric", "expected_return"
            ) == "expected_return" and not (-1 < tier["value"] <= 1):
                raise ValueError("return_threshold_must_be_ratio")
        age = definition.get("max_quote_age_seconds", 900)
        if not _finite(age) or not 0 < age <= 86400:
            raise ValueError("invalid_quote_age")
        for binding in definition.get("bindings", {}).values():
            for name in ("model", "version", "scenario", "currency"):
                _identity(binding.get(name))
        _json(definition)

    def set_rule(self, owner, account, rule_id, definition, expected_revision=None):
        _identity(rule_id)
        definition = json.loads(_json(definition))
        bindings = {}
        for code, binding in definition.get("bindings", {}).items():
            canonical = canonical_instrument(code)
            if canonical in bindings:
                raise ValueError("duplicate_binding_alias")
            bindings[canonical] = binding
        definition["bindings"] = bindings
        for field in ("instruments", "exclude"):
            if field in definition:
                definition[field] = sorted(
                    {canonical_instrument(code) for code in definition[field]}
                )
        self.validate_rule(definition)
        with self.connection(write=True) as db:
            revision = self._account(db, owner, account, expected_revision)
            self._route(db, owner, definition["route"])
            previous = db.execute(
                "SELECT definition FROM rules WHERE owner=? AND account=? AND rule_id=?",
                (owner, account, rule_id),
            ).fetchone()
            if previous and previous[0] != _json(definition):
                self.cancel_pending_thresholds(db, owner, account, rule_id=rule_id)
            db.execute(
                "INSERT OR REPLACE INTO rules VALUES(?,?,?,?)",
                (owner, account, rule_id, _json(definition)),
            )
            db.execute(
                "UPDATE accounts SET revision=revision+1 WHERE owner=? AND account=?",
                (owner, account),
            )
            return {
                "rule_id": rule_id,
                "revision": revision + 1,
                "definition": definition,
            }

    def set_exception(self, owner, account, instrument, paused=True):
        instrument = canonical_instrument(instrument)
        with self.connection(write=True) as db:
            self._account(db, owner, account)
            previous = db.execute(
                "SELECT 1 FROM exceptions WHERE owner=? AND account=? AND instrument=?",
                (owner, account, instrument),
            ).fetchone()
            if bool(previous) != bool(paused):
                self.cancel_pending_thresholds(db, owner, account, instrument)
            if paused:
                db.execute(
                    "INSERT OR IGNORE INTO exceptions VALUES(?,?,?)",
                    (owner, account, instrument),
                )
            else:
                db.execute(
                    "DELETE FROM exceptions WHERE owner=? AND account=? AND instrument=?",
                    (owner, account, instrument),
                )
            return {"instrument": instrument, "paused": bool(paused)}

    def reset_tiers(self, owner, account, rule_id, instrument=None, tier_id=None):
        """Explicit rearm only; never called by ordinary rule/snapshot updates."""
        with self.connection(write=True) as db:
            self._account(db, owner, account)
            sql = "owner=? AND account=? AND rule_id=?"
            params = [owner, account, rule_id]
            if instrument is not None:
                instrument = canonical_instrument(instrument)
                sql += " AND instrument=?"
                params.append(instrument)
            if tier_id is not None:
                sql += " AND tier_id=?"
                params.append(tier_id)
            rows = db.execute(
                "SELECT outbox_id,state FROM tiers WHERE " + sql, params
            ).fetchall()
            reset_count = len(rows)
            # A submitted send cannot be recalled. Preserve its consumed state
            # until the uncertain send is resolved rather than risk a duplicate.
            for row in rows:
                entry = db.execute(
                    "SELECT state FROM outbox WHERE id=?", (row[0],)
                ).fetchone()
                if row["state"] == "unknown" or (
                    entry and entry[0] in {"submitted", "unknown"}
                ):
                    raise RevisionConflict("delivery_uncertain_resolve_before_reset")
            for row in rows:
                db.execute(
                    "UPDATE outbox SET state='cancelled' WHERE id=? AND state IN "
                    "('queued','failed','claimed')",
                    (row[0],),
                )
                db.execute(
                    "DELETE FROM tiers WHERE outbox_id=? AND state IN ('pending','failed','claimed')",
                    (row[0],),
                )
            db.execute("DELETE FROM tiers WHERE " + sql, params)
            return {"reset": reset_count}

    @staticmethod
    def _reminder(db, owner, reminder_id, expected_revision=None):
        _identity(owner, reminder_id)
        row = db.execute(
            "SELECT * FROM reminders WHERE owner=? AND reminder_id=?",
            (owner, reminder_id),
        ).fetchone()
        if row is None:
            raise KeyError("reminder_not_found")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise RevisionConflict("reminder_revision_changed")
        return dict(row)

    def create_reminder(
        self,
        owner,
        reminder_id,
        due_at,
        text,
        route,
        request_id=None,
        now=None,
        reply_delivery=False,
    ):
        _identity(owner, reminder_id, text, route)
        due, current = parse_time(due_at), parse_time(now)
        with self.connection(write=True) as db:
            self._route(db, owner, route)
            if request_id:
                existing = db.execute(
                    "SELECT * FROM reminders WHERE owner=? AND request_id=?",
                    (owner, request_id),
                ).fetchone()
                if existing:
                    return dict(existing, duplicate=True)
            existing = db.execute(
                "SELECT * FROM reminders WHERE owner=? AND reminder_id=?",
                (owner, reminder_id),
            ).fetchone()
            if existing:
                return dict(existing, duplicate=True)
            state = "completed" if due <= current else "pending"
            if reply_delivery and due <= current:
                state = "receipt_pending"
            db.execute(
                "INSERT INTO reminders VALUES(?,?,?,?,?,1,?,?)",
                (owner, reminder_id, due.isoformat(), text, route, state, request_id),
            )
            return dict(
                self._reminder(db, owner, reminder_id),
                immediate=due <= current,
                notice=(
                    "该提醒时间已过，本次回复计为一次提醒。" if due <= current else ""
                ),
            )

    @staticmethod
    def _invalidate_reminder_outbox(db, owner, reminder_id):
        possibly_sent = False
        rows = db.execute(
            "SELECT id,payload,state FROM outbox WHERE owner=? AND kind='time'",
            (owner,),
        ).fetchall()
        for row in rows:
            if json.loads(row["payload"]).get("reminder_id") != reminder_id:
                continue
            staged = False
            if (
                row["state"] == "unknown"
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_immediate_receipts'"
                ).fetchone()
            ):
                payload = json.loads(row["payload"])
                staged = bool(
                    db.execute(
                        "SELECT 1 FROM personal_immediate_receipts WHERE owner=? AND reminder_id=? AND revision=? AND outcome='pending'",
                        (owner, reminder_id, payload.get("revision")),
                    ).fetchone()
                )
            if row["state"] in {"submitted", "unknown", "sent"} and not staged:
                possibly_sent = True
            else:
                db.execute(
                    "UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],)
                )
        return possibly_sent

    def edit_reminder(
        self,
        owner,
        reminder_id,
        expected_revision,
        due_at=None,
        text=None,
        route=None,
        now=None,
        reply_delivery=False,
    ):
        current = parse_time(now)
        with self.connection(write=True) as db:
            old = self._reminder(db, owner, reminder_id, expected_revision)
            due = parse_time(due_at or old["due_at"])
            text, route = (
                text if text is not None else old["text"],
                route or old["route"],
            )
            _identity(text, route)
            self._route(db, owner, route)
            possibly_sent = self._invalidate_reminder_outbox(db, owner, reminder_id)
            state = "completed" if due <= current else "pending"
            if reply_delivery and due <= current:
                state = "receipt_pending"
            db.execute(
                "UPDATE reminders SET due_at=?,text=?,route=?,revision=revision+1,state=? "
                "WHERE owner=? AND reminder_id=?",
                (due.isoformat(), text, route, state, owner, reminder_id),
            )
            return dict(
                self._reminder(db, owner, reminder_id),
                possibly_sent=possibly_sent,
                immediate=due <= current,
            )

    def cancel_reminder(self, owner, reminder_id, expected_revision):
        with self.connection(write=True) as db:
            self._reminder(db, owner, reminder_id, expected_revision)
            possibly_sent = self._invalidate_reminder_outbox(db, owner, reminder_id)
            db.execute(
                "UPDATE reminders SET state='cancelled',revision=revision+1 "
                "WHERE owner=? AND reminder_id=?",
                (owner, reminder_id),
            )
            return dict(
                self._reminder(db, owner, reminder_id), possibly_sent=possibly_sent
            )

    def status(self, owner, account=None):
        _identity(owner)
        with self.connection() as db:
            params = [owner]
            predicate = "owner=?"
            if account is not None:
                self._account(db, owner, account)
                predicate += " AND account=?"
                params.append(account)
            accounts = []
            for row in db.execute("SELECT * FROM accounts WHERE " + predicate, params):
                item = dict(row)
                ids = (owner, row["account"])
                item["positions"] = [
                    json.loads(p[0])
                    for p in db.execute(
                        "SELECT data FROM positions WHERE owner=? AND account=?", ids
                    )
                ]
                item["rules"] = [
                    {"rule_id": r[0], "definition": json.loads(r[1])}
                    for r in db.execute(
                        "SELECT rule_id,definition FROM rules WHERE owner=? AND account=?",
                        ids,
                    )
                ]
                item["exceptions"] = [
                    r[0]
                    for r in db.execute(
                        "SELECT instrument FROM exceptions WHERE owner=? AND account=?",
                        ids,
                    )
                ]
                item["tiers"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM tiers WHERE owner=? AND account=?", ids
                    )
                ]
                item["evaluations"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM evaluations WHERE owner=? AND account=?", ids
                    )
                ]
                watermark = db.execute(
                    "SELECT as_of FROM snapshot_watermarks WHERE owner=? AND account=?",
                    ids,
                ).fetchone()
                item["as_of"] = watermark[0] if watermark else None
                item["pending_issues"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM pending_issues WHERE owner=? AND account=?", ids
                    )
                ]
                accounts.append(item)
            return {
                "accounts": accounts,
                "reminders": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM reminders WHERE owner=?", (owner,)
                    )
                ],
                "outbox": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM outbox WHERE " + predicate, params
                    )
                ],
            }

    @staticmethod
    def enqueue(db, *, owner, account, kind, route, text, payload, business_key, now):
        entry_id = uuid.uuid4().hex
        result = db.execute(
            "INSERT OR IGNORE INTO outbox "
            "(id,business_key,owner,account,kind,route,text,payload,state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'queued',?)",
            (
                entry_id,
                business_key,
                owner,
                account,
                kind,
                route,
                text,
                _json(payload),
                parse_time(now).isoformat(),
            ),
        )
        return entry_id if result.rowcount else None
