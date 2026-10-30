"""Build pending independent audit tasks without inventing audit outcomes."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

from .contracts import fingerprint

PENDING = "pending_independent_audit"


def _event_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone(timedelta(hours=8)))
        return timestamp.date()
    except ValueError:
        return None


def _references(value: object, field: str) -> list:
    """Keep references from every finding, adjustment, and review subsection."""
    result = []
    if isinstance(value, dict):
        refs = value.get(field)
        if isinstance(refs, list):
            result.extend(copy.deepcopy(refs))
        for key, child in value.items():
            if key != field:
                result.extend(_references(child, field))
    elif isinstance(value, list):
        for child in value:
            result.extend(_references(child, field))
    return result


def _review_failed(event: dict) -> bool:
    if event.get("kind") == "correction_findings":
        return True
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    blockers = payload.get("blockers") or payload.get("errors") or []
    if not isinstance(blockers, list):
        return False
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        code = str(blocker.get("code", ""))
        if blocker.get("stage") == "review" or code.startswith(
            (
                "review_",
                "invalid_review_",
                "unexpected_review_",
                "incomplete_independent_review",
            )
        ):
            return True
    return False


def build_audit_queue(
    companies: list[dict],
    review_events: list[dict],
    as_of: date | str,
    sample_count: int = 5,
) -> list[dict]:
    """Queue all adjustments/disagreements and a stable daily clean sample.

    Input events are supplied by the caller; this pure function does not infer
    whether an earlier finding was resolved. The persistence layer must preserve
    actual human/independent-review outcomes separately. All returned rows are
    pending, even if an LLM embedded a 'completed' claim inside its output.
    """
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("sample_count must be a nonnegative integer")
    day = as_of if type(as_of) is date else date.fromisoformat(str(as_of))
    if not isinstance(companies, list) or not isinstance(review_events, list):
        raise TypeError("companies and review_events must be lists")
    rows, publications, known_sources = {}, {}, {}

    def ensure(issuer: str, company: dict | None = None) -> dict:
        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError("audit company id must be a nonempty string")
        if issuer not in rows:
            rows[issuer] = {
                "id": "audit:"
                + fingerprint({"day": day.isoformat(), "issuer": issuer})[:24],
                "as_of": day.isoformat(),
                "company_id": issuer,
                "code": None,
                "name": None,
                "status": PENDING,
                "reasons": [],
                "version_refs": [],
                "source_refs": [],
                "fact_refs": [],
                "event_refs": [],
            }
        row = rows[issuer]
        if company:
            row["code"] = company.get("code") or row["code"]
            row["name"] = company.get("name") or row["name"]
        return row

    def append_unique(row: dict, key: str, values: list) -> None:
        known = {fingerprint(value) for value in row[key]}
        for value in values:
            digest = fingerprint(value)
            if digest not in known:
                row[key].append(copy.deepcopy(value))
                known.add(digest)

    def attach(row: dict, payload: dict) -> None:
        version = payload.get("version")
        if isinstance(version, str) and version:
            append_unique(row, "version_refs", [version])
        append_unique(
            row,
            "fact_refs",
            [r for r in _references(payload, "fact_refs") if isinstance(r, str)],
        )
        for ref in _references(payload, "source_refs"):
            sid = ref.get("id") if isinstance(ref, dict) else ref
            if not isinstance(sid, str) or not sid:
                continue
            metadata = known_sources.get((row["company_id"], sid), [])
            if isinstance(ref, dict):
                metadata = [
                    {
                        key: ref[key]
                        for key in ("id", "url", "page", "hash", "origin_id")
                        if key in ref
                    }
                ]
            append_unique(
                row,
                "source_refs",
                metadata or [{"id": sid, "resolution": "not_in_current_publication"}],
            )

    # Index first so duplicates and event-only failures can retain source metadata.
    for company in companies:
        if not isinstance(company, dict):
            raise TypeError("audit company must be an object")
        identity = (
            company.get("company")
            if isinstance(company.get("company"), dict)
            else company
        )
        issuer = identity.get("id") or company.get("id")
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("audit company id missing")
        publication = company.get("publication")
        if not isinstance(publication, dict):
            continue
        for source in publication.get("source_refs", []):
            if isinstance(source, dict) and isinstance(source.get("id"), str):
                metadata = {
                    key: source[key]
                    for key in ("id", "url", "page", "hash", "origin_id")
                    if key in source
                }
                variants = known_sources.setdefault((issuer, source["id"]), [])
                if metadata not in variants:
                    variants.append(metadata)
        if publication.get("status") != "published":
            continue
        if (
            not isinstance(publication.get("version"), str)
            or not publication["version"]
        ):
            raise ValueError("published audit input requires a version")
        publications.setdefault(issuer, []).append((identity, company, publication))
        if publication.get("adjustments"):
            row = ensure(issuer, identity)
            append_unique(row, "reasons", ["normalization_adjustment"])
            attach(row, publication)

    for event in review_events:
        if not isinstance(event, dict):
            raise TypeError("audit event must be an object")
        event_day = _event_date(event.get("created_at"))
        if event_day is not None and event_day > day:
            continue
        if not _review_failed(event):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise TypeError("audit event payload must be an object")
        issuer = event.get("company_id") or payload.get("issuer_id")
        row = ensure(issuer)
        append_unique(row, "reasons", ["review_disagreement"])
        attach(row, payload)
        append_unique(
            row,
            "event_refs",
            [
                {
                    "id": event.get("id"),
                    "kind": event.get("kind"),
                    "created_at": event.get("created_at"),
                }
            ],
        )
        for identity, _, publication in publications.get(issuer, []):
            ensure(issuer, identity)
            attach(row, publication)

    eligible = []
    for issuer, versions in publications.items():
        if issuer in rows or any(
            company.get("errors") or publication.get("issues")
            for _, company, publication in versions
        ):
            continue
        version_ids = sorted({publication["version"] for _, _, publication in versions})
        score = fingerprint(
            {"day": day.isoformat(), "issuer": issuer, "versions": version_ids}
        )
        eligible.append((score, issuer))
    for _, issuer in sorted(eligible)[:sample_count]:
        row = ensure(issuer)
        append_unique(row, "reasons", ["random_published_sample"])
        for identity, _, publication in publications[issuer]:
            ensure(issuer, identity)
            attach(row, publication)

    for row in rows.values():
        for field in (
            "reasons",
            "version_refs",
            "source_refs",
            "fact_refs",
            "event_refs",
        ):
            row[field].sort(key=fingerprint)
    return sorted(
        rows.values(),
        key=lambda row: (
            "random_published_sample" in row["reasons"],
            row["company_id"],
        ),
    )
