"""Retain explicitly supplied official applicability evidence, without fetching.

Missing evidence remains unknown. An earliest available report or quote is never
listing proof, and this adapter does not infer a filing deadline.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .common import fingerprint, load
from .models import Instrument

_OFFICIAL_DOMAINS = {
    "a_share": ("cninfo.com.cn", "sse.com.cn", "szse.cn"),
    "hk": ("hkexnews.hk", "hkex.com.hk"),
    "us": ("sec.gov",),
    "sg": ("sgx.com",),
}


def _official_url(value, market):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and any(
            host == domain or host.endswith("." + domain)
            for domain in _OFFICIAL_DOMAINS[market]
        )
    )


def _record_identity_matches(record, instrument):
    declared = record.get("instrument")
    if declared is not None and Instrument.parse(declared).key != instrument.key:
        return False
    code = record.get("code") or record.get("stock_code") or record.get("secCode")
    if code is not None:
        normalized = str(code).zfill(5) if instrument.market == "hk" else str(code)
        if normalized.upper() != instrument.code.upper():
            return False
    return True


def declared_applicability(spec, instrument, year: int, as_of: date) -> dict | None:
    """Return one consistent verified declaration; invalid/ambiguous stays None.

    Evidence is caller-supplied, hash-bound official metadata, not independently
    refetched or validated as an issuer's actual legal listing/filing obligation.
    Existing source documents must take precedence in the calling orchestrator.
    """
    try:
        if (
            not spec.source_manifest
            or isinstance(year, bool)
            or not isinstance(year, int)
        ):
            return None
        instrument = (
            instrument
            if isinstance(instrument, Instrument)
            else Instrument.parse(instrument)
        )
        if not isinstance(as_of, date):
            return None
        manifest = load(Path(spec.source_manifest))
        rows = manifest.get("applicability", []) if isinstance(manifest, dict) else []
        if not isinstance(rows, list):
            return None
        accepted = []
        for row in rows:
            if not isinstance(row, dict) or row.get("instrument") != instrument.key:
                continue
            if (
                isinstance(row.get("fiscal_year"), bool)
                or row.get("fiscal_year") != year
            ):
                continue
            status = row.get("status")
            if status not in {"prelisting", "not_yet_due"}:
                continue
            evidence = row.get("evidence")
            if not isinstance(evidence, dict) or not isinstance(
                evidence.get("record"), dict
            ):
                continue
            record = evidence["record"]
            if (
                not _official_url(evidence.get("source_url"), instrument.market)
                or evidence.get("sha256") != fingerprint(record)
                or not _record_identity_matches(record, instrument)
            ):
                continue
            if status == "prelisting":
                if record.get("listing_date") != row.get("listing_date"):
                    continue
                listed = date.fromisoformat(row["listing_date"])
                if year >= listed.year:
                    continue
            else:
                if record.get("period_end") != row.get("period_end"):
                    continue
                end = date.fromisoformat(row["period_end"])
                due = end
                if row.get("filing_due_date"):
                    if record.get("filing_due_date") != row["filing_due_date"]:
                        continue
                    due = date.fromisoformat(row["filing_due_date"])
                if end.year != year or due < end or as_of >= due:
                    continue
            accepted.append(copy.deepcopy(row))
        if not accepted or len({fingerprint(row) for row in accepted}) != 1:
            return None
        return {
            **accepted[0],
            "basis": "caller_supplied_official_record_not_refetched",
            "evaluated_as_of": as_of.isoformat(),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
