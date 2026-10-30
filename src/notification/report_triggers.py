"""Declarative, configuration-owned predicates for scheduled reports."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

_ROOTS = {"dividend_events", "placements", "announcements", "signal_scan", "alerts"}
_PATH_PART = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*|\*)\Z")
_OPERATORS = {"exists", "non_empty", "contains", "equals", "gt", "gte", "lt", "lte"}
_MAX_CONDITIONS = 20


@dataclass(frozen=True)
class TriggerEvaluation:
    matched: bool
    configured: dict[str, Any]
    matched_conditions: tuple[int, ...]


def validate_report_trigger(value: Any) -> None:
    """Validate a small data-only predicate document; it cannot name code."""
    if value is None or (isinstance(value, (dict, list)) and not value):
        return
    if not isinstance(value, dict) or set(value) - {"mode", "conditions"}:
        raise ValueError("触发规则仅接受 mode 和 conditions")
    mode = value.get("mode", "any")
    conditions = value.get("conditions")
    if not isinstance(mode, str) or mode not in {"any", "all"}:
        raise ValueError("触发规则 mode 必须为 any 或 all")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= _MAX_CONDITIONS:
        raise ValueError("触发规则 conditions 必须有 1 到 20 项")
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) - {
            "path", "operator", "value"
        }:
            raise ValueError("每条触发条件仅接受 path、operator 和 value")
        path = condition.get("path")
        parts = path.split(".") if isinstance(path, str) else []
        if (
            not 1 <= len(parts) <= 8
            or parts[0] not in _ROOTS
            or any(not _PATH_PART.fullmatch(part) for part in parts)
        ):
            raise ValueError("触发条件 path 不在允许的报告数据范围")
        operator = condition.get("operator")
        if not isinstance(operator, str) or operator not in _OPERATORS:
            raise ValueError("触发条件 operator 无效")
        if operator in {"exists", "non_empty"}:
            if "value" in condition:
                raise ValueError(f"{operator} 不接受 value")
        else:
            if "value" not in condition:
                raise ValueError(f"{operator} 必须提供 value")
            operand = condition["value"]
            if isinstance(operand, (dict, list)) or operand is None:
                raise ValueError("触发条件 value 必须为有限标量")
            if isinstance(operand, float) and not math.isfinite(operand):
                raise ValueError("触发条件 value 必须为有限标量")
            if operator in {"gt", "gte", "lt", "lte"} and (
                type(operand) not in {int, float}
                or (isinstance(operand, float) and not math.isfinite(operand))
            ):
                raise ValueError(f"{operator} 的 value 必须为有限数字")


def required_report_data_roots(configured: Any) -> set[str]:
    """Return data roots referenced by validated rules for report preparation."""
    validate_report_trigger(configured)
    if not configured:
        return set()
    return {condition["path"].split(".", 1)[0] for condition in configured["conditions"]}


def evaluate_report_triggers(session: Any, configured: Any) -> TriggerEvaluation:
    """Evaluate configured predicates with any/all semantics, without event logic."""
    validate_report_trigger(configured)
    if not configured:
        return TriggerEvaluation(True, {}, ())
    results = tuple(
        _evaluate_condition(session, condition)
        for condition in configured["conditions"]
    )
    matched = all(results) if configured.get("mode", "any") == "all" else any(results)
    return TriggerEvaluation(
        matched,
        configured,
        tuple(index for index, value in enumerate(results) if value),
    )


def _resolve_path(value: Any, parts: list[str]) -> list[Any]:
    values = [value]
    for part in parts:
        expanded = []
        for item in values:
            if part == "*":
                if isinstance(item, dict):
                    expanded.extend(item.values())
                elif isinstance(item, (list, tuple, set)):
                    expanded.extend(item)
            elif isinstance(item, dict) and part in item:
                expanded.append(item[part])
            elif hasattr(item, part) and not part.startswith("_"):
                attribute = getattr(item, part)
                if not callable(attribute):
                    expanded.append(attribute)
            elif isinstance(item, (list, tuple, set)):
                expanded.extend(
                    child[part]
                    for child in item
                    if isinstance(child, dict) and part in child
                )
        values = expanded
    return values


def _evaluate_condition(session: Any, condition: dict[str, Any]) -> bool:
    parts = condition["path"].split(".")
    root = parts[0]
    if isinstance(session, dict):
        if root not in session:
            return False
        initial = session[root]
    elif hasattr(session, root):
        initial = getattr(session, root)
    else:
        return False
    values = _resolve_path(initial, parts[1:])
    if not values:
        return False
    operator = condition["operator"]
    if operator == "exists":
        return True
    if operator == "non_empty":
        return any(bool(value) for value in values)
    operand = condition["value"]
    for value in values:
        try:
            if operator == "contains" and isinstance(value, (str, list, tuple, set)):
                if operand in value:
                    return True
            elif operator == "equals" and type(value) is type(operand) and value == operand:
                return True
            elif operator in {"gt", "gte", "lt", "lte"} and type(value) in {
                int, float
            } and not isinstance(value, bool) and (
                isinstance(value, int) or math.isfinite(value)
            ):
                if operator == "gt" and value > operand:
                    return True
                if operator == "gte" and value >= operand:
                    return True
                if operator == "lt" and value < operand:
                    return True
                if operator == "lte" and value <= operand:
                    return True
        except (TypeError, ValueError):
            continue
    return False
