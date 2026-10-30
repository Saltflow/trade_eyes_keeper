"""A bounded, durable acceptance experiment, independent of daily quotas.

Reservations are conservative and never refunded after interruption. The same
transaction charges both the lifetime experiment and normal daily audit ledger.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .contracts import canonical


class AcceptanceCampaign:
    def __init__(self, settings, store, *, clock=None):
        self.config = settings.get("acceptance", {}) or {}
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.enabled = self.config.get("enabled") is True
        self.id = self.config.get("id", "")
        if self.enabled:
            if not isinstance(self.id, str) or not self.id.strip():
                raise ValueError("acceptance.id is required")
            for key in (
                "target_limit",
                "max_model_calls",
                "max_input_tokens",
                "max_output_tokens",
                "max_extraction_batches_per_company",
            ):
                if type(self.config.get(key)) is not int or self.config[key] <= 0:
                    raise ValueError(f"acceptance.{key} must be a positive integer")
            self._dates(self.config)
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS acceptance_campaigns "
                "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    @staticmethod
    def _dates(config):
        start = datetime.fromisoformat(config["started_at"])
        end = datetime.fromisoformat(config["expires_at"])
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("acceptance dates must be aware and increasing")
        return start, end

    def _read(self, db):
        row = db.execute(
            "SELECT payload FROM acceptance_campaigns WHERE id=?", (self.id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, db, state):
        db.execute(
            "INSERT INTO acceptance_campaigns VALUES(?,?) ON CONFLICT(id) "
            "DO UPDATE SET payload=excluded.payload",
            (self.id, canonical(state)),
        )

    def _expire(self, state):
        if state and state["status"] == "active":
            _, end = self._dates(state["config"])
            if self.clock() >= end:
                state.update(status="expired", reason="acceptance_time_limit")
        return state

    def state(self):
        if not self.enabled:
            return {"enabled": False}
        with self.store.transaction() as db:
            state = self._read(db)
            if state:
                self._expire(state)
                self._write(db, state)
                return {
                    **state,
                    "limits": self.limits(state),
                    "started_at": state["config"]["started_at"],
                    "expires_at": state["config"]["expires_at"],
                    "hold_model_spending": state["status"] != "active",
                }
        return {"enabled": True, "id": self.id, "status": "not_started"}

    def start(self, targets):
        with self.store.transaction() as db:
            existing = self._read(db)
            if existing:
                return self._expire(existing)
            start, _ = self._dates(self.config)
            if self.clock() < start:
                raise ValueError("acceptance start is in the future")
            targets = list(dict.fromkeys(targets))[: self.config["target_limit"]]
            state = {
                "enabled": True,
                "id": self.id,
                "config": dict(self.config),
                "status": "active" if targets else "no_progress",
                "reason": "" if targets else "no_eligible_saved_evidence",
                "targets": targets,
                "outcomes": {},
                "usage": {},
                "extraction_batches": {},
                "reservations": [],
                "scope": "bounded_pilot_not_full_universe_acceptance",
            }
            self._expire(state)
            self._write(db, state)
            return state

    @staticmethod
    def limits(state):
        config = state["config"]
        return {
            "model_calls": config["max_model_calls"],
            "input_token_reservations": config["max_input_tokens"],
            "output_token_reservations": config["max_output_tokens"],
        }

    def resume_after_repair(self, *, repair_version, expires_at):
        """Explicit operator resume; retain lifetime costs and previous outcomes.

        Never called by the scheduler: a new repair identifier and renewed
        bounded window are required after validation and user authorization.
        """
        if (
            not self.enabled
            or not isinstance(repair_version, str)
            or not repair_version.strip()
        ):
            raise ValueError("enabled campaign and repair_version are required")
        now = self.clock()
        end = datetime.fromisoformat(expires_at)
        if end.tzinfo is None or not now < end <= now + timedelta(hours=2):
            raise ValueError(
                "repair resume window must be future and at most two hours"
            )
        with self.store.transaction() as db:
            state = self._expire(self._read(db))
            if not state or state["status"] not in {
                "no_progress",
                "expired",
                "blocked",
            }:
                raise ValueError("only a stopped unresolved campaign can resume")
            history = state.setdefault("repair_resumes", [])
            if any(item["repair_version"] == repair_version for item in history):
                raise ValueError("this repair has already been resumed")
            if any(
                state["usage"].get(k, 0) >= limit
                for k, limit in self.limits(state).items()
            ):
                raise ValueError("acceptance lifetime budget exhausted")
            history.append(
                {
                    "repair_version": repair_version,
                    "resumed_at": now.isoformat(),
                    "previous_status": state["status"],
                    "previous_reason": state.get("reason"),
                    "previous_expires_at": state["config"]["expires_at"],
                    "previous_finished_at": state.get("finished_at"),
                    "previous_outcomes": state.get("outcomes", {}),
                    "usage_at_resume": dict(state["usage"]),
                }
            )
            state["outcomes"] = {
                issuer: outcome
                for issuer, outcome in state.get("outcomes", {}).items()
                if outcome.get("status") in {"published", "screened"}
            }
            state.update(status="active", reason="validated_repair_resume")
            state.pop("finished_at", None)
            state["config"]["expires_at"] = end.isoformat()
            self._write(db, state)
            return state

    def reserve_model(self, day, amounts):
        with self.store.transaction() as db:
            state = self._expire(self._read(db))
            if not state or state["status"] != "active":
                if state:
                    self._write(db, state)
                return False
            limits = self.limits(state)
            if set(amounts) != set(limits) or any(
                type(v) is not int or v <= 0 for v in amounts.values()
            ):
                raise ValueError("invalid acceptance model reservation")
            if any(
                state["usage"].get(k, 0) + v > limits[k] for k, v in amounts.items()
            ):
                state.update(
                    status="budget_exhausted", reason="acceptance_model_budget"
                )
                self._write(db, state)
                return False
            for kind, amount in amounts.items():
                state["usage"][kind] = state["usage"].get(kind, 0) + amount
                db.execute(
                    "INSERT INTO budgets VALUES(?,?,?) ON CONFLICT(day,kind) "
                    "DO UPDATE SET used=used+excluded.used",
                    (day, kind, amount),
                )
            self._write(db, state)
            return True

    def add_usage(self, usage):
        with self.store.transaction() as db:
            state = self._read(db)
            if not state:
                return
            for kind in ("input_tokens", "output_tokens"):
                amount = usage.get(kind)
                if type(amount) is int and amount >= 0:
                    state["usage"][kind] = state["usage"].get(kind, 0) + amount
            self._write(db, state)

    def reserve_batch(self, issuer):
        with self.store.transaction() as db:
            state = self._expire(self._read(db))
            if not state or state["status"] != "active":
                return False
            count = state["extraction_batches"].get(issuer, 0)
            if count >= state["config"]["max_extraction_batches_per_company"]:
                return False
            state["extraction_batches"][issuer] = count + 1
            self._write(db, state)
            return True

    def reallocate_batch_capacity(self, *, repair_version, per_company_limit):
        """Operator repair allocation within unchanged campaign spending caps."""
        if not isinstance(repair_version, str) or not repair_version.strip():
            raise ValueError("repair_version required")
        if type(per_company_limit) is not int or not 1 <= per_company_limit <= 24:
            raise ValueError("per-company repair cap must be at most 24")
        with self.store.transaction() as db:
            state = self._expire(self._read(db))
            if not state or state["status"] != "active":
                raise ValueError("active campaign required")
            old = state["config"]["max_extraction_batches_per_company"]
            if per_company_limit <= old:
                raise ValueError("new allocation must exceed previous allocation")
            history = state.setdefault("batch_reallocations", [])
            if any(item["repair_version"] == repair_version for item in history):
                raise ValueError("repair allocation already recorded")
            history.append(
                {
                    "repair_version": repair_version,
                    "previous_limit": old,
                    "new_limit": per_company_limit,
                    "at": self.clock().isoformat(),
                    "usage": dict(state["usage"]),
                    "batch_counts": dict(state["extraction_batches"]),
                }
            )
            state["config"]["max_extraction_batches_per_company"] = per_company_limit
            self._write(db, state)
            return state

    def reserve_operation(self, day, kind, key, limit):
        """Deduplicate cover/research operations across days, with audit counts."""
        with self.store.transaction() as db:
            state = self._expire(self._read(db))
            if not state or state["status"] != "active":
                return False
            identity = [kind, key]
            if identity in state["reservations"]:
                # Research is an entitlement for this target, not a retry count.
                # Model reservations and unchanged-input checks still bound work.
                return kind == "research"
            if sum(x[0] == kind for x in state["reservations"]) >= limit:
                return False
            state["reservations"].append(identity)
            db.execute(
                "INSERT INTO budgets VALUES(?,?,1) ON CONFLICT(day,kind) "
                "DO UPDATE SET used=used+1",
                (day, kind),
            )
            self._write(db, state)
            return True

    def checkpoint(self, issuer, outcome):
        with self.store.transaction() as db:
            state = self._read(db)
            state["outcomes"][issuer] = outcome
            self._write(db, state)

    def finish(self, status, reason):
        with self.store.transaction() as db:
            state = self._expire(self._read(db))
            if state and state["status"] == "active":
                state.update(
                    status=status, reason=reason, finished_at=self.clock().isoformat()
                )
                self._write(db, state)
