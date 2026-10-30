"""Mechanical joins on declared identities, with original fields preserved."""

from collections import Counter
from copy import deepcopy

from .v2_schema import VERSION, context_index, stable


def _code(value):
    return isinstance(value, str) and value not in {"", "unknown"}


def _row_key(row):
    if not _code(row.get("identity_code")) or not _code(row.get("unit_code")):
        return None
    statistic = row.get("statistic")
    if not isinstance(statistic, dict) or not _code(statistic.get("kind")):
        return None
    kind, method = statistic["kind"], statistic.get("method", {})
    if kind in {"period_average", "average"} and (
        not isinstance(method, dict) or method.get("state") != "stated"
    ):
        return None
    if kind in {"period_measure", "rate"} and statistic.get("annualized") in (
        None,
        "not_specified",
    ):
        return None
    return stable(
        [
            row["identity_code"],
            row["unit_code"],
            row.get("currency"),
            sorted(row.get("dimensions", []), key=stable),
            row.get("statistic"),
            row.get("definition"),
        ]
    )


def _table_key(table):
    scope, basis = table.get("scope", {}), table.get("accounting_basis_code")
    if not all(
        _code(value)
        for value in (
            table.get("identity_code"),
            scope.get("entity_code"),
            scope.get("kind"),
            basis,
        )
    ):
        return None
    if basis == "REGULATORY" and not _code(table.get("accounting_basis")):
        return None
    return stable(
        [
            table["identity_code"],
            scope["kind"],
            scope["entity_code"],
            basis,
            table.get("accounting_basis") if basis == "REGULATORY" else None,
        ]
    )


def _mergeable(table, config=None):
    """Only readable structure and declared identity; no business validation."""
    if not isinstance(table, dict) or table.get("kind") != "numeric":
        return False
    if not isinstance(table.get("scope"), dict) or _table_key(table) is None:
        return False
    columns, rows = table.get("columns"), table.get("rows")
    return isinstance(columns, dict) and isinstance(rows, list)


def _readable_row(table, row):
    return (
        isinstance(row, dict)
        and isinstance(row.get("values"), dict)
        and isinstance(row.get("dimensions", []), list)
        and not (set(row["values"]) - set(table["columns"]))
        and all(isinstance(table["columns"][key], dict) for key in row["values"])
    )


def _effective_period(row, key, column):
    overrides = row.get("period_overrides", {})
    if not isinstance(overrides, dict):
        return None
    return deepcopy(overrides[key] if key in overrides else column.get("period"))


def _explicit_period(period):
    if not isinstance(period, dict) or not _code(period.get("end")):
        return False
    return period.get("kind") == "instant" or (
        period.get("kind") == "duration" and _code(period.get("start"))
    )


def _reports(table, row):
    return {
        column.get("reported_in", {}).get("report_id")
        for key, column in table["columns"].items()
        if key in row["values"]
        and isinstance(column, dict)
        and isinstance(column.get("reported_in"), dict)
        and _code(column["reported_in"].get("report_id"))
    }


def merge_tables(entries):
    """Origin identifies preserved source records, never semantic matching."""
    result = deepcopy(entries[0][1])
    result["columns"], result["rows"], result["source_fields"] = {}, [], []
    duplicates = Counter(
        (report, _row_key(row))
        for _, table in entries
        for row in table["rows"]
        if _readable_row(table, row)
        for report in _reports(table, row)
        if _row_key(row) is not None
    )
    rows, columns = {}, {}
    for origin, table in entries:
        result["source_fields"].append(
            {
                "origin": origin,
                "fields": deepcopy({k: v for k, v in table.items() if k != "rows"}),
            }
        )
        for row_index, source in enumerate(table["rows"]):
            if not _readable_row(table, source):
                item = deepcopy(source)
                if isinstance(item, dict):
                    item["alignment"] = "independent"
                    item["source_fields"] = [
                        {
                            "origin": origin,
                            "columns": deepcopy(table["columns"]),
                            "fields": deepcopy(source),
                        }
                    ]
                result["rows"].append(item)
                continue
            identity = _row_key(source)
            ambiguous = identity is None or any(
                duplicates[(report, identity)] > 1 for report in _reports(table, source)
            )
            key = stable(["source", origin, row_index]) if ambiguous else identity
            if key not in rows:
                row = deepcopy(source)
                row.update(
                    values={}, missing_reasons={}, period_overrides={}, issues=[]
                )
                row["source_fields"] = []
                rows[key] = row
                result["rows"].append(row)
            row, mapping, semantics = rows[key], {}, {}
            for source_key in source["values"]:
                column = table["columns"][source_key]
                period = _effective_period(source, source_key, column)
                version = column.get("reported_in")
                semantic = stable(
                    [
                        version,
                        period,
                        column.get("identity_code"),
                        column.get("adjustment"),
                        column.get("comparison"),
                    ]
                )
                explicit = (
                    _explicit_period(period)
                    and _code(column.get("identity_code"))
                    and isinstance(version, dict)
                    and _code(version.get("report_id"))
                )
                if column.get("identity_code") in (
                    "YOY",
                    "QOQ",
                    "OTHER_COMPARISON",
                ):
                    comparison = column.get("comparison")
                    explicit = (
                        explicit
                        and isinstance(comparison, dict)
                        and _explicit_period(comparison.get("base_period"))
                    )
                semantics[source_key] = (semantic, period, explicit)
            counts = Counter(value[0] for value in semantics.values())
            for source_key, value in source["values"].items():
                semantic, period, explicit = semantics[source_key]
                if not explicit or counts[semantic] > 1:
                    semantic += stable([origin, row_index, source_key])
                if semantic not in columns:
                    column_key = "c" + str(len(columns) + 1)
                    columns[semantic] = column_key
                    column = deepcopy(table["columns"][source_key])
                    column["period"] = period
                    result["columns"][column_key] = column
                column_key = columns[semantic]
                if column_key in row["values"]:
                    original_column = result["columns"][column_key]
                    semantic += stable([origin, row_index, source_key, "repeat"])
                    column_key = "c" + str(len(columns) + 1)
                    columns[semantic] = column_key
                    result["columns"][column_key] = deepcopy(original_column)
                row["values"][column_key] = deepcopy(value)
                missing = source.get("missing_reasons", {})
                if isinstance(missing, dict) and source_key in missing:
                    row["missing_reasons"][column_key] = deepcopy(missing[source_key])
                mapping[source_key] = column_key
            row["source_fields"].append(
                {
                    "origin": origin,
                    "row_index": row_index,
                    "reported_in": [
                        deepcopy(table["columns"][k].get("reported_in"))
                        for k in source["values"]
                    ],
                    "fields": deepcopy(
                        {k: v for k, v in source.items() if k != "values"}
                    ),
                    "column_map": mapping,
                }
            )
            if isinstance(source.get("issues"), list):
                row["issues"].extend(deepcopy(source["issues"]))
    result["issues"] = [
        deepcopy(item)
        for _, table in entries
        for item in (
            table.get("issues", []) if isinstance(table.get("issues", []), list) else []
        )
    ]
    return result, []


