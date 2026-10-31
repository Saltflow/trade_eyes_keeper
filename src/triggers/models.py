"""Validated JSON contracts for persistent user triggers; no executable input."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from zoneinfo import ZoneInfo

ACTION_KINDS = frozenset({"reminder", "brief", "daily"})
OPERATORS = frozenset(
    {
        "lt",
        "lte",
        "gt",
        "gte",
        "eq",
        "ne",
        "in",
        "not_in",
        "contains",
        "between",
        "exists",
        "truthy",
        "startswith",
        "endswith",
    }
)


def timestamp(value: object, zone: str = "Asia/Shanghai") -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.fromisoformat(value.isoformat())
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("a date or ISO timestamp is required")
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(zone))
    return result.astimezone(timezone.utc)


def now_utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("an aware evaluation timestamp is required")
    return result.astimezone(timezone.utc)


def safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("a bounded fact path is required")
    parts = value.split(".")
    if len(parts) > 24 or any(
        not re.fullmatch(r"[\w][\w-]*|\*", part) or part.startswith("_")
        for part in parts
    ):
        raise ValueError("invalid fact path")
    return value


def _json(value: object, limit: int = 131072):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("trigger input must be finite JSON") from exc
    if len(encoded.encode()) > limit:
        raise ValueError("trigger input is too large")
    return json.loads(encoded)


def normalize_condition(value: dict, depth: int = 0) -> dict:
    if not isinstance(value, dict) or depth > 12:
        raise ValueError("invalid or excessively nested condition")
    for key in ("all", "any"):
        if key in value:
            if (
                set(value) != {key}
                or not isinstance(value[key], list)
                or not 1 <= len(value[key]) <= 64
            ):
                raise ValueError("logical conditions require a bounded nonempty list")
            return {
                key: [normalize_condition(child, depth + 1) for child in value[key]]
            }
    if "not" in value:
        if set(value) != {"not"}:
            raise ValueError("invalid not condition")
        return {"not": normalize_condition(value["not"], depth + 1)}
    allowed = {"field", "path", "op", "value", "max_age_seconds", "quantifier"}
    if set(value) - allowed:
        raise ValueError("unknown predicate fields")
    field = safe_path(value.get("field", value.get("path")))
    operator = value.get("op", "eq")
    if operator not in OPERATORS:
        raise ValueError("unsupported predicate operator")
    if operator not in {"exists", "truthy"} and "value" not in value:
        raise ValueError("predicate value is required")
    result = {"field": field, "op": operator}
    if "value" in value:
        result["value"] = _json(value["value"])
    if operator == "between" and (
        not isinstance(result.get("value"), list) or len(result["value"]) != 2
    ):
        raise ValueError("between requires two bounds")
    if "max_age_seconds" in value:
        age = value["max_age_seconds"]
        if (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not 0 < age <= 366 * 86400
        ):
            raise ValueError("invalid freshness limit")
        result["max_age_seconds"] = float(age)
    quantifier = value.get("quantifier", "any")
    if quantifier not in {"any", "all"}:
        raise ValueError("invalid wildcard quantifier")
    result["quantifier"] = quantifier
    return result


def clock(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ):
        raise ValueError("time must use HH:MM")
    return value


def normalize_spec(spec: dict) -> dict:
    if not isinstance(spec, dict) or set(spec) - {
        "label",
        "name",
        "condition",
        "schedule",
        "event",
        "actions",
        "match_policy",
        "cooldown_seconds",
    }:
        raise ValueError(
            "invalid trigger fields; owner and route come from authenticated context"
        )
    spec = _json(spec)
    label = spec.get("label", spec.get("name", "触发器"))
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 200:
        raise ValueError("a bounded trigger label is required")
    actions = spec.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
        raise ValueError("at least one bounded action is required")
    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict) or set(action) - {
            "kind",
            "type",
            "payload",
            "text",
        }:
            raise ValueError("invalid trigger action")
        kind = action.get("kind", action.get("type"))
        if kind not in ACTION_KINDS:
            raise ValueError("action must be reminder, brief or daily")
        payload = action.get("payload", {})
        if not isinstance(payload, dict):
            raise TypeError("action payload must be an object")
        if "text" in action:
            payload = {**payload, "text": action["text"]}
        normalized_actions.append({"kind": kind, "payload": _json(payload, 65536)})
    result = {
        "label": label.strip(),
        "actions": normalized_actions,
        "match_policy": spec.get("match_policy", "edge"),
    }
    if result["match_policy"] not in {"edge", "once", "level"}:
        raise ValueError("invalid match policy")
    cooldown = spec.get("cooldown_seconds", 86400)
    if (
        isinstance(cooldown, bool)
        or not isinstance(cooldown, (int, float))
        or not 1 <= cooldown <= 366 * 86400
    ):
        raise ValueError("invalid cooldown")
    result["cooldown_seconds"] = float(cooldown)
    if spec.get("condition") is not None:
        result["condition"] = normalize_condition(spec["condition"])
    if spec.get("schedule") is not None:
        from .schedule import normalize_schedule

        result["schedule"] = normalize_schedule(spec["schedule"])
    if spec.get("event") is not None:
        event = spec["event"]
        if not isinstance(event, dict) or set(event) - {
            "kind",
            "symbols",
            "report_period",
            "day_offsets",
            "timezone",
            "notify_at",
            "day_after_time",
            "offset_times",
        }:
            raise ValueError("invalid announcement event rule")
        kind = event.get("kind")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-zA-Z][\w.-]{0,99}", kind):
            raise ValueError("invalid event kind")
        zone = event.get("timezone", "Asia/Shanghai")
        ZoneInfo(zone)
        offsets = event.get("day_offsets", [0])
        if (
            not isinstance(offsets, list)
            or not offsets
            or any(
                isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 365
                for n in offsets
            )
        ):
            raise ValueError("invalid event day offsets")
        normalized = {
            "kind": kind,
            "timezone": zone,
            "day_offsets": sorted(set(offsets)),
            "day_after_time": clock(
                event.get("day_after_time", event.get("notify_at", "09:50"))
            ),
        }
        if "notify_at" in event:
            normalized["notify_at"] = clock(event["notify_at"])
        if "symbols" in event:
            if not isinstance(event["symbols"], list) or any(
                not isinstance(n, str) or not n.strip() or len(n) > 64
                for n in event["symbols"]
            ):
                raise ValueError("invalid event symbols")
            normalized["symbols"] = sorted(
                {
                    symbol.strip().upper().replace(".", "-")
                    for symbol in event["symbols"]
                }
            )
        if "report_period" in event:
            if (
                not isinstance(event["report_period"], str)
                or not 1 <= len(event["report_period"]) <= 64
            ):
                raise ValueError("invalid event reporting period")
            normalized["report_period"] = event["report_period"]
        overrides = event.get("offset_times", {})
        if not isinstance(overrides, dict) or any(
            not str(k).isdigit() or int(k) not in offsets for k in overrides
        ):
            raise ValueError("invalid event offset time override")
        normalized["offset_times"] = {str(k): clock(v) for k, v in overrides.items()}
        result["event"] = normalized
    if not set(result) & {"condition", "schedule", "event"} or {
        "schedule",
        "event",
    } <= set(result):
        raise ValueError(
            "a trigger needs a condition, schedule or event; schedule and event are separate"
        )
    _json(result)
    return result


def collect_dependencies(spec: dict) -> set[str]:
    result = set()

    def visit(node):
        if not isinstance(node, dict):
            return
        if "field" in node or "path" in node:
            result.add(safe_path(node.get("field", node.get("path"))))
        for key in ("all", "any"):
            for child in node.get(key, []):
                visit(child)
        if "not" in node:
            visit(node["not"])

    visit(spec.get("condition"))
    if spec.get("event"):
        result.add("events." + spec["event"]["kind"])
    return result
