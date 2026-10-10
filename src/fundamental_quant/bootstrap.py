"""A durable first acquisition pass, separate from routine daily refreshes."""

from __future__ import annotations

import json


class BootstrapAcquisition:
    def __init__(self, settings, store, routine_limit):
        self.store = store
        self.routine_limit = routine_limit
        config = settings.get("bootstrap", {})
        if not isinstance(config, dict):
            raise TypeError("invalid fundamental_quant.bootstrap")
        self.enabled = config.get("enabled", False)
        if type(self.enabled) is not bool:
            raise ValueError("invalid fundamental_quant.bootstrap.enabled")
        self.limit = config.get("daily_acquisition_limit", 500)
        if type(self.limit) is not int or (self.enabled and self.limit < routine_limit):
            raise ValueError("bootstrap acquisition limit must cover routine limit")
        self.batch_id = config.get("id", "initial")
        if not isinstance(self.batch_id, str) or not self.batch_id.strip():
            raise ValueError("invalid fundamental_quant.bootstrap.id")
        self.pending = set()
        self.baseline_members = set()
        self.baseline_pending = set()
        self.baseline_size = config.get("baseline_size", 500)
        if type(self.baseline_size) is not int or self.baseline_size < 1:
            raise ValueError("invalid bootstrap.baseline_size")
        self.state = {"phase": "routine", "daily_acquisition_limit": routine_limit}
        self.refresh()

    def refresh(self, *, initialize=False, complete=False):
        if not self.enabled:
            return self.state
        # No bounded events() tail: a large pool must not lose older receipts.
        with self.store.transaction() as db:
            events = db.execute(
                "SELECT kind,payload FROM events WHERE kind IN "
                "('bootstrap_started','bootstrap_completed','bootstrap_baseline') ORDER BY id"
            ).fetchall()
            started, ended, baseline = None, False, None
            for event in events:
                payload = json.loads(event["payload"])
                if payload.get("bootstrap_id") == self.batch_id:
                    if event["kind"] == "bootstrap_started":
                        started = payload
                    elif event["kind"] == "bootstrap_completed":
                        ended = True
                    else:
                        baseline = payload
            if started is None and initialize:
                members = [
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM companies WHERE stage<>'excluded' "
                        "AND COALESCE(json_extract(company,'$.peer_only'),0)=0 "
                        "ORDER BY id"
                    )
                ]
                if members:
                    started = {"bootstrap_id": self.batch_id, "members": members}
                    db.execute(
                        "INSERT INTO events(company_id,kind,payload,created_at) "
                        "VALUES('','bootstrap_started',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        (json.dumps(started),),
                    )
            visited = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT company_id FROM events WHERE kind IN "
                    "('collection_finished','universe_excluded') "
                    "UNION SELECT id FROM companies WHERE stage='excluded'"
                )
            }
            members = set(started["members"]) if started else set()
            self.pending = members - visited
            if baseline is None and initialize and members:
                reservations = db.execute(
                    "SELECT r.day,r.key FROM reservations r JOIN companies c ON c.id=r.key "
                    "WHERE r.kind='acquisition' AND COALESCE(json_extract(c.company,'$.peer_only'),0)=0 "
                    "ORDER BY r.day,r.key"
                ).fetchall()
                by_day = {}
                for row in reservations:
                    by_day.setdefault(row[0], []).append(row[1])
                # Freeze exactly the first reserved cohort, never a shifting top N.
                target = min(self.baseline_size, len(members))
                ordered = next(
                    (ids for ids in by_day.values() if len(ids) >= target),
                    list(dict.fromkeys(row[1] for row in reservations)),
                )
                if len(ordered) >= target:
                    baseline = {
                        "bootstrap_id": self.batch_id,
                        "members": ordered[:target],
                    }
                    db.execute(
                        "INSERT INTO events(company_id,kind,payload,created_at) "
                        "VALUES('','bootstrap_baseline',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        (json.dumps(baseline),),
                    )
            self.baseline_members = set(baseline["members"]) if baseline else set()
            self.baseline_pending = self.baseline_members - visited
            if started and not self.pending and not ended and complete:
                db.execute(
                    "INSERT INTO events(company_id,kind,payload,created_at) "
                    "VALUES('','bootstrap_completed',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (json.dumps({"bootstrap_id": self.batch_id}),),
                )
                ended = True
        self.state = {
            "phase": "routine" if ended else "bootstrap",
            "bootstrap_id": self.batch_id,
            "daily_acquisition_limit": self.routine_limit if ended else self.limit,
            "routine_daily_acquisition_limit": self.routine_limit,
            "cohort_size": len(members),
            "first_pass_finished_or_excluded": len(members) - len(self.pending),
            "first_pass_pending": len(self.pending),
            "cohort_frozen": started is not None,
            "first_pass_is_not_data_readiness": True,
            "baseline": {
                "size": len(self.baseline_members),
                "target_size": self.baseline_size,
                "frozen": baseline is not None,
                "first_pass_finished": len(self.baseline_members)
                - len(self.baseline_pending),
                "first_pass_pending": len(self.baseline_pending),
                "collection_is_not_readiness": True,
            },
        }
        return self.state

    def priority(self, issuer):
        if self.baseline_pending:
            if issuer in self.baseline_pending:
                return 0
            return 1 if issuer in self.pending else 2
        return int(
            self.state["phase"] == "bootstrap"
            and self.state.get("cohort_frozen", False)
            and issuer not in self.pending
        )
