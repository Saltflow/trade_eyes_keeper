"""Strict announcement discovery never turns incomplete source reads into absence."""

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from zoneinfo import ZoneInfo

from src.data.announcement_fetcher import AnnouncementFetcher, _AnnouncementBudgetHTTP
from src.data.us_announcements import SecAnnouncementProvider
from src.instruments.point_in_time import CninfoAnnualReportProvider
from src.triggers.facts import normalize_q3_event

NOW = datetime(2026, 10, 30, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
START, END = date(2026, 10, 29), date(2026, 10, 30)


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.content = json.dumps(payload, ensure_ascii=False).encode()

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def row(index=1, **changes):
    return {
        "secCode": "601088",
        "announcementTitle": "2026年第三季度报告",
        "announcementTime": int(
            datetime(2026, 10, 29, tzinfo=NOW.tzinfo).timestamp() * 1000
        ),
        "announcementId": str(index),
        "adjunctUrl": f"finalpage/2026-10-29/{index}.PDF",
        **changes,
    }


@pytest.fixture
def setup_source(monkeypatch):
    http = Mock()
    provider = CninfoAnnualReportProvider({}, http=http)
    provider._org_id = Mock(return_value="unit-org")
    monkeypatch.setattr(
        "src.instruments.point_in_time.CninfoAnnualReportProvider",
        lambda config: provider,
    )
    return AnnouncementFetcher({}), http, provider


def fetch(source):
    return source.fetch_announcements_status(["601088"], START, END, observed_at=NOW)[
        "601088"
    ]


def test_strict_source_uses_exact_identity_dates_and_preserves_raw_page_hash(
    setup_source,
):
    source, http, provider = setup_source
    http.post.return_value = Response(
        {
            "announcements": [row()],
            "totalpages": 1,
            "totalAnnouncement": 1,
            "hasMore": False,
        }
    )
    result = fetch(source)
    assert result["available"] is True
    assert result["coverage_start"] == START and result["coverage_end"] == END
    assert result["items"][0]["published_at"] == "2026-10-29T00:00:00+08:00"
    assert len(result["items"][0]["source_sha256"]) == 64
    provider._org_id.assert_called_once_with("601088")
    data = http.post.call_args.kwargs["data"]
    assert data["stock"] == "601088,unit-org"
    assert data["seDate"] == "2026-10-29~2026-10-30"
    assert source.llm_analyzer is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"announcements": None},
        {"announcements": []},
        {"announcements": [], "totalpages": 1},
        {"announcements": [], "totalpages": 2, "hasMore": False},
        {
            "announcements": [],
            "totalpages": 1,
            "hasMore": False,
            "totalAnnouncement": 1,
        },
        {"announcements": [row()], "totalpages": 99},
        {"announcements": [row()], "totalpages": 1, "hasMore": True},
        {"announcements": [row()], "totalpages": 1, "totalAnnouncement": 2},
        {"announcements": [row(secCode="600519")], "totalpages": 1},
        {"announcements": [row(announcementTime=None)], "totalpages": 1},
        {
            "announcements": [row(adjunctUrl="https://evil.example/wrong.pdf")],
            "totalpages": 1,
        },
    ],
)
def test_invalid_empty_schema_truncation_or_identity_is_unavailable(setup_source, body):
    source, http, _ = setup_source
    http.post.return_value = Response(body)
    result = fetch(source)
    assert result["available"] is False
    assert result["items"] == []
    assert result["error"]


@pytest.mark.parametrize("pages", [0, 1])
def test_explicit_successful_empty_page_is_available(setup_source, pages):
    source, http, _ = setup_source
    http.post.return_value = Response(
        {
            "announcements": [],
            "totalpages": pages,
            "totalAnnouncement": 0,
            "hasMore": False,
        }
    )
    assert fetch(source)["available"] is True


def test_official_zero_page_metadata_uses_exact_record_count(setup_source):
    # CNINFO live 601088 October-2025 response reports totalpages=0 for 13 rows.
    source, http, _ = setup_source
    http.post.return_value = Response(
        {
            "announcements": [row(index) for index in range(13)],
            "totalpages": 0,
            "totalAnnouncement": 13,
            "totalRecordNum": 13,
            "hasMore": False,
        }
    )
    assert len(fetch(source)["items"]) == 13


