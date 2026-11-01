"""Explicit evidence can mark inapplicability; absent evidence never can."""

from datetime import date
from types import SimpleNamespace

import pytest

from src.research_pipeline.applicability import declared_applicability
from src.research_pipeline.common import atomic_write, fingerprint
from src.research_pipeline.models import Instrument


def declared(
    tmp_path, *, instrument="a_share:600348", status="prelisting", year=2019, **fields
):
    record = {
        "instrument": instrument,
        "record_id": "caller-supplied-official-record",
        **fields,
    }
    domain = {
        "a_share": "www.sse.com.cn",
        "hk": "www.hkexnews.hk",
        "us": "www.sec.gov",
    }[instrument.split(":")[0]]
    row = {
        "instrument": instrument,
        "fiscal_year": year,
        "status": status,
        "evidence": {
            "source_url": "https://" + domain + "/record",
            "record": record,
            "sha256": fingerprint(record),
        },
        **fields,
    }
    path = tmp_path / "source_manifest.json"
    atomic_write(path, {"documents": [], "applicability": [row]})
    return SimpleNamespace(source_manifest=path), row


def test_prelisting_preserves_supplied_evidence_and_does_not_mutate(tmp_path):
    spec, row = declared(tmp_path, listing_date="2020-06-01")
    before = spec.source_manifest.read_bytes()
    result = declared_applicability(
        spec, Instrument.parse("a_share:600348"), 2019, date(2026, 11, 1)
    )
    assert result["status"] == "prelisting" and result["evidence"] == row["evidence"]
    assert result["basis"] == "caller_supplied_official_record_not_refetched"
    result["evidence"]["record"]["record_id"] = "consumer mutation"
    assert spec.source_manifest.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    ["missing_proof", "tampered_hash", "unofficial_url", "wrong_record_issuer"],
)
def test_missing_or_tampered_proof_remains_unresolved(tmp_path, change):
    spec, row = declared(tmp_path, listing_date="2020-06-01")
    if change == "missing_proof":
        row.pop("evidence")
    elif change == "tampered_hash":
        row["evidence"]["record"]["record_id"] = "changed"
    elif change == "unofficial_url":
        row["evidence"]["source_url"] = "https://www.sse.com.cn.example.org/record"
    else:
        row["evidence"]["record"]["instrument"] = "a_share:600546"
        row["evidence"]["sha256"] = fingerprint(row["evidence"]["record"])
    atomic_write(spec.source_manifest, {"applicability": [row]})
    assert (
        declared_applicability(spec, "a_share:600348", 2019, date(2026, 11, 1)) is None
    )


@pytest.mark.parametrize("listing_date", ["2019-01-01", "2018-12-31", "bad date"])
def test_same_year_listing_or_inconsistent_date_cannot_exclude_a_year(
    tmp_path, listing_date
):
    spec, _ = declared(tmp_path, listing_date=listing_date)
    assert (
        declared_applicability(spec, "a_share:600348", 2019, date(2026, 11, 1)) is None
    )


def test_hk_noncalendar_period_not_yet_due_uses_exact_period_end(tmp_path):
    spec, row = declared(
        tmp_path,
        instrument="hk:00883",
        status="not_yet_due",
        year=2026,
        period_end="2026-09-30",
    )
    result = declared_applicability(spec, "hk:00883", 2026, date(2026, 8, 31))
    assert result["period_end"] == row["period_end"]
    assert declared_applicability(spec, "hk:00883", 2026, date(2026, 9, 30)) is None
    assert declared_applicability(spec, "hk:00883", 2025, date(2025, 8, 31)) is None


def test_unrelated_issuer_or_missing_manifest_does_not_create_not_applicable(tmp_path):
    spec, _ = declared(tmp_path, listing_date="2020-06-01")
    assert (
        declared_applicability(spec, "a_share:600546", 2019, date(2026, 11, 1)) is None
    )
    spec.source_manifest = tmp_path / "missing.json"
    assert (
        declared_applicability(spec, "a_share:600348", 2019, date(2026, 11, 1)) is None
    )


def test_explicit_official_filing_deadline_preserves_post_year_end_window(tmp_path):
    spec, _ = declared(
        tmp_path,
        instrument="us:XOM",
        status="not_yet_due",
        year=2026,
        period_end="2026-12-31",
        filing_due_date="2027-03-01",
    )
    assert declared_applicability(spec, "us:XOM", 2026, date(2027, 2, 1))
    assert declared_applicability(spec, "us:XOM", 2026, date(2027, 3, 1)) is None


def test_unbound_filing_deadline_cannot_hide_missing_report(tmp_path):
    spec, row = declared(
        tmp_path,
        instrument="us:XOM",
        status="not_yet_due",
        year=2026,
        period_end="2026-12-31",
    )
    row["filing_due_date"] = "2027-03-01"
    atomic_write(spec.source_manifest, {"applicability": [row]})
    assert declared_applicability(spec, "us:XOM", 2026, date(2027, 2, 1)) is None
    spec.source_manifest = None
    assert (
        declared_applicability(spec, "a_share:600348", 2019, date(2026, 11, 1)) is None
    )
