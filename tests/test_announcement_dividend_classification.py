from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

from src.data import announcement_fetcher
from src.data.announcement_fetcher import AnnouncementFetcher
from src.data.dividend_events import DIVIDEND_PENDING, DailyDividendEventResolver


@pytest.fixture(autouse=True)
def report_clock(monkeypatch):
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 21, tzinfo=timezone.utc)
    monkeypatch.setattr(announcement_fetcher, "datetime", clock)


@pytest.fixture
def implementation():
    return {
        "stock_code": "601088",
        "title": "中国神华：2026年半年度权益分派实施公告",
        "date": "2026-10-16",
        "url": "https://example.test/announcement",
    }


def test_implementation_retained_beyond_regular_announcement_window(implementation):
    fetcher = AnnouncementFetcher({})
    ordinary = {**implementation, "title": "关于董事离任的公告"}

    assert fetcher._filter_announcements_by_window(
        [implementation, ordinary], original_days=3, dividend_days=420
    ) == [implementation]


def test_implementation_does_not_bypass_dividend_window(implementation):
    fetcher = AnnouncementFetcher({})
    implementation["date"] = (date(2026, 10, 21) - timedelta(days=421)).isoformat()

    assert fetcher._filter_announcements_by_window([implementation], 3, 420) == []


def test_implementation_survives_important_announcement_selection(
    implementation, monkeypatch
):
    fetcher = AnnouncementFetcher({})
    old_plan = {**implementation, "title": "中国神华：中期利润分配方案公告"}
    monkeypatch.setattr(
        fetcher, "_fetch_from_sse_backup", lambda *args: [implementation, old_plan]
    )

    assert fetcher.get_recent_important_announcements(["601088"], 7, 420) == {
        "601088": [implementation, old_plan]
    }


def test_implementation_enriched_into_pending_dividend(implementation, tmp_path):
    fetcher = AnnouncementFetcher({})
    fetcher.enable_content_fetching = True
    fetcher.enable_llm_extraction = True
    fetcher.content_fetcher = Mock()
    fetcher.content_fetcher.fetch_content.return_value = {
        "success": True,
        "extracted_text": "A股 股权登记日2026/10/23 除息日2026/10/26 派息日2026/10/26",
        "content_hash": "fixture",
    }
    fetcher.llm_analyzer = Mock()
    fetcher.llm_analyzer.extract_dividend_details_from_announcement.return_value = {
        "success": True,
        "cash_dividend_per_share": 0.98,
        "ex_rights_date": "2026-10-26",
        "payment_date": "2026-10-26",
    }

    enriched = fetcher._enrich_announcements([implementation])
    fetcher.content_fetcher.fetch_content.assert_called_once_with(
        implementation["url"], "601088", implementation["date"]
    )
    fetcher.llm_analyzer.extract_dividend_details_from_announcement.assert_called_once()
    resolver = DailyDividendEventResolver(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    )
    events = resolver.resolve(
        {"601088": enriched},
        pd.DataFrame([{"stock_code": "601088"}]),
        as_of=date(2026, 10, 21),
    )
    assert len(events) == 1
    assert events[0]["status"] == DIVIDEND_PENDING
    assert events[0]["ex_date"] == events[0]["payment_date"] == "2026-10-26"
    assert events[0]["cash_per_share"] == 0.98
