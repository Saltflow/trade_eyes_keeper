"""SEC identity mapping, class-specific fund filings and report routing."""

from datetime import date, datetime, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from src.data import announcement_fetcher
from src.data.announcement_fetcher import AnnouncementFetcher
from src.data.us_announcements import SecAnnouncementProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "trade-eyes-test test@example.com")
    monkeypatch.setattr("src.data.us_announcements.time.sleep", lambda *_: None)
    return SecAnnouncementProvider(http=Mock())


def columns(
    day="2026-10-23", form="8-K", doc="report.htm", accession="0001728451-26-000005"
):
    return {
        "accessionNumber": [accession],
        "filingDate": [day],
        "form": [form],
        "primaryDocument": [doc],
    }


def company(recent=None, archives=None):
    return {
        "cik": 1067983,
        "tickers": ["BRK-B", "BRK-A"],
        "filings": {"recent": recent or columns(), "files": archives or []},
    }


def atom(day="2026-10-23", form="NPORT-P", cik="0000036405", url=None, count=1):
    url = (
        url
        or "https://www.sec.gov/Archives/edgar/data/36405/000003640526000473/filing-index.htm"
    )
    entry = (
        f"<entry><filing-date>{day}</filing-date><filing-type>{form}</filing-type>"
        f"<filing-href>{url}</filing-href></entry>"
    )
    return (
        f'<feed xmlns="http://www.w3.org/2005/Atom"><company-info><cik>{cik}</cik>'
        f"</company-info>{entry * count}</feed>"
    ).encode()


def test_stock_class_symbol_maps_to_sec_identity(provider):
    provider.provider.http.get.side_effect = [
        Mock(**{"json.return_value": {"0": {"ticker": "BRK-B", "cik_str": 1067983}}}),
        Mock(**{"json.return_value": company()}),
    ]
    rows = provider.fetch("BRK.B", date(2026, 10, 23), date(2026, 10, 30))
    assert rows[0]["stock_code"] == "BRK.B" and rows[0]["type"] == "8-K"
    assert "重大事项报告" in rows[0]["title"]
    assert (
        rows[0]["url"]
        == "https://www.sec.gov/Archives/edgar/data/1067983/000172845126000005/report.htm"
    )
    assert (
        "test@example.com"
        in provider.provider.http.get.call_args.kwargs["headers"]["User-Agent"]
    )


def test_overlapping_archives_are_loaded_and_deduplicated(provider):
    provider.provider._tickers = {"BRK-B": "0001067983"}
    older = columns()
    archives = [
        {
            "name": "CIK0001067983-submissions-001.json",
            "filingFrom": "2025-01-01",
            "filingTo": "2026-10-23",
        },
        {
            "name": "CIK0001067983-submissions-002.json",
            "filingFrom": "2020-01-01",
            "filingTo": "2020-12-31",
        },
    ]
    provider.provider.http.get.side_effect = [
        Mock(**{"json.return_value": company(archives=archives)}),
        Mock(**{"json.return_value": older}),
    ]
    # Same document can appear in both blocks; keep one.
    rows = provider.fetch("BRK.B", date(2025, 9, 5), date(2026, 10, 30))
    assert len(rows) == 1
    assert provider.provider.http.get.call_count == 2


@pytest.mark.parametrize(
    "change",
    ["identity", "columns", "date", "path", "accession", "descriptions", "archive"],
)
def test_malformed_company_evidence_is_rejected(provider, change):
    provider.provider._tickers = {"BRK-B": "0001067983"}
    payload = company()
    recent = payload["filings"]["recent"]
    if change == "identity":
        payload["cik"] = 123
    elif change == "columns":
        recent["form"] = []
    elif change == "date":
        recent["filingDate"] = ["invalid"]
    elif change == "path":
        recent["primaryDocument"] = ["../wrong.htm"]
    elif change == "accession":
        recent["accessionNumber"] = ["not-an-accession"]
    elif change == "descriptions":
        recent["primaryDocDescription"] = []
    else:
        payload["filings"]["files"] = [
            {
                "name": "../../wrong.json",
                "filingFrom": "2026-01-01",
                "filingTo": "2026-10-30",
            }
        ]
    provider.provider.http.get.return_value.json.return_value = payload
    with pytest.raises(ValueError):
        provider.fetch("BRK.B", date(2025, 9, 5), date(2026, 10, 30))


