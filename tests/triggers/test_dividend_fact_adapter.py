"""The dividend fact source is the same public view consumed by the Bot."""

from datetime import datetime
from unittest.mock import Mock

import pytest
from zoneinfo import ZoneInfo

from src.data.announcement_fetcher import AnnouncementFetcher
from src.triggers.facts import TriggerFactProviders, load_trigger_facts


@pytest.fixture(autouse=True)
def offline_announcements(monkeypatch):
    """Dividend assertions do not exercise the accompanying disclosure fetch."""
    blocked = Mock(side_effect=AssertionError("external request forbidden"))
    for target in (
        "requests.sessions.Session.request",
        "socket.create_connection",
        "socket.socket.connect",
    ):
        monkeypatch.setattr(target, blocked)

    def empty_window(codes, start, end, **kwargs):
        return {
            code: {
                "available": True,
                "items": [],
                "coverage_start": start,
                "coverage_end": end,
                "source": "controlled-empty-disclosures",
            }
            for code in codes
        }

    announcements = Mock(side_effect=empty_window)
    monkeypatch.setattr(
        AnnouncementFetcher, "fetch_announcements_status", announcements
    )
    yield announcements
    # A caught transport error must not let these tests pass by downgrading an
    # unrelated dependency to unknown instead of keeping the test fully offline.
    blocked.assert_not_called()


def test_dividend_fact_reuses_public_schedule_and_surfaces_failure():
    now = datetime(2026, 11, 6, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = "events.dividend.next_payment"
    schedule = {
        "events": [{"code": "01339", "payment_date": "2026-11-06"}],
        "monitored_symbols": ["01339"],
        "status": "partial",
    }
    source = Mock(return_value=schedule)
    config = {"stocks": [{"code": "01339"}]}
    providers = TriggerFactProviders(dividends=source)
    snapshot = load_trigger_facts({path}, config, as_of=now, providers=providers)
    fact = snapshot.resolve(path)
    assert snapshot.resolve("announcements.01339.coverage").status == "available"
    source.assert_called_once_with(["01339"], now)
    assert fact.value == schedule
    assert fact.status == "partial"
    source.side_effect = OSError("offline")
    fact = load_trigger_facts({path}, config, as_of=now, providers=providers).resolve(
        path
    )
    assert fact.status == "unknown"
    assert fact.error == "offline"


def test_default_dividend_source_does_not_consume_pending_display(monkeypatch):
    from src.data.dividend_events import DailyDividendEventResolver

    now = datetime(2026, 11, 6, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = Mock(return_value={"events": [], "status": "available"})
    monkeypatch.setattr(DailyDividendEventResolver, "public_schedule", source)
    path = "events.dividend.next_payment"
    snapshot = load_trigger_facts({path}, {"stocks": ["01339"]}, as_of=now)
    fact = snapshot.resolve(path)
    assert snapshot.resolve("announcements.01339.coverage").status == "available"
    assert fact.status == "available"
    assert source.call_args.kwargs["as_of"] == now.date()
    assert source.call_args.args[1]["stock_code"].tolist() == ["01339"]
