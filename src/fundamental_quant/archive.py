"""Read existing SQLite history without migrations, evaluation or transport."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

TABLES = {
    "companies": ("companies", "id"),
    "publications": ("publications", "version"),
    "longitudinal_publications": ("longitudinal_reports", "version"),
    "pdf_research_sessions": ("sessions", "id"),
    "research_report_staging": ("publications", "version"),
}
REQUIRED = {
    "companies": {"company", "stage"},
    "publications": {"company_id", "status", "payload"},
    "longitudinal_publications": {"group_id", "status", "payload"},
    "pdf_research_sessions": {"company_id", "state"},
    "research_report_staging": {"company_id", "status", "payload"},
}
META = {
    "id", "company_id", "group_id", "version", "status", "stage", "updated_at",
    "created_at", "html_path", "json_path", "report_path", "reason",
}


def _metadata(row, table, key):
    item = {field: row[field] for field in META if field in row}
    identifiers = {str(row[field]) for field in (
        "id", "company_id", "group_id"
    ) if row.get(field) is not None}
    for field in ("company", "payload", "state"):
        payload = json.loads(row[field]) if row.get(field) else {}
        if not isinstance(payload, dict):
            raise TypeError("archive_json_object_required")
        company = payload if field == "company" else payload.get("company", {})
        if isinstance(company, dict):
            for name in ("id", "code", "name"):
                if company.get(name) is not None:
                    item.setdefault(name, company[name])
                    if name != "name":
                        identifiers.add(str(company[name]))
        packet = payload.get("packet", {})
        for company in packet.get("spec", {}).get("companies", []):
            identifiers.update(str(company[name]) for name in (
                "id", "issuer_id", "code"
            ) if company.get(name) is not None)
        for name in ("as_of", "phase", "status"):
            if payload.get(name) is not None:
                item.setdefault(name, payload[name])
    identifiers.update(value.split(":")[-1] for value in tuple(identifiers))
    item.update(historical=True, identifiers=sorted(identifiers))
    item["original_status"] = item.get("status", row.get("stage"))
    item.update(archive_table=table, archive_key=row[key])
    return item


def read_archive(root: Path) -> dict:
    database = root.resolve() / "research.sqlite3"
    result = {
        "status": "historical", "historical": True, "paid_model_calls": 0,
        "notice": "Retired research archive; saved records are not current advice.",
        "database": str(database), "companies": [], "publications": [],
        "longitudinal_reports": [], "sessions": [], "artifacts": {}, "errors": [],
    }
    for name in ("index.html", "report.json", "report.csv", "research_notes/index.html"):
        path = root / name
        if path.is_file():
            result["artifacts"][name] = str(path.resolve())
    if not database.is_file():
        result.update(status="not_started", errors=[{"code": "research_not_started"}])
        return result
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not tables.intersection(TABLES):
                raise ValueError("archive_schema_unknown")
            for table, (target, key) in TABLES.items():
                if table not in tables:
                    continue
                columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if not ({key} | REQUIRED[table]).issubset(columns):
                    raise ValueError("archive_schema_invalid:" + table)
                for row in db.execute(f'SELECT * FROM "{table}"'):
                    result[target].append(_metadata(dict(row), table, key))
    except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        result["status"] = "error"
        result["errors"].append({"code": "archive_unreadable", "detail": str(exc)})
    return result