def test_official_null_empty_requires_explicit_zero_count_and_terminal_proof(
    setup_source,
):
    source, http, _ = setup_source
    http.post.return_value = Response(
        {
            "announcements": None,
            "totalpages": 0,
            "totalAnnouncement": 0,
            "totalRecordNum": 0,
            "hasMore": False,
        }
    )
    assert fetch(source)["available"] is True
    http.post.return_value = Response(
        {
            "announcements": None,
            "totalpages": 0,
            "totalAnnouncement": 1,
            "hasMore": False,
        }
    )
    assert fetch(source)["available"] is False


def test_http_failure_is_unavailable_without_legacy_list_fallback(setup_source):
    source, http, _ = setup_source
    http.post.side_effect = requests.HTTPError("503 service unavailable")
    source.fetch_announcements = Mock(
        side_effect=AssertionError("legacy swallowing fallback")
    )
    result = fetch(source)
    assert result["available"] is False
    assert "503" in result["error"]
    source.fetch_announcements.assert_not_called()


def test_all_pages_required_and_later_failure_clears_partial_items(setup_source):
    source, http, _ = setup_source
    http.post.side_effect = [
        Response({"announcements": [row()], "totalpages": 2}),
        requests.Timeout("second page timeout"),
    ]
    result = fetch(source)
    assert result["available"] is False and result["items"] == []
    assert "second page" in result["error"]


def test_complete_pagination_and_repeated_or_changing_pages(setup_source):
    source, http, _ = setup_source
    http.post.side_effect = [
        Response({"announcements": [row()], "totalpages": 2}),
        Response({"announcements": [row(2)], "totalpages": 2}),
    ]
    assert len(fetch(source)["items"]) == 2
    http.post.side_effect = [
        Response({"announcements": [row()], "totalpages": 2}),
        Response({"announcements": [row()], "totalpages": 2}),
    ]
    assert fetch(source)["available"] is False
    http.post.side_effect = [
        Response({"announcements": [row()], "totalpages": 2}),
        Response({"announcements": [row(2)], "totalpages": 1}),
    ]
    assert fetch(source)["available"] is False


def test_hk_and_us_official_strict_providers_keep_failures_distinct():
    source = AnnouncementFetcher({})
    source._hk_announcements = Mock()
    source._hk_announcements.fetch.return_value = []
    source._us_announcements = Mock()
    source._us_announcements.fetch.side_effect = ValueError(
        "SEC incomplete archive coverage"
    )
    result = source.fetch_announcements_status(
        ["00700", "AAPL"], START, END, observed_at=NOW
    )
    assert result["00700"]["available"] is True
    assert result["00700"]["items"] == []
    assert result["AAPL"]["available"] is False
    assert "coverage" in result["AAPL"]["error"]


def test_source_failure_is_per_security_not_global(setup_source):
    source, http, provider = setup_source
    provider._org_id.side_effect = [ValueError("identity unavailable"), "second-org"]
    http.post.return_value = Response(
        {"announcements": [], "totalpages": 0, "hasMore": False}
    )
    result = source.fetch_announcements_status(
        ["601088", "159655"], START, END, observed_at=NOW
    )
    assert not result["601088"]["available"]
    assert result["159655"]["available"]


def test_sec_report_period_is_preserved_from_primary_columns_not_filing_month():
    provider = SecAnnouncementProvider({}, http=Mock())
    columns = {
        "accessionNumber": ["0001728451-26-000005"],
        "filingDate": ["2026-10-29"],
        "form": ["10-Q"],
        "primaryDocument": ["quarter.htm"],
        "reportDate": ["2026-09-30"],
    }
    rows = provider._company_rows(columns, "UNIT", "0001728451", START, END)
    assert rows[0]["reportDate"] == "2026-09-30"
    columns.pop("reportDate")
    assert (
        "reportDate"
        not in provider._company_rows(columns, "UNIT", "0001728451", START, END)[0]
    )


@pytest.mark.parametrize("dates", [[], ["not-a-date"], ["2026-10-31"]])
def test_invalid_sec_report_dates_are_unavailable(dates):
    provider = SecAnnouncementProvider({}, http=Mock())
    columns = {
        "accessionNumber": ["0001728451-26-000005"],
        "filingDate": ["2026-10-29"],
        "form": ["10-Q"],
        "primaryDocument": ["quarter.htm"],
        "reportDate": dates,
    }
    with pytest.raises(ValueError):
        provider._company_rows(columns, "UNIT", "0001728451", START, END)


