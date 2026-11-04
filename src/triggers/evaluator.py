"""Three-valued conditions and durable occurrence admission; no I/O providers."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from numbers import Real

from zoneinfo import ZoneInfo

from .models import now_utc, safe_path, timestamp
from .schedule import due_times, next_due

TRUE, FALSE, UNKNOWN = "true", "false", "unknown"


@dataclass(frozen=True)
class ConditionResult:
    truth: str
    reasons: tuple[str, ...] = ()


def _get(value, key, default=None):
    return (
        value.get(key, default)
        if isinstance(value, Mapping)
        else getattr(value, key, default)
    )


def _combine(results, any_match):
    truths = [item.truth for item in results]
    if (TRUE if any_match else FALSE) in truths:
        truth = TRUE if any_match else FALSE
    elif UNKNOWN in truths or not truths:
        truth = UNKNOWN
    else:
        truth = FALSE if any_match else TRUE
    return ConditionResult(
        truth, tuple(reason for item in results for reason in item.reasons)
    )


def _resolve(snapshot, field):
    safe_path(field)
    if snapshot is None:
        return {"status": "unknown"}
    resolver = getattr(snapshot, "resolve", None)
    if callable(resolver):
        return resolver(field)
    facts = _get(snapshot, "facts", _get(snapshot, "values", snapshot))
    if isinstance(facts, Mapping) and field in facts:
        item = facts[field]
    else:
        item = facts
        for part in field.split("."):
            if not isinstance(item, Mapping) or part not in item:
                return {"status": "unknown"}
            item = item[part]
    if isinstance(item, Mapping) and "status" in item and "value" in item:
        return item
    return {
        "status": "available",
        "value": item,
        "as_of": _get(snapshot, "as_of"),
        "observed_at": _get(snapshot, "observed_at"),
    }


def _compare(left, operator, right):
    if operator == "exists":
        return left is not None
    if operator == "truthy":
        return bool(left)
    if operator in {"lt", "lte", "gt", "gte", "between"}:
        numbers = [left, *(right if operator == "between" else [right])]
        if not all(
            isinstance(n, Real) and not isinstance(n, bool) and math.isfinite(n)
            for n in numbers
        ):
            raise ValueError("ordered comparisons need finite numbers")
    if operator in {"eq", "ne"}:
        if isinstance(left, bool) != isinstance(right, bool):
            return operator == "ne"
        if isinstance(left, Real) and not math.isfinite(left):
            raise ValueError("nonfinite observation")
        return (left == right) if operator == "eq" else (left != right)
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    if operator == "between":
        return right[0] <= left <= right[1]
    if operator == "in":
        return left in right
    if operator == "not_in":
        return left not in right
    if operator == "contains":
        return right in left
    if operator == "startswith":
        if not isinstance(left, str) or not isinstance(right, str):
            raise TypeError("string observation and prefix are required")
        return left.startswith(right)
    if operator == "endswith":
        if not isinstance(left, str) or not isinstance(right, str):
            raise TypeError("string observation and suffix are required")
        return left.endswith(right)
    raise ValueError("unsupported comparison")


def evaluate_condition(condition, snapshot, now: datetime) -> ConditionResult:
    now = now_utc(now)
    if condition is None:
        return ConditionResult(TRUE)
    for key in ("all", "any"):
        if key in condition:
            return _combine(
                [evaluate_condition(child, snapshot, now) for child in condition[key]],
                key == "any",
            )
    if "not" in condition:
        result = evaluate_condition(condition["not"], snapshot, now)
        return ConditionResult(
            {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[result.truth], result.reasons
        )
    field = condition["field"]

    def check(fact):
        status = _get(fact, "status", "unknown")
        if status not in {"available", "observed", "known_zero", "derived"}:
            return ConditionResult(UNKNOWN, (field + ":" + str(status),))
        for name in ("observed_at", "as_of"):
            has_metadata = (
                name in fact if isinstance(fact, Mapping) else hasattr(fact, name)
            )
            observed = _get(fact, name) if has_metadata else _get(snapshot, name)
            if observed is None:
                if "max_age_seconds" in condition:
                    return ConditionResult(UNKNOWN, (field + ":missing_timestamp",))
                continue
            try:
                age = (now - timestamp(observed)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return ConditionResult(UNKNOWN, (field + ":invalid_timestamp",))
            if age < (-5 if name == "observed_at" else 0):
                return ConditionResult(UNKNOWN, (field + ":future_observation",))
            if "max_age_seconds" in condition and age > condition["max_age_seconds"]:
                return ConditionResult(UNKNOWN, (field + ":stale",))
        try:
            matched = _compare(
                _get(fact, "value"), condition["op"], condition.get("value")
            )
            return ConditionResult(TRUE if matched else FALSE)
        except (TypeError, ValueError, KeyError, OverflowError):
            return ConditionResult(UNKNOWN, (field + ":incompatible_value",))

    try:
        fact = _resolve(snapshot, field)
    except (TypeError, ValueError, KeyError, AttributeError):
        return ConditionResult(UNKNOWN, (field + ":unavailable",))
    values = _get(fact, "value")
    if "*" in field and isinstance(values, list):
        results = [
            check(
                item
                if _get(item, "status") is not None
                else {
                    "status": "available",
                    "value": item,
                    "as_of": _get(fact, "as_of"),
                    "observed_at": _get(fact, "observed_at"),
                }
            )
            for item in values
        ]
        if _get(fact, "status") not in {
            "available",
            "observed",
            "known_zero",
            "derived",
        }:
            results.append(ConditionResult(UNKNOWN, (field + ":partial_coverage",)))
        return _combine(results, condition.get("quantifier", "any") == "any")
    return check(fact)


def event_occurrences(rule, snapshot, now: datetime, *, future=False):
    zone, now = ZoneInfo(rule["timezone"]), now_utc(now)
    local = now.astimezone(zone)
    for event in _get(snapshot, "events", []) or []:
        if _get(event, "kind") != rule["kind"]:
            continue
        if rule.get("symbols") and _get(event, "symbol") not in rule["symbols"]:
            continue
        period, actual = (
            rule.get("report_period"),
            str(_get(event, "report_period", "")),
        )
        if period and not (
            actual.upper().endswith(period.upper())
            if re.fullmatch(r"Q[1-4]", period, re.IGNORECASE)
            else actual == period
        ):
            continue
        identity = _get(event, "id")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 512:
            continue
        try:
            publication = timestamp(
                _get(event, "published_at") or _get(event, "occurred_at")
            )
        except (ValueError, TypeError, OverflowError):
            continue
        if publication > now:
            continue
        for offset in rule["day_offsets"]:
            day = publication.astimezone(zone).date() + timedelta(days=offset)
            target_clock = rule.get("offset_times", {}).get(
                str(offset),
                rule.get("notify_at") if offset == 0 else rule["day_after_time"],
            )
            if target_clock:
                hour, minute = map(int, target_clock.split(":"))
                due = timestamp(
                    datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                )
                due = max(due, publication)
            else:
                due = publication
            if future:
                matched = due > now
            else:
                matched = local.date() == day and due <= now
            if matched:
                yield {
                    "key": f"event:{identity}:day:{offset}:{day}",
                    "due": due,
                    "context": {
                        "event_id": identity,
                        "kind": rule["kind"],
                        "symbol": _get(event, "symbol"),
                        "report_period": actual,
                        "title": _get(event, "title", ""),
                        "url": _get(event, "url", ""),
                        "published_at": publication.isoformat(),
                        "publication_date": (
                            str(_get(event, "publication_date"))
                            if _get(event, "publication_date")
                            else publication.astimezone(zone).date().isoformat()
                        ),
                        "source": _get(event, "source", ""),
                        "source_documents": list(
                            _get(event, "source_documents", ()) or ()
                        ),
                        "day_offset": offset,
                        "date": day.isoformat(),
                    },
                }


def dividend_target(rule, state, snapshot, now):
    """Select public dates, retaining only a minimal restart-safe target.

    Absence is not cancellation. A supported replacement for the same security
    and ex-date supersedes the old date; failures keep its original provenance.
    """
    local = now.astimezone(ZoneInfo(rule["timezone"]))
    try:
        saved = json.loads(state.get("event_state") or "{}")
    except (TypeError, ValueError):
        saved = {}
    fact = _resolve(snapshot, "events.dividend.next_payment")
    value = _get(fact, "value") or {}
    if not isinstance(value, Mapping):
        value = {}
    symbols = _get(snapshot, "wildcard_symbols")
    if symbols is None:
        symbols = value.get("monitored_symbols")
    requested = rule.get("symbols")

    def included(row):
        code = str(row.get("code", "")).upper().replace(".", "-")
        return (symbols is None or code in symbols) and (
            not requested or code in requested
        )

    old = [row for row in saved.get("events", []) if included(row)]
    rows = [
        row
        for row in value.get("events", [])
        if isinstance(row, Mapping) and included(row)
    ]
    # A later explicit disclosure supersedes an older one. When publication
    # dates are unavailable, use the existing source priority, not read time.
    rank = {
        "announcement": 0,
        "fund_distribution": 1,
        "hk_dividend_schedule": 1,
        "stored_corporate_action": 2,
        "yahoo_calendar": 3,
    }
    selected = {}
    for row in sorted(
        rows,
        key=lambda r: (
            bool(r.get("published_at")),
            str(r.get("published_at") or ""),
            -rank.get(r.get("source"), 9),
        ),
    ):
        if row.get("ex_date"):
            selected[(row.get("code"), row["ex_date"])] = dict(row)
    retained = []
    for row in old:
        key = (row.get("code"), row.get("ex_date"))
        replacement = selected.get(key)
        if (
            replacement
            and row.get("published_at")
            and (str(replacement.get("published_at") or "") < str(row["published_at"]))
        ):
            selected.pop(key)
            replacement = None
        if replacement is None:
            retained.append({**row, "freshness": "unconfirmed"})
    current = [
        {
            **row,
            "freshness": (
                "unconfirmed"
                if row.get("freshness") == "unconfirmed"
                or _get(fact, "status") not in {"available", "partial"}
                else "observed"
            ),
        }
        for row in selected.values()
        if not row.get("cancelled")
    ]
    candidates = []
    for row in retained + current:
        try:
            day = (
                timestamp(str(row.get("payment_date")))
                .astimezone(ZoneInfo(rule["timezone"]))
                .date()
            )
        except (ValueError, TypeError):
            continue
        # Old persisted targets are marked missed, never silently advanced to
        # another dividend after an outage. Historical newly-read rows are not
        # pending targets and may be ignored.
        if (
            day >= local.date()
            or row in retained
            or any(
                old_row.get("code") == row.get("code")
                and old_row.get("ex_date") == row.get("ex_date")
                for old_row in old
            )
        ):
            candidates.append(row)
    if not candidates:
        return {"events": [], "status": "waiting_for_data"}, None
    day = min(row["payment_date"] for row in candidates)
    events = sorted(
        (row for row in candidates if row["payment_date"] == day),
        key=lambda row: (row["code"], row.get("ex_date", "")),
    )
    target = {"events": events, "payment_date": day, "status": "pending"}
    if day < local.date().isoformat():
        target["status"] = "missed"
        return target, None
    hour, minute = map(int, rule["notify_at"].split(":"))
    due = datetime.fromisoformat(day).replace(
        hour=hour, minute=minute, tzinfo=ZoneInfo(rule["timezone"])
    )
    occurrence = {
        "key": "dividend:next_payment:" + day,
        "due": due,
        "context": {
            "event_id": "dividend:next_payment:" + day,
            "kind": "dividend.next_payment",
            "date": day,
            "payment_date": day,
            "dividends": events,
            "source": "public_dividend_schedule",
            "title": "公开派息日（非券商实际到账确认）",
            "freshness": "unconfirmed"
            if any(row.get("freshness") == "unconfirmed" for row in events)
            else "observed",
        },
    }
    return target, occurrence


class TriggerEvaluator:
    def __init__(self, store):
        self.store = store
        self.last_outcomes = []

    def evaluate(self, snapshot=None, now=None, owner=None):
        now = now_utc(now)
        actions, outcomes = [], []
        for selected in self.store.active(owner):
            with self.store.transaction() as db:
                trigger = self.store.get(selected["owner"], selected["number"], db=db)
                if trigger is None or trigger["state"] != "active":
                    continue
                spec = trigger["spec"]
                state = self.store.evaluation_state(
                    trigger["owner"], trigger["number"], db=db
                )
                if state["last_evaluated_at"] and now < timestamp(
                    state["last_evaluated_at"]
                ):
                    outcomes.append(
                        {
                            "owner": trigger["owner"],
                            "number": trigger["number"],
                            "truth": UNKNOWN,
                            "reasons": ["out_of_order_evaluation"],
                            "actions_created": 0,
                        }
                    )
                    continue
                result = evaluate_condition(spec.get("condition"), snapshot, now)
                updates = {
                    "diagnostic": json.dumps(
                        {"truth": result.truth, "reasons": result.reasons}
                    ),
                }
                if result.truth != UNKNOWN:
                    updates["last_truth"] = result.truth
                    updates["last_evaluated_at"] = now.isoformat()
                candidates = []
                if result.truth == TRUE and not (
                    spec["match_policy"] == "once" and state["fired_once"]
                ):
                    if spec.get("event", {}).get("kind") == "dividend.next_payment":
                        target, occurrence = dividend_target(
                            spec["event"], state, snapshot, now
                        )
                        updates["event_state"] = json.dumps(target, ensure_ascii=False)
                        updates["diagnostic"] = json.dumps(
                            {"truth": result.truth, "status": target["status"]}
                        )
                        if target["status"] == "missed":
                            updates["fired_once"] = 1
                        if occurrence and occurrence["due"] <= now:
                            candidates = [occurrence]
                    elif "event" in spec:
                        candidates = list(
                            event_occurrences(spec["event"], snapshot, now)
                        )
                    elif "schedule" in spec:
                        previous = (
                            timestamp(state["last_evaluated_at"])
                            if state["last_evaluated_at"]
                            else None
                        )
                        candidates = [
                            {
                                "key": "schedule:" + due.isoformat(),
                                "context": {"scheduled_at": due.isoformat()},
                            }
                            for due in due_times(spec["schedule"], now, previous)
                        ]
                    else:
                        previous = (
                            timestamp(state["last_fired_at"])
                            if state["last_fired_at"]
                            else None
                        )
                        matched = (
                            spec["match_policy"] != "level"
                            and state["last_truth"] != TRUE
                        ) or (
                            spec["match_policy"] == "level"
                            and (
                                previous is None
                                or (now - previous).total_seconds()
                                >= spec["cooldown_seconds"]
                            )
                        )
                        if matched:
                            episode = state["episode"] + 1
                            updates["episode"] = episode
                            candidates = [
                                {
                                    "key": f"edge:{episode}",
                                    "context": {"matched_at": now.isoformat()},
                                }
                            ]
                created = []
                for candidate in candidates:
                    admitted = self.store.record_occurrence(
                        trigger, candidate["key"], candidate["context"], now, db=db
                    )
                    created.extend(admitted)
                    if admitted and spec["match_policy"] == "once":
                        break
                if created:
                    updates.update(fired_once=1, last_fired_at=now.isoformat())
                self.store.record_state(
                    trigger["owner"], trigger["number"], updates, db=db
                )
                if updates.get("event_state") and target["status"] == "missed":
                    self.store._finish_if_settled(
                        db, trigger["owner"], trigger["number"], now.isoformat()
                    )
                actions.extend(created)
                outcomes.append(
                    {
                        "owner": trigger["owner"],
                        "number": trigger["number"],
                        "truth": result.truth,
                        "reasons": list(result.reasons),
                        "actions_created": len(created),
                    }
                )
        self.last_outcomes = outcomes
        return actions

    def next_deadline(self, now=None, snapshot=None, owner=None):
        now = now_utc(now)
        candidates = []
        for trigger in self.store.active(owner):
            spec = trigger["spec"]
            if "schedule" in spec:
                due = next_due(spec["schedule"], now)
                if due is not None:
                    candidates.append(due)
            if spec.get("event", {}).get("kind") == "dividend.next_payment":
                state = self.store.evaluation_state(trigger["owner"], trigger["number"])
                _, occurrence = dividend_target(spec["event"], state, snapshot, now)
                if occurrence and occurrence["due"] > now:
                    candidates.append(occurrence["due"])
            elif "event" in spec:
                candidates.extend(
                    item["due"]
                    for item in event_occurrences(
                        spec["event"], snapshot, now, future=True
                    )
                )
        return min(candidates, default=None)