def _shared_policies(policies):
    grouped, result = {}, []
    for policy in policies:
        if not isinstance(policy, dict):
            result.append(deepcopy(policy))
            continue
        fields = {key: value for key, value in policy.items() if key != "reported_in"}
        key = stable(fields)
        if key not in grouped:
            item = deepcopy(fields)
            item["appears_in"] = []
            grouped[key] = item
            result.append(item)
        version = deepcopy(policy.get("reported_in"))
        if version not in grouped[key]["appears_in"]:
            grouped[key]["appears_in"].append(version)
    return result


def assemble(records, config=None):
    if not records:
        raise ValueError("no_extraction_results")
    identities, reports = [], {}
    for record in records:
        if record.get("schema_version") not in ("3.0", "4.0"):
            raise ValueError("unsupported_history_contract_no_migration")
        company = record["company"]
        identity = company.get("code") or company.get("name")
        if identity is None:
            raise ValueError("history_requires_known_company")
        identities.append(identity)
        for report in record["reports"]:
            if not _code(report.get("report_id")):
                raise ValueError("history_requires_known_report_id")
            report_company = report.get("company")
            if isinstance(report_company, dict):
                field = (
                    "code"
                    if company.get("code") and report_company.get("code")
                    else "name"
                )
                if (
                    company.get(field) is not None
                    and report_company.get(field) is not None
                    and report_company[field] != company[field]
                ):
                    raise ValueError("report_company_does_not_match_record")
            previous = reports.get(report["report_id"])
            if previous is not None and previous != report:
                raise ValueError("same_report_id_has_different_report_period")
            reports[report["report_id"]] = deepcopy(report)
        declared_reports = {report["report_id"] for report in record["reports"]}
        for table in record["tables"]:
            if not isinstance(table, dict) or not isinstance(
                table.get("columns"), dict
            ):
                continue
            for column in table["columns"].values():
                version = (
                    column.get("reported_in") if isinstance(column, dict) else None
                )
                if (
                    isinstance(version, dict)
                    and _code(version.get("report_id"))
                    and version["report_id"] not in declared_reports
                ):
                    raise ValueError("column_report_id_outside_record")
    if len(set(identities)) != 1:
        raise ValueError("assemble_one_company_at_a_time")
    records = sorted(records, key=lambda record: stable(record["reports"]))
    result = {
        "schema_version": VERSION,
        "stage": "history",
        "company": deepcopy(records[0]["company"]),
        "report_period": "多期",
        "reports": list(reports.values()),
        "tables": [],
        "policies": [],
        "changes": [],
        "events": [],
        "metadata": [],
        "management": [],
        "issues": [],
        "unstructured_blocks": {},
        "block_status": {},
        "conflicts": [],
    }
    groups = {}
    for record_index, record in enumerate(records):
        for collection in (
            "policies",
            "changes",
            "events",
            "metadata",
            "management",
            "issues",
        ):
            items = record.get(collection, [])
            if isinstance(items, list):
                result[collection].extend(deepcopy(items))
            else:
                result["unstructured_blocks"][str(record_index) + "/" + collection] = (
                    deepcopy(items)
                )
        if record.get("unstructured_blocks"):
            result["unstructured_blocks"][str(record_index)] = deepcopy(
                record["unstructured_blocks"]
            )
        result["block_status"][str(record_index)] = deepcopy(
            record.get("block_status", {})
        )
        for table_index, table in enumerate(record["tables"]):
            origin = stable([record_index, table_index, record["reports"]])
            legacy = record["schema_version"] == "3.0"
            if legacy or not _mergeable(table):
                item = deepcopy(table)
                if isinstance(item, dict):
                    item["alignment"] = "legacy_preserved" if legacy else "independent"
                result["tables"].append(item)
                continue
            groups.setdefault(_table_key(table), []).append((origin, table))
    for entries in groups.values():
        table, _ = merge_tables(entries)
        result["tables"].append(table)
    result["shared_policies"] = _shared_policies(result["policies"])
    result["context_index"] = context_index(result)
    return result