def test_fund_uses_exact_share_class_instead_of_parent_trust(provider):
    provider.provider.http.get.side_effect = [
        Mock(**{"json.return_value": {}}),
        Mock(
            **{
                "json.return_value": {
                    "fields": ["cik", "seriesId", "classId", "symbol"],
                    "data": [[36405, "S000002839", "C000092055", "VOO"]],
                }
            }
        ),
        Mock(content=atom()),
    ]
    rows = provider.fetch("VOO", date(2026, 10, 23), date(2026, 10, 30))
    assert rows[0]["type"] == "NPORT-P" and rows[0]["stock_code"] == "VOO"
    url = provider.provider.http.get.call_args.args[0]
    assert parse_qs(urlsplit(url).query)["CIK"] == ["C000092055"]
    assert "/submissions/" not in url


def test_fund_pages_until_window_is_covered_and_keeps_empty_truthful(provider):
    provider.provider._tickers = {}
    provider._funds = {"VOO": ("0000036405", "C000092055")}
    provider.provider.http.get.side_effect = [
        Mock(content=atom(count=100)),
        Mock(content=atom("2025-01-01")),
    ]
    assert len(provider.fetch("VOO", date(2025, 9, 5), date(2026, 10, 30))) == 1
    query = parse_qs(urlsplit(provider.provider.http.get.call_args.args[0]).query)
    assert query["start"] == ["100"]
    provider.provider.http.get.side_effect = None
    provider.provider.http.get.return_value.content = atom("2026-08-28")
    assert provider.fetch("VOO", date(2026, 10, 23), date(2026, 10, 30)) == []


@pytest.mark.parametrize(
    "feed",
    [atom(cik="999"), atom(url="https://evil.test/wrong"), b"<html>blocked</html>"],
)
def test_fund_identity_and_link_checks(provider, feed):
    provider.provider._tickers = {}
    provider._funds = {"VOO": ("0000036405", "C000092055")}
    provider.provider.http.get.return_value.content = feed
    with pytest.raises(ValueError):
        provider.fetch("VOO", date(2026, 10, 23), date(2026, 10, 30))


def test_fund_query_budget(provider):
    provider.provider._tickers = {}
    provider._funds = {"VOO": ("0000036405", "C000092055")}
    provider.MAX_FUND_PAGES = 1
    provider.provider.http.get.return_value.content = atom(count=100)
    with pytest.raises(ValueError, match="limit exceeded"):
        provider.fetch("VOO", date(2025, 9, 5), date(2026, 10, 30))


def test_sec_credentials_are_required_without_fabrication(provider, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT")
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    provider.provider.sec_user_agent = ""
    with pytest.raises(RuntimeError, match="User-Agent unavailable"):
        provider.fetch("BRK.B", date(2026, 10, 23), date(2026, 10, 30))
    provider.provider.http.get.assert_not_called()


def test_us_codes_are_retained_routed_and_selected_without_hk_llm(monkeypatch):
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(announcement_fetcher, "datetime", clock)
    fetcher = AnnouncementFetcher({})
    provider = Mock()
    item = {
        "stock_code": "BRK.B",
        "exchange": "sec",
        "title": "8-K · 重大事项报告",
        "type": "8-K",
        "date": "2026-10-23",
        "url": "https://www.sec.gov/Archives/test.htm",
    }
    provider.fetch.side_effect = [[], [item]]
    monkeypatch.setattr(
        "src.data.us_announcements.SecAnnouncementProvider", Mock(return_value=provider)
    )
    monkeypatch.setattr(
        fetcher,
        "_enrich_announcements",
        Mock(side_effect=AssertionError("unexpected LLM")),
    )
    assert fetcher.get_recent_important_announcements(["VOO", "BRK.B"], 7, 420) == {
        "VOO": [],
        "BRK.B": [item],
    }
    provider.fetch.side_effect = requests.Timeout("SEC unavailable")
    assert fetcher.fetch_announcements(["VOO"]) == {"VOO": []}
