"""Carry matched disclosure evidence into the complete daily report."""

from __future__ import annotations

import re
from copy import deepcopy
from urllib.parse import urlsplit

from .models import timestamp


def is_managed_daily(config: dict) -> bool:
    """The worker separately verifies the durable authenticated binding."""
    binding = config.get("_trigger_context")
    return isinstance(binding, dict) and binding.get("kind") == "daily"


def populate_trigger_announcements(session, config: dict) -> None:
    """Merge original event evidence without fabricating missing disclosures.

    Event context is supplied by the evaluator, never the LLM's action payload.
    Preserve both the original publication timestamp and provider's other rows.
    """
    binding = config.get("_trigger_context") or {}
    announcements = deepcopy(getattr(session, "announcements", {}) or {})
    for event in binding.get("events", []):
        if not isinstance(event, dict):
            continue
        code, title, url = (event.get(key) for key in ("symbol", "title", "url"))
        if not all(
            isinstance(value, str) and value.strip() for value in (code, title, url)
        ):
            continue
        try:
            parsed = urlsplit(url)
            published = timestamp(event.get("published_at"))
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or re.search(r"[\x00-\x20\x7f]", url)
        ):
            continue
        items = announcements.setdefault(code, [])
        if any(item.get("url") == url for item in items if isinstance(item, dict)):
            continue
        items.insert(
            0,
            {
                "title": title,
                "url": url,
                "date": event.get("publication_date")
                or str(event["published_at"])[:10],
                "published_at": published.isoformat(),
                "exchange": event.get("source", ""),
                "report_period": event.get("report_period", ""),
                "source_documents": deepcopy(event.get("source_documents", [])),
            },
        )
    session.announcements = announcements