def test_real_cninfo_q3_response_fixture_and_provenance(setup_source):
    source, http, _ = setup_source
    root = Path(__file__).parent / "fixtures" / "announcements"
    raw = (root / "cninfo_601088_202510_response.json").read_bytes()
    meta = json.loads(
        (root / "cninfo_601088_202510_response.source.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(raw).hexdigest() == meta["response_sha256"]
    response = Response(json.loads(raw))
    response.content = raw
    http.post.return_value = response
    result = source.fetch_announcements_status(
        ["601088"],
        date(2025, 10, 1),
        date(2025, 10, 31),
        observed_at=NOW,
    )["601088"]
    assert result["available"] is True and len(result["items"]) == 13
    events = [
        event
        for item in result["items"]
        if (event := normalize_q3_event(item, "601088", observed_at=NOW))
    ]
    assert len(events) == 1
    assert events[0].report_period == "2025Q3"
    assert events[0].publication_date == date(2025, 10, 25)
    assert events[0].source_sha256 == meta["response_sha256"]


def test_cancelled_strict_source_does_not_resolve_identity_or_request_http(
    setup_source,
):
    source, http, provider = setup_source
    result = source.fetch_announcements_status(
        ["601088", "00700", "VOO"],
        START,
        END,
        observed_at=NOW,
        cancelled=lambda: True,
    )
    provider._org_id.assert_not_called()
    http.post.assert_not_called()
    assert all(
        not item["available"] and "cancelled" in item["error"]
        for item in result.values()
    )


def test_budget_exhaustion_after_complete_code_preserves_success_and_skips_remaining(
    setup_source, monkeypatch
):
    source, http, provider = setup_source
    elapsed = [0.0]
    monkeypatch.setattr(
        "src.data.announcement_fetcher.time.monotonic", lambda: elapsed[0]
    )

    def complete_first(*args, **kwargs):
        assert kwargs["timeout"] <= 1
        elapsed[0] = 2.0
        return Response({"announcements": [row()], "totalpages": 1})

    http.post.side_effect = complete_first
    result = source.fetch_announcements_status(
        ["601088", "159655"], START, END, observed_at=NOW, deadline=1
    )
    assert result["601088"]["available"] and len(result["601088"]["items"]) == 1
    assert not result["159655"]["available"]
    assert "budget exhausted" in result["159655"]["error"]
    provider._org_id.assert_called_once_with("601088")
    assert http.post.call_count == 1


def test_budget_exhaustion_between_pages_does_not_claim_complete_window(
    setup_source, monkeypatch
):
    source, http, _ = setup_source
    elapsed = [0.0]
    monkeypatch.setattr(
        "src.data.announcement_fetcher.time.monotonic", lambda: elapsed[0]
    )

    def first_page(*args, **kwargs):
        elapsed[0] = 2.0
        return Response({"announcements": [row()], "totalpages": 2})

    http.post.side_effect = first_page
    result = source.fetch_announcements_status(
        ["601088"], START, END, observed_at=NOW, deadline=1
    )["601088"]
    assert not result["available"] and result["items"] == []
    assert http.post.call_count == 1


def test_http_budget_proxy_caps_remaining_timeout_and_never_mutates_shared_http(
    monkeypatch,
):
    monkeypatch.setattr("src.data.announcement_fetcher.time.monotonic", lambda: 2.5)
    http = Mock()
    bounded = _AnnouncementBudgetHTTP(http, None, 3.0)
    bounded.post("https://official.example/test", timeout=45)
    assert http.post.call_args.kwargs["timeout"] == 0.5
    bounded.get("https://official.example/test", timeout=(5, 10))
    assert http.get.call_args.kwargs["timeout"] == (0.5, 0.5)
    assert http.get is not bounded.get


def test_completed_positive_event_survives_other_symbol_budget_exhaustion(
    setup_source, monkeypatch
):
    from src.triggers.facts import load_trigger_facts

    source, http, _ = setup_source
    elapsed = [0.0]
    monkeypatch.setattr(
        "src.data.announcement_fetcher.time.monotonic", lambda: elapsed[0]
    )
    monkeypatch.setattr(
        "src.data.announcement_fetcher.AnnouncementFetcher", lambda config: source
    )

    def first(*args, **kwargs):
        elapsed[0] = 2.0
        return Response({"announcements": [row()], "totalpages": 1})

    http.post.side_effect = first
    snapshot = load_trigger_facts(
        {"events.announcement.q3_report"},
        {"stocks": ["601088", "159655"]},
        as_of=NOW,
        budget_seconds=1,
    )
    assert len(snapshot.events) == 1 and snapshot.events[0].symbol == "601088"
    assert snapshot.resolve("events.announcement.q3_report").status == "unknown"
    assert snapshot.resolve("announcements.159655.coverage").status == "unknown"
