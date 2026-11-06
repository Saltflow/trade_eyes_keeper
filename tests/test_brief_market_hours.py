"""Scheduled briefs use exchange sessions and source timestamps, not fetch dates."""

from datetime import datetime
from unittest.mock import Mock

import pytest

from src.notification.brief_market import has_open_brief_market


@pytest.fixture(autouse=True)
def prevent_live_http(monkeypatch):
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda *args, **kwargs: pytest.fail(
            "brief scheduling test attempted live HTTP"
        ),
    )


@pytest.mark.parametrize(
    "code,clock,expected",
    [
        ("601728", "2026-10-21T09:29:59+08:00", False),
        ("601728", "2026-10-21T09:30:00+08:00", True),
        ("601728", "2026-10-21T11:30:00+08:00", False),
        ("601728", "2026-10-21T13:00:00+08:00", True),
        ("601728", "2026-10-21T15:00:00+08:00", False),
        ("601728", "2026-10-01T09:50:00+08:00", False),
        ("601728", "2026-02-18T09:50:00+08:00", False),
        ("00883", "2026-10-21T12:30:00+08:00", False),
        ("00883", "2026-10-21T13:00:00+08:00", True),
        ("00883", "2026-12-24T14:30:00+08:00", False),
        ("VOO", "2026-10-21T21:29:59+08:00", False),
        ("VOO", "2026-10-21T21:30:00+08:00", True),
        ("VOO", "2026-11-10T22:29:59+08:00", False),
        ("VOO", "2026-11-10T22:30:00+08:00", True),
        ("VOO", "2026-10-24T02:00:00+08:00", True),
        ("VOO", "2026-10-24T10:00:00+08:00", False),
        ("VOO", "2026-07-03T22:30:00+08:00", False),
        ("VOO", "2026-11-27T14:00:00-05:00", False),
    ],
)
def test_exchange_hours_holidays_breaks_and_dst(code, clock, expected):
    assert has_open_brief_market([code], datetime.fromisoformat(clock)) is expected


def test_calendar_failure_does_not_authorize_report(monkeypatch):
    monkeypatch.setattr(
        "src.notification.brief_market.resolve_market_session",
        Mock(side_effect=ValueError("calendar unavailable")),
    )
    assert not has_open_brief_market(
        ["601728", "00883"], datetime.fromisoformat("2026-10-21T09:50:00+08:00")
    )


@pytest.mark.parametrize(
    "clock", ["2026-10-21T09:00:00+08:00", "2026-10-01T09:50:00+08:00"]
)
def test_scheduled_run_exits_before_fetching_or_rebalancing(monkeypatch, clock):
    import main

    frozen = Mock()
    frozen.now.return_value = datetime.fromisoformat(clock)
    monkeypatch.delenv("BOT_FORCE", raising=False)
    monkeypatch.setattr(main, "datetime", frozen)
    monkeypatch.setattr(
        main, "load_config", lambda: {"stocks": ["601728"], "scheduler": {}}
    )
    fetcher = Mock()
    manager = Mock()
    monkeypatch.setattr(main, "StockDataFetcher", fetcher)
    monkeypatch.setattr(main, "SessionManager", manager)
    main.run_brief_report()
    manager.assert_not_called()
    fetcher.assert_not_called()


@pytest.mark.parametrize(
    "code,source,expected",
    [
        ("601728", "20261021161459", "2026-10-21T16:14:59+08:00"),
        ("00883", "2026/10/21 16:08:25", "2026-10-21T16:08:25+08:00"),
        ("VOO", "2026-10-21 11:04:59", "2026-10-21T11:04:59-04:00"),
    ],
)
def test_captured_qq_clock_formats_preserve_market_timezone(code, source, expected):
    from src.data.quote_time import parse_quote_time

    assert parse_quote_time(source, code).isoformat() == expected


def test_existing_singapore_quote_uses_provider_market_instead_of_us_default():
    from src.data.quote_time import parse_quote_time

    assert (
        parse_quote_time("20261021100000", "D05.SI", market="sg")
        .utcoffset()
        .total_seconds()
        == 8 * 3600
    )


def test_explicit_manual_force_retains_closed_market_snapshot(monkeypatch):
    import main

    frozen = Mock()
    frozen.now.return_value = datetime.fromisoformat("2026-10-01T09:00:00+08:00")
    monkeypatch.setattr(main, "datetime", frozen)
    monkeypatch.setattr(
        main, "load_config", lambda: {"stocks": ["601728"], "scheduler": {}}
    )
    manager = Mock(side_effect=RuntimeError("stop offline manual probe"))
    monkeypatch.setattr(main, "SessionManager", manager)
    main.run_brief_report(force=True)
    manager.assert_called_once()
