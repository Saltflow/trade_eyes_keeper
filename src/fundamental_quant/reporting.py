"""Bounded Bot presentation of historical metadata, never research claims."""

import html
import json

FIELDS = {
    "id", "code", "name", "company_id", "group_id", "version", "status",
    "original_status", "stage", "updated_at", "created_at", "as_of", "phase",
    "historical", "errors", "issues", "reason", "html_path", "json_path",
    "report_path", "archive_table", "archive_key", "identifiers",
}


def visible_company(company: dict) -> dict:
    """Claims stay in the archive even when an input says 'published'."""
    result = {key: value for key, value in company.items() if key in FIELDS}
    result["historical"] = True
    publication = company.get("publication")
    if isinstance(publication, dict):
        result["publication"] = {
            key: value for key, value in publication.items() if key in FIELDS
        }
    return result


def bot_view(result: dict) -> dict:
    collections = {"companies", "publications", "longitudinal_reports", "sessions"}
    if not collections.intersection(result):
        return visible_company(result)
    allowed = FIELDS | {
        "database", "artifacts", "paid_model_calls", "notice", "as_of_meaning"
    }
    compact = {key: value for key, value in result.items() if key in allowed}
    for key in collections:
        if key in result:
            compact[key] = [visible_company(item) for item in result[key]]
    compact["historical"] = True
    return compact


def format_query(result: dict) -> str:
    text = json.dumps(bot_view(result), ensure_ascii=False, indent=2, allow_nan=False)
    if len(text.encode("utf-16-le")) // 2 > 2600:
        text = text.encode("utf-16-le")[:4800].decode("utf-16-le", errors="ignore")
        text += "\n… 展示已截断；完整历史路径请用 --fundamental-quant-report。"
    return (
        "Fundamental Quant（已退役，仅历史记录；不代表当前建议）\n<pre>"
        + html.escape(text) + "</pre>"
    )
