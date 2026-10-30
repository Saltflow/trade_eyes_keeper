"""Deterministic rule evaluation and crash-conscious delivery transitions."""

import json
import operator
import uuid
from datetime import timedelta

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import SHANGHAI, MonitorStore, _finite, canonical_instrument, parse_time

COMPARISONS = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
    "equals": operator.eq,
}


class MonitorEngine:
    def __init__(self, store: MonitorStore):
        self.store = store

    @staticmethod
    def _ready(definition, instrument, observation, now):
        if not isinstance(observation, dict):
            return "observation_unavailable"
        if observation.get("ready") is False:
            return observation.get("reason", "observation_unavailable")
        quote = observation.get("quote", {})
        try:
            observed = parse_time(quote["as_of"])
            price = quote["price"]
        except (ValueError, KeyError, TypeError):
            return "quote_time_or_price_missing"
        if not _finite(price) or price <= 0 or not quote.get("source"):
            return "invalid_quote"
        if observed > now + timedelta(seconds=60):
            return "future_quote"
        if quote.get("valid_until") is not None:
            try:
                if now >= parse_time(quote["valid_until"]):
                    return "quote_session_expired"
            except (ValueError, TypeError):
                return "invalid_quote_valid_until"
        if quote.get("market_open") is True:
            age = definition.get("max_quote_age_seconds", 900)
            if (now - observed).total_seconds() > age:
                return "stale_quote"
        elif quote.get("market_open") is False:
            session = quote.get("latest_completed_session")
            try:
                zone = ZoneInfo(quote.get("market_timezone", "Asia/Shanghai"))
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                return "invalid_market_timezone"
            if not session or observed.astimezone(zone).date().isoformat() != session:
                return "unverified_completed_session"
        else:
            return "market_session_unknown"
        metric = definition.get("metric", "expected_return")
        value = observation.get("metrics", {}).get(metric, observation.get(metric))
        if not _finite(value):
            return observation.get("reason", "nonfinite_metric")
        if metric == "expected_return":
            if not -1 < value <= 1:
                return "expected_return_not_ratio"
            binding = definition.get("bindings", {}).get(instrument)
            research = observation.get("research", {})
            if not binding:
                return "research_binding_missing"
            if any(
                binding.get(k) != research.get(k)
                for k in ("model", "version", "scenario")
            ):
                return "research_binding_mismatch"
            if binding.get("currency") != quote.get("currency"):
                return "research_currency_mismatch"
        return None

    def evaluate(self, owner, account, observations, now=None):
        current = parse_time(now)
        result = {"enqueued": [], "not_ready": [], "checked": 0}
        with self.store.connection(write=True) as db:
            self.store._account(db, owner, account)
            ids = (owner, account)
            db.execute("DELETE FROM evaluations WHERE owner=? AND account=?", ids)
            positions = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM positions WHERE owner=? AND account=?", ids
                )
            ]
            paused = {
                r[0]
                for r in db.execute(
                    "SELECT instrument FROM exceptions WHERE owner=? AND account=?", ids
                )
            }
            pending_issues = {
                r["instrument"]: r["reason"]
                for r in db.execute(
                    "SELECT instrument,reason FROM pending_issues WHERE owner=? AND account=?",
                    ids,
                )
            }
            rules = db.execute(
                "SELECT rule_id,definition FROM rules WHERE owner=? AND account=?", ids
            ).fetchall()
            for rule in rules:
                rule_id, definition = rule["rule_id"], json.loads(rule["definition"])
                if not definition.get("enabled", True):
                    continue
                scoped = observations.get("__rules__", {}).get(rule_id, observations)
                normalized = {}
                duplicates = set()
                for code, observation in scoped.items():
                    if code == "__rules__":
                        continue
                    try:
                        canonical = canonical_instrument(code)
                    except ValueError:
                        continue
                    if canonical in normalized:
                        duplicates.add(canonical)
                    normalized[canonical] = observation
                for code in duplicates:
                    normalized.pop(code, None)
                for position in positions:
                    code = position["instrument"]
                    if (
                        position["quantity"] <= 0
                        or code in paused
                        or code in definition.get("exclude", [])
                    ):
                        continue
                    if (
                        definition.get("instruments") is not None
                        and code not in definition["instruments"]
                    ):
                        continue
                    obs = normalized.get(code)
                    reason = pending_issues.get(code) or self._ready(
                        definition, code, obs, current
                    )
                    # A queued/definitively failed notification is a reservation,
                    # not a consumed tier. Rebuild it from the current observation
                    # instead of sending an obsolete crossing after a recovery.
                    pending = db.execute(
                        "SELECT payload FROM outbox WHERE owner=? AND account=? "
                        "AND kind='threshold' AND state IN ('queued','failed','claimed')",
                        ids,
                    ).fetchall()
                    metric = definition.get("metric", "expected_return")
                    for entry in pending:
                        payload = json.loads(entry["payload"])
                        if (
                            payload["instrument"] != code
                            or payload["rule_id"] != rule_id
                        ):
                            continue
                        if reason or any(
                            payload.get(key) != value
                            for key, value in {
                                "metric": metric,
                                "value": obs.get("metrics", {}).get(
                                    metric, obs.get(metric)
                                ),
                                "quote": obs.get("quote", {}),
                                "research": obs.get("research", {}),
                                "binding": definition.get("bindings", {}).get(code, {}),
                            }.items()
                        ):
                            self.store.cancel_pending_thresholds(
                                db, owner, account, code, rule_id
                            )
                            break
                    db.execute(
                        "INSERT INTO evaluations VALUES(?,?,?,?,?,?)",
                        (*ids, code, rule_id, current.isoformat(), reason),
                    )
                    if reason:
                        result["not_ready"].append(
                            {"instrument": code, "rule_id": rule_id, "reason": reason}
                        )
                        continue
                    result["checked"] += 1
                    value = obs.get("metrics", {}).get(metric, obs.get(metric))
                    tiers = []
                    for tier in definition["tiers"]:
                        existing = db.execute(
                            "SELECT 1 FROM tiers WHERE owner=? AND account=? AND instrument=? "
                            "AND rule_id=? AND tier_id=?",
                            (*ids, code, rule_id, tier["id"]),
                        ).fetchone()
                        if existing is None and COMPARISONS[tier["operator"]](
                            value, tier["value"]
                        ):
                            tiers.append(tier)
                    if not tiers:
                        continue
                    payload = {
                        "instrument": code,
                        "rule_id": rule_id,
                        "metric": metric,
                        "value": value,
                        "tiers": tiers,
                        "quote": obs["quote"],
                        "research": obs.get("research", {}),
                        "binding": definition.get("bindings", {}).get(code, {}),
                    }
                    formatted = (
                        f"{value:.6%}" if metric == "expected_return" else str(value)
                    )
                    boundaries = "; ".join(
                        f"{t['id']}: {t['operator']} "
                        + (
                            f"{t['value']:.6%}"
                            if metric == "expected_return"
                            else str(t["value"])
                        )
                        for t in tiers
                    )
                    text = (
                        f"账户 {account}，{position.get('name', code)} ({code})："
                        f"{definition.get('text', '配置阈值已达到')}。"
                        f"{metric}={formatted}；{boundaries}。"
                        f"行情时点 {obs['quote']['as_of']}。"
                        + (
                            f"依据研究 {obs['research'].get('version')}，场景 {obs['research'].get('scenario')}。"
                            if obs.get("research")
                            else ""
                        )
                    )
                    entry_id = self.store.enqueue(
                        db,
                        owner=owner,
                        account=account,
                        kind="threshold",
                        route=definition["route"],
                        text=text,
                        payload=payload,
                        business_key="threshold:" + uuid.uuid4().hex,
                        now=current,
                    )
                    for tier in tiers:
                        db.execute(
                            "INSERT INTO tiers VALUES(?,?,?,?,?,'pending',?)",
                            (*ids, code, rule_id, tier["id"], entry_id),
                        )
                    result["enqueued"].append(entry_id)
        return result

    def enqueue_due(self, now=None):
        current = parse_time(now)
        result = []
        with self.store.connection(write=True) as db:
            rows = db.execute(
                "SELECT * FROM reminders WHERE state='pending' AND due_at<=?",
                (current.isoformat(),),
            ).fetchall()
            for reminder in rows:
                payload = {
                    "reminder_id": reminder["reminder_id"],
                    "revision": reminder["revision"],
                    "due_at": reminder["due_at"],
                }
                text = f"到期提醒：{reminder['text']}。原定时间 {parse_time(reminder['due_at']).astimezone(SHANGHAI).isoformat()}。"
                entry_id = self.store.enqueue(
                    db,
                    owner=reminder["owner"],
                    account=None,
                    kind="time",
                    route=reminder["route"],
                    text=text,
                    payload=payload,
                    now=current,
                    business_key=json.dumps(
                        [
                            "time",
                            reminder["owner"],
                            reminder["reminder_id"],
                            reminder["revision"],
                        ]
                    ),
                )
                db.execute(
                    "UPDATE reminders SET state='queued' WHERE owner=? AND reminder_id=? AND revision=?",
                    (reminder["owner"], reminder["reminder_id"], reminder["revision"]),
                )
                if entry_id:
                    result.append(entry_id)
        return result

    def claim_delivery(self, worker, now=None, lease_seconds=300):
        current = parse_time(now)
        with self.store.connection(write=True) as db:
            expired = (current - timedelta(seconds=lease_seconds)).isoformat()
            uncertain = db.execute(
                "SELECT id FROM outbox WHERE state IN ('claimed','submitted') AND claimed_at<=?",
                (expired,),
            ).fetchall()
            for entry in uncertain:
                db.execute(
                    "UPDATE outbox SET state='unknown',error='worker_lease_expired' WHERE id=?",
                    (entry[0],),
                )
                db.execute(
                    "UPDATE tiers SET state='unknown' WHERE outbox_id=?", (entry[0],)
                )
                entry_row = db.execute(
                    "SELECT * FROM outbox WHERE id=?", (entry[0],)
                ).fetchone()
                if entry_row["kind"] == "time":
                    payload = json.loads(entry_row["payload"])
                    db.execute(
                        "UPDATE reminders SET state='unknown' WHERE owner=? AND reminder_id=? AND revision=? AND state='queued'",
                        (
                            entry_row["owner"],
                            payload["reminder_id"],
                            payload["revision"],
                        ),
                    )
            row = db.execute(
                "SELECT * FROM outbox WHERE state='queued' OR "
                "(state='failed' AND (next_attempt_at IS NULL OR next_attempt_at<=?)) "
                "ORDER BY CASE state WHEN 'queued' THEN 0 ELSE 1 END,created_at,id LIMIT 1",
                (current.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            token = worker + ":" + uuid.uuid4().hex
            db.execute(
                "UPDATE outbox SET state='claimed',token=?,claimed_at=?,attempts=attempts+1 WHERE id=?",
                (token, current.isoformat(), row["id"]),
            )
            return dict(
                dict(row),
                token=token,
                payload=json.loads(row["payload"]),
                state="claimed",
            )

    def release_unsubmitted(self, entry_id, token):
        """Release only our known-unsent claim; never revive a cancelled send."""
        with self.store.connection(write=True) as db:
            return bool(
                db.execute(
                    "UPDATE outbox SET state='queued',token=NULL,claimed_at=NULL,"
                    "error='not_sent:cancelled_before_submission',metadata=? "
                    "WHERE id=? AND token=? AND state='claimed'",
                    (
                        json.dumps({"send_attempted": False}),
                        entry_id,
                        token,
                    ),
                ).rowcount
            )

    def mark_submitted(self, entry_id, token, now=None):
        """Commit the final cancellable boundary immediately before HTTP send."""
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE id=? AND token=? AND state='claimed'",
                (entry_id, token),
            ).fetchone()
            if row is None:
                return False
            if row["kind"] == "time":
                payload = json.loads(row["payload"])
                reminder = db.execute(
                    "SELECT state,revision FROM reminders WHERE owner=? AND reminder_id=?",
                    (row["owner"], payload["reminder_id"]),
                ).fetchone()
                if (
                    not reminder
                    or reminder["state"] != "queued"
                    or reminder["revision"] != payload["revision"]
                ):
                    db.execute(
                        "UPDATE outbox SET state='cancelled' WHERE id=?", (entry_id,)
                    )
                    return False
            else:
                payload = json.loads(row["payload"])
                ids = (row["owner"], row["account"])
                position = db.execute(
                    "SELECT data FROM positions WHERE owner=? AND account=? AND instrument=?",
                    (*ids, payload["instrument"]),
                ).fetchone()
                rule = db.execute(
                    "SELECT definition FROM rules WHERE owner=? AND account=? AND rule_id=?",
                    (*ids, payload["rule_id"]),
                ).fetchone()
                paused = db.execute(
                    "SELECT 1 FROM exceptions WHERE owner=? AND account=? AND instrument=?",
                    (*ids, payload["instrument"]),
                ).fetchone()
                pending_issue = db.execute(
                    "SELECT 1 FROM pending_issues WHERE owner=? AND account=? AND instrument=?",
                    (*ids, payload["instrument"]),
                ).fetchone()
                definition = json.loads(rule[0]) if rule else {}
                invalid = (
                    not rule
                    or not position
                    or json.loads(position[0])["quantity"] <= 0
                    or paused
                    or pending_issue
                    or not definition.get("enabled", True)
                    or payload["instrument"] in definition.get("exclude", [])
                    or (
                        definition.get("instruments") is not None
                        and payload["instrument"] not in definition["instruments"]
                    )
                    or definition.get("metric", "expected_return") != payload["metric"]
                    or any(
                        t not in definition.get("tiers", []) for t in payload["tiers"]
                    )
                    or definition.get("route") != row["route"]
                    or any(
                        definition.get("bindings", {})
                        .get(payload["instrument"], {})
                        .get(k)
                        != payload.get("research", {}).get(k)
                        for k in ("model", "version", "scenario")
                    )
                    or definition.get("bindings", {}).get(payload["instrument"], {})
                    != payload.get("binding", {})
                )
                if not invalid and now is not None:
                    invalid = (
                        self._ready(
                            definition,
                            payload["instrument"],
                            {
                                "metrics": {payload["metric"]: payload["value"]},
                                "quote": payload["quote"],
                                "research": payload.get("research", {}),
                            },
                            parse_time(now),
                        )
                        is not None
                    )
                if invalid:
                    db.execute(
                        "UPDATE outbox SET state='cancelled' WHERE id=?", (entry_id,)
                    )
                    db.execute(
                        "DELETE FROM tiers WHERE outbox_id=? AND state IN ('pending','failed','claimed')",
                        (entry_id,),
                    )
                    return False
            db.execute("UPDATE outbox SET state='submitted' WHERE id=?", (entry_id,))
            return True

    commit_send = mark_submitted

    def finish_delivery(
        self, entry_id, token, outcome, error="", now=None, metadata=None
    ):
        if outcome not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid_delivery_outcome")
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE id=? AND token=? AND state='submitted'",
                (entry_id, token),
            ).fetchone()
            if row is None:
                return False
            if outcome == "sent" and row["state"] != "submitted":
                return False
            delay = min(3600, 60 * 2 ** min(row["attempts"] - 1, 6))
            retry_at = (
                (parse_time(now) + timedelta(seconds=delay)).isoformat()
                if outcome == "failed"
                else None
            )
            db.execute(
                "UPDATE outbox SET state=?,error=?,next_attempt_at=?,metadata=? WHERE id=?",
                (
                    outcome,
                    error,
                    retry_at,
                    json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False),
                    entry_id,
                ),
            )
            db.execute(
                "UPDATE tiers SET state=? WHERE outbox_id=?", (outcome, entry_id)
            )
            if row["kind"] == "time":
                payload = json.loads(row["payload"])
                state = {"sent": "completed", "failed": "queued", "unknown": "unknown"}[
                    outcome
                ]
                db.execute(
                    "UPDATE reminders SET state=? WHERE owner=? AND reminder_id=? AND revision=? AND state='queued'",
                    (state, row["owner"], payload["reminder_id"], payload["revision"]),
                )
            return True

    def resend_unknown(self, owner, entry_id):
        """Explicit user-authorized resend, with the duplicate risk in the receipt."""
        with self.store.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE id=? AND owner=? AND state='unknown'",
                (entry_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError("uncertain_delivery_not_found")
            db.execute(
                "UPDATE outbox SET state='queued',token=NULL,next_attempt_at=NULL WHERE id=?",
                (entry_id,),
            )
            if row["kind"] == "time":
                payload = json.loads(row["payload"])
                db.execute(
                    "UPDATE reminders SET state='queued' WHERE owner=? AND reminder_id=? AND revision=? AND state='unknown'",
                    (owner, payload["reminder_id"], payload["revision"]),
                )
            return {"id": entry_id, "duplicate_possible": True}
