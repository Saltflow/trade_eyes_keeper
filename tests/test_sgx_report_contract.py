"""Offline routing and transport acceptance using explicit unit fixtures."""

from datetime import date
from types import SimpleNamespace

import pandas as pd

from src.data.announcement_fetcher import AnnouncementFetcher
from src.notification.report_builders import build_daily_event_rows
from src.research_pipeline.models import Instrument, ResearchSpec
from src.research_pipeline.sources import SourceService


def notice():
    return {
        "stock_code": "UNIT.SI",
        "exchange": "sgx",
        "title": "Financial Statements and Related Announcement::Full Yearly Results",
        "type": "Full Yearly Results",
        "date": "2026-09-28",
        "published_at": "2026-09-28T17:00:00+08:00",
        "url": "https://links.sgx.com/1.0.0/corporate-announcements/UNITTESTANNUAL",
        "document_id": "UNITTESTANNUAL",
    }


def test_strict_and_ordinary_announcements_share_sgx_and_keep_financial_category(
    monkeypatch,
):
    calls = []

    class Provider:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self, code, start, end, **kwargs):
            calls.append(code)
            return [notice()]

    monkeypatch.setattr("src.data.sgx_disclosures.SGXDisclosureProvider", Provider)
    fetcher = AnnouncementFetcher({})
    monkeypatch.setattr(fetcher, "_authority_http", lambda: None)
    strict = fetcher.fetch_announcements_status(
        ["UNIT.SI"], date(2026, 9, 1), date(2026, 11, 4)
    )
    assert strict["UNIT-SI"]["source"] == "sgx"
    assert strict["UNIT-SI"]["available"]
    notices = fetcher.get_recent_important_announcements(["UNIT.SI"], 90, 420)
    assert any(
        "Full Yearly" in row["title"] for rows in notices.values() for row in rows
    )
    events = build_daily_event_rows(
        {"UNIT.SI": [notice()]}, {}, pd.DataFrame(), as_of=date(2026, 11, 4)
    )
    assert events[0]["category_label"] == "财报业绩"
    assert events[0]["market"] == "sg"
    assert len(calls) == 2


def test_annual_research_uses_existing_discovery_and_download(tmp_path, monkeypatch):
    spec = ResearchSpec(
        ("sg:UNIT",),
        tmp_path / "run",
        repo=tmp_path,
        store_root=tmp_path / "store",
        start_year=2025,
        end_year=2026,
    )
    captured = {}

    class Provider:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def annuals(self, *args):
            return [
                {
                    **notice(),
                    "period_end": "2026-06-30",
                    "annual_attachment": {
                        "name": "Annual Report.pdf",
                        "url": "https://links.sgx.com/FileOpen/unit.pdf",
                    },
                    "source_record": {},
                    "source_document": {"sha256": "a" * 64},
                    "security_identity": {
                        "stock_code": "UNIT",
                        "isin_code": "UNITISIN",
                    },
                }
            ]

    monkeypatch.setattr("src.data.sgx_disclosures.SGXDisclosureProvider", Provider)
    source = SourceService(spec, SimpleNamespace(root=spec.store_root))
    docs = source.discover(Instrument.parse("sg:UNIT"), date(2026, 11, 4))
    assert len(docs) == 1
    assert docs[0].fiscal_year == 2026 and docs[0].period_end == "2026-06-30"
    assert docs[0].profiles == [] and docs[0].currency is None
    assert captured["hub"] is source.hub and captured["authority"] is source.authority
    downloads = []

    def download(url, target, *args, **kwargs):
        downloads.append(url)
        target.write_bytes(b"%PDF-offline-source-fixture")

    monkeypatch.setattr("src.data.dataset_documents.download_document", download)
    first = source.acquire(docs[0])
    again = source.acquire(docs[0])
    assert downloads == [docs[0].url]
    assert first.sha256 == again.sha256


def test_sgx_quote_source_leaves_unsupported_fundamentals_missing():
    from src.core.data_fetcher import StockDataFetcher

    fetcher = StockDataFetcher({"stocks": ["G3B.SI"]})
    assert all(
        value is None for value in fetcher._fetch_fundamental_data("G3B.SI").values()
    )
