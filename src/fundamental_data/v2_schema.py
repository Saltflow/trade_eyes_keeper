"""Explicit data contracts; no PDF heuristics or financial verification."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import date
from itertools import combinations

from jsonschema import Draft202012Validator, FormatChecker

VERSION = "4.0"


def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("nonfinite_json")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def issue(target, detail, reason="context_uncertain", retained_value=None):
    return {
        "target": target,
        "reason": reason,
        "detail": detail,
        "retained_value": retained_value,
    }


def _period_errors(period, location):
    if not isinstance(period, dict):
        return []
    errors = []
    start, end = period.get("start"), period.get("end")
    if period.get("kind") == "instant" and (start is not None or end is None):
        errors.append(location + ": instant_requires_end_only")
    if period.get("kind") == "duration":
        if start is None or end is None:
            errors.append(location + ": duration_requires_dates")
        elif (
            isinstance(start, str)
            and isinstance(end, str)
            and (len(start) == len(end) and start > end)
        ):
            errors.append(location + ": start_after_end")
    for value in (start, end):
        if value is not None:
            try:
                if not isinstance(value, str) or not re.fullmatch(
                    r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value
                ):
                    raise ValueError("invalid_date")
                parts = value.split("-")
                # Validate the disclosed precision without changing the value.
                date(
                    int(parts[0]),
                    int(parts[1]) if len(parts) > 1 else 1,
                    int(parts[2]) if len(parts) > 2 else 1,
                )
            except (ValueError, TypeError):
                errors.append(location + ": invalid_date")
    return errors


def _table_errors(table, allow_uncovered=False):
    """Check explicit references only. Never repair a positional legacy table."""
    if not isinstance(table, dict) or not isinstance(table.get("columns"), dict):
        return []
    errors, columns = [], table["columns"]
    for key, column in columns.items():
        if isinstance(column, dict) and table.get("kind") == "numeric":
            errors += _period_errors(column.get("period"), "columns/" + key)
            comparison = column.get("comparison")
            if isinstance(comparison, dict):
                errors += _period_errors(
                    comparison.get("base_period"), "comparison/" + key
                )
    source_rows = table.get("rows", [])
    if not isinstance(source_rows, list):
        return errors
    for index, row in enumerate(source_rows):
        if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
            continue
        values, missing = row["values"], row.get("missing_reasons", {})
        if not isinstance(missing, dict):
            continue
        prefix = "rows/" + str(index)
        if row.get("row_kind") == "heading" and not values:
            if missing:
                errors.append(prefix + ": heading_has_missing_reasons")
            continue
        if set(values) - set(columns) or (
            not allow_uncovered and set(values) != set(columns)
        ):
            errors.append(prefix + ": column_keys_do_not_match")
        if set(missing) != {key for key, value in values.items() if value is None}:
            errors.append(prefix + ": null_requires_exact_missing_reason")
        overrides = row.get("cell_context", {})
        if isinstance(overrides, dict) and set(overrides) - set(columns):
            errors.append(prefix + ": unknown_cell_context_column")
        dimensions = row.get("dimensions", [])
        if isinstance(dimensions, list):
            axes = [
                d["axis"]
                for d in dimensions
                if isinstance(d, dict) and isinstance(d.get("axis"), str)
            ]
            if len(axes) != len(set(axes)):
                errors.append(prefix + ": duplicate_dimension_axis")
        if table.get("kind") == "numeric":
            statistic = row.get("statistic", {})
            if not isinstance(statistic, dict):
                continue
            for key in values:
                column = columns.get(key)
                overrides = row.get("period_overrides", {})
                period = (
                    overrides[key]
                    if isinstance(overrides, dict) and key in overrides
                    else column.get("period", {})
                    if isinstance(column, dict)
                    else {}
                )
                if not isinstance(period, dict):
                    continue
                kind = statistic.get("kind")
                if not isinstance(kind, str):
                    continue
                if kind == "point" and period.get("kind") == "duration":
                    errors.append(prefix + ": point_in_duration_column")
                if (
                    kind in {"period_total", "period_average"}
                    and period.get("kind") == "instant"
                ):
                    errors.append(prefix + ": period_measure_in_instant_column")
    return errors


def validate(config, stage, data):
    schema = config[stage + "_schema"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [
        "/".join(map(str, error.absolute_path)) + ": " + error.message
        for error in validator.iter_errors(data)
    ]

    def visit(node, path=""):
        if isinstance(node, dict):
            if isinstance(node.get("kind"), str) and node["kind"] in {
                "numeric",
                "attributes",
            }:
                errors.extend(
                    path + ": " + e
                    for e in _table_errors(node, allow_uncovered=stage == "history")
                )
            if {"kind", "start", "end", "coverage"}.issubset(node):
                errors.extend(_period_errors(node, path))
            if (
                set(node) == {"value", "precision"}
                and isinstance(node.get("precision"), str)
                and node["precision"] in {"year", "month", "day"}
            ):
                expected = {"year": 4, "month": 7, "day": 10}[node["precision"]]
                value = node["value"]
                if not isinstance(value, str) or len(value) != expected:
                    errors.append(path + ": event_date_precision_mismatch")
                else:
                    errors.extend(
                        _period_errors(
                            {"kind": "instant", "start": None, "end": value}, path
                        )
                    )
            if isinstance(node.get("method"), dict):
                method = node["method"]
                if method.get("state") == "stated" and not method.get("text"):
                    errors.append(path + ": stated_method_requires_text")
            for key, value in node.items():
                if key not in {"unstructured_blocks", "retained_value"}:
                    visit(value, path + "/" + key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, path + "/" + str(index))

    visit(data)
    if isinstance(data, dict) and stage in config.get("jobs", {}):
        job = config["jobs"][stage]
        represented = {
            item["topic"]
            for key in ("tables", "policies", "changes")
            for item in (
                data.get(key, []) if isinstance(data.get(key, []), list) else []
            )
            if isinstance(item, dict) and isinstance(item.get("topic"), str)
        }
        raw_issues = data.get("issues", [])
        raw_issues = raw_issues if isinstance(raw_issues, list) else []
        represented.update(
            item["target"][len("topics/") :]
            for item in raw_issues
            if isinstance(item, dict)
            and isinstance(item.get("target"), str)
            and item["target"].startswith("topics/")
        )
        if stage.startswith("notes_"):
            for topic in set(job["topics"]) - represented:
                errors.append("topics/" + topic + ": no_result_or_reason")
            for topic in represented - set(job["topics"]):
                errors.append("topics/" + str(topic) + ": outside_frozen_job")
    if stage == "capital" and isinstance(data, dict):
        raw_tables = data.get("tables", [])
        raw_tables = raw_tables if isinstance(raw_tables, list) else []
        tags = {
            row.get("requested_item")
            for table in raw_tables
            if isinstance(table, dict) and isinstance(table.get("rows"), list)
            for row in table["rows"]
            if isinstance(row, dict) and isinstance(row.get("requested_item"), str)
        }
        for target in set(config["minimum_capital_items"]) - tags:
            errors.append("capital_items/" + target + ": required_target_missing")
    return errors


def item_errors(config, collection, value):
    validator = Draft202012Validator(
        config["item_schemas"][collection], format_checker=FormatChecker()
    )
    return [str(error.message) for error in validator.iter_errors(value)]


def table_notes(config, table):
    """Describe existing checks without using them as a retention gate."""
    validator = Draft202012Validator(
        config["table_schema"], format_checker=FormatChecker()
    )
    structural = list(validator.iter_errors(table))

    def messages(error):
        if error.context:
            branches = {}
            for child in error.context:
                branches.setdefault(next(iter(child.schema_path)), []).append(child)
            return [
                note
                for child in min(branches.values(), key=len)
                for note in messages(child)
            ]
        return ["/".join(map(str, error.absolute_path)) + ": " + error.message]

    return [note for error in structural for note in messages(error)] + validate(
        config, "table", table
    )[len(structural) :]


def attribute_context(table, row, key):
    """Only explicit table -> column -> cell overrides, never text inference."""
    context = {
        "scope": deepcopy(table["scope"]),
        "accounting_basis": table["accounting_basis"],
        **deepcopy(table.get("context", {})),
    }
    context.update(deepcopy(table["columns"][key].get("context", {})))
    context.update(deepcopy(row.get("cell_context", {}).get(key, {})))
    return context


def combine_blocks(config, parts, company=None, year=None, report_id=None):
    """Retain parsed records; business diagnostics belong to the audit command."""
    report_id = (
        report_id or "recording_" + hashlib.sha256(stable(parts).encode()).hexdigest()
    )
    statements = parts.get("statements", {})
    identity = statements.get("company") if isinstance(statements, dict) else None
    if (
        not isinstance(identity, dict)
        or set(identity) != {"name", "code"}
        or any(
            value is not None and not isinstance(value, str)
            for value in identity.values()
        )
    ):
        identity = {"name": None, "code": None}
    period = statements.get("report_period") if isinstance(statements, dict) else None
    period = period if isinstance(period, str) else "报告期未取得"
    versions = {
        data.get("schema_version")
        for data in parts.values()
        if isinstance(data, dict) and isinstance(data.get("schema_version"), str)
    }
    result = {
        "schema_version": next(iter(versions)) if len(versions) == 1 else VERSION,
        "stage": "extract",
        "company": deepcopy(identity),
        "report_period": period,
        "reports": [
            {
                "report_id": report_id,
                "report_period": period,
                "company": deepcopy(identity),
            }
        ],
        "tables": [],
        "policies": [],
        "changes": [],
        "events": [],
        "metadata": [],
        "management": [],
        "issues": [],
        "unstructured_blocks": {},
        "block_status": {},
    }
    version = {"report_id": report_id, "report_period": period}
    for name, data in parts.items():
        stage = config.get("jobs", {}).get(name, {}).get("schema_stage", name)
        if not isinstance(data, dict):
            result["unstructured_blocks"][name] = deepcopy(data)
            result["block_status"][name] = "non_object"
            continue
        if stage + "_schema" not in config:
            result["unstructured_blocks"][name] = deepcopy(data)
            result["block_status"][name] = "unknown_block"
            continue
        result["block_status"][name] = "retained"
        retained = {}
        allowed = set(config[stage + "_schema"].get("properties", {}))
        for key, value in data.items():
            if key not in allowed:
                retained[key] = deepcopy(value)
        tables = data.get("tables", [])
        if not isinstance(tables, list):
            retained["tables"] = deepcopy(tables)
            tables = []
        for table in tables:
            item = deepcopy(table)
            if isinstance(item, dict) and item.get("kind") == "numeric":
                columns = item.get("columns", {})
                if isinstance(columns, dict):
                    for column in columns.values():
                        if isinstance(column, dict):
                            column.setdefault("reported_in", deepcopy(version))
            elif isinstance(item, dict):
                item.setdefault("reported_in", deepcopy(version))
            result["tables"].append(item)
        for collection in (
            "policies",
            "changes",
            "events",
            "metadata",
            "management",
            "issues",
        ):
            items = data.get(collection, [])
            if not isinstance(items, list):
                retained[collection] = deepcopy(items)
                continue
            for item in items:
                item = deepcopy(item)
                if collection != "issues" and isinstance(item, dict):
                    item.setdefault("reported_in", deepcopy(version))
                result[collection].append(item)
        if retained:
            result["unstructured_blocks"][name] = retained
    return result


def context_index(data):
    result = []
    tables = data.get("tables", [])
    for index, table in enumerate(tables if isinstance(tables, list) else []):
        if not isinstance(table, dict):
            result.append({"table_index": index, "raw_content": deepcopy(table)})
            continue
        columns = table.get("columns", {})
        rows = table.get("rows", [])
        result.append(
            {
                "table_index": index,
                "kind": table.get("kind"),
                "role": table.get("role", "attributes"),
                "module": table.get("module"),
                "topic": table.get("topic"),
                "title": table.get("title"),
                "part": table.get("part"),
                "scope": deepcopy(table.get("scope")),
                "accounting_basis": table.get("accounting_basis"),
                "rows": len(rows) if isinstance(rows, list) else None,
                "columns": len(columns) if isinstance(columns, dict) else None,
                "periods": [
                    deepcopy(column.get("period"))
                    for column in (
                        columns.values() if isinstance(columns, dict) else []
                    )
                    if isinstance(column, dict) and "period" in column
                ],
            }
        )
    return result


def peer_input(records, config=None):
    settings = (config or {}).get("peer_input", {})
    topics = settings.get("structure_topics", [])
    policy_topics = settings.get("policy_topics", [])
    companies, seen = [], set()
    for record in records:
        if record.get("schema_version") != VERSION:
            raise ValueError("peer_input_requires_current_contract")
        company = record["company"]
        key = company.get("code") or company.get("name")
        if key is None or key in seen:
            raise ValueError("duplicate_or_unknown_company_use_history_first")
        seen.add(key)
        companies.append(
            {
                "company": deepcopy(company),
                "reports": deepcopy(record["reports"]),
                "metadata": deepcopy(record.get("metadata", [])),
                "structure_tables": [
                    deepcopy(table)
                    for table in record.get("tables", [])
                    if table["topic"] in topics
                ],
                "policy_context": [
                    deepcopy(policy)
                    for policy in record.get("policies", [])
                    if policy["topic"] in policy_topics
                ],
                "issues": deepcopy(record.get("issues", [])),
                "context_index": context_index(record),
            }
        )
    return {"companies": companies}


def peer_errors(data, companies):
    names = [entry["company"]["name"] for entry in companies]
    wanted = {tuple(sorted(pair)) for pair in combinations(names, 2)}
    pairs = data.get("peer_pairs", [])
    actual = [tuple(sorted(pair.get("companies", []))) for pair in pairs]
    errors = []
    if set(actual) != wanted or len(actual) != len(wanted):
        errors.append("peer_pair_coverage")
    scope = data.get("research_scope", {})
    if set(scope.get("companies", [])) != set(names):
        errors.append("research_scope_companies")
    for group in data.get("groups", []):
        if set(group.get("companies", [])) - set(names):
            errors.append("unknown_group_company")
    return errors
