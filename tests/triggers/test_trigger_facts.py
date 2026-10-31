"""Unit-only source fixtures; no network, reports, model calls or user tasks."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from src.data.market_calendar import MarketSession
from src.models.schemas import SessionContext
from src.triggers.facts import (
    FactSnapshot,
    FactValue,
    load_trigger_facts,
    normalize_q3_event,
)

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=ZONE)
CONFIG = {"stocks": ["601088", "00700"]}


def open_session(code, *, as_of):
    return MarketSession(
        open=as_of.replace(hour=9, minute=30).astimezone(timezone.utc),
        close=as_of.replace(hour=15, minute=0).astimezone(timezone.utc),
        break_start=as_of.replace(hour=11, minute=30).astimezone(timezone.utc),
        break_end=as_of.replace(hour=13, minute=0).astimezone(timezone.utc),
    )


def quote(**changes):
    return {"close": 51.0, "low": 45.0, "quote_time": NOW.isoformat(), **changes}


def announcement(**changes):
    return {
        "stock_code": "601088",
        "title": "中国神华2026年第三季度报告",
        "published_at": "2026-10-29T20:30:00+08:00",
        "exchange": "cninfo",
        "url": "https://static.cninfo.com.cn/unit/q3.pdf",
        **changes,
    }


def statuses(codes, start, end, *, rows=None, unavailable=()):
    return {
        code: {
            "available": code not in unavailable,
            "items": rows or [],
            "coverage_start": start,
            "coverage_end": end,
            "source": "unit",
            "error": "HTTP timeout" if code in unavailable else None,
        }
        for code in codes
    }


def test_last_price_uses_current_trade_not_intraday_low_and_only_quote_dependency():
    get_quote = Mock(return_value=quote())
    expensive = Mock(side_effect=AssertionError("must not load unrelated producer"))
    snap = load_trigger_facts(
        {"quotes.601088.last"},
        CONFIG,
        as_of=NOW,
        providers={
            "quote": get_quote,
            "market_session": open_session,
            "history": expensive,
            "valuation": expensive,
            "report": expensive,
            "announcements": expensive,
        },
    )
    assert snap.resolve("quotes.601088.last").value == 51
    assert snap.resolve("quotes.601088.last").status == "available"
    assert snap.resolve("quotes.601088.low").value == 45
    assert snap.resolve("quotes.601088.last").as_of == NOW
    get_quote.assert_called_once_with("601088")
    expensive.assert_not_called()


@pytest.mark.parametrize(
    "source_time,clock,session",
    [
        (NOW - timedelta(seconds=121), NOW, open_session),
        (NOW + timedelta(seconds=1), NOW, open_session),
        (NOW, NOW.replace(hour=16), open_session),
        (NOW.replace(hour=11, minute=29), NOW.replace(hour=12), open_session),
        (NOW, NOW, lambda code, **kwargs: None),
    ],
)
def test_closed_stale_future_and_lunch_quotes_are_not_available(
    source_time, clock, session
):
    snap = load_trigger_facts(
        {"quotes.601088.last"},
        CONFIG,
        as_of=clock,
        providers={
            "quote": lambda code: quote(quote_time=source_time.isoformat()),
            "market_session": session,
        },
    )
    assert snap.resolve("quotes.601088.last").status == "stale"
    assert snap.resolve("quotes.601088.fresh").value is False


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"close": 49},
        quote(close=True),
        quote(close=float("nan")),
        quote(close=0),
    ],
)
def test_missing_or_invalid_prices_never_become_zero_or_false(bad):
    snap = load_trigger_facts(
        {"quotes.601088.last"},
        CONFIG,
        as_of=NOW,
        providers={
            "quote": lambda code: bad,
            "market_session": open_session,
        },
    )
    assert snap.resolve("quotes.601088.last").status == "unknown"
    assert snap.resolve("quotes.601088.last").value is None


def test_wildcard_retains_each_security_availability():
    snap = load_trigger_facts(
        {"quotes.*.last"},
        CONFIG,
        as_of=NOW,
        providers={
            "quote": lambda code: quote(close=49) if code == "601088" else None,
            "market_session": open_session,
        },
    )
    leaves = snap.resolve("quotes.*.last").value
    assert {item.status for item in leaves} == {"available", "unknown"}
    assert {item.security for item in leaves} == {"601088", "00700"}


def test_removed_stock_in_old_report_cannot_match_current_pool_wildcard():
    old_report = {
        "stocks_data": {
            "601088": {"dividend_yield": 20.0},  # Removed from current monitored pool.
            "00700": {"dividend_yield": None},
        }
    }
    snap = load_trigger_facts(
        {"stocks_data.*.dividend_yield"},
        {"stocks": ["00700"]},
        as_of=NOW,
        session=old_report,
    )
    leaves = snap.resolve("stocks_data.*.dividend_yield").value
    assert len(leaves) == 1 and leaves[0].security == "00700"
    assert leaves[0].status == "unknown"
    assert "stocks_data.601088.dividend_yield" not in snap.facts
    assert "601088" not in str(snap.resolve("stocks_data").value)


def test_explicit_unmonitored_dependency_is_retained_but_excluded_from_wildcard():
    snap = load_trigger_facts(
        {"stocks_data.*.dividend_yield", "stocks_data.601088.dividend_yield"},
        {"stocks": ["00700"]},
        as_of=NOW,
        session={
            "stocks_data": {
                "601088": {"dividend_yield": 20.0},
                "00700": {"dividend_yield": 1.0},
            }
        },
    )
    assert snap.resolve("stocks_data.601088.dividend_yield").value == 20
    leaves = snap.resolve("stocks_data.*.dividend_yield").value
    assert len(leaves) == 1 and leaves[0].security == "00700" and leaves[0].value == 1


def test_report_snapshot_priority_and_known_zero_are_preserved():
    snapshot = {
        "stocks_data": {"601088": {"roe": 0.0, "date": "2026-10-29", "pe_ratio": 5.0}},
        "placements": {"601088": {"approved": False}},
        "dividend_events": [],
    }
    callback = Mock(side_effect=AssertionError("snapshot should be reused"))
    snap = load_trigger_facts(
        {
            "stocks_data.601088.roe",
            "stocks_data.601088.pe_ratio",
            "placements.601088.approved",
            "dividend_events",
        },
        CONFIG,
        as_of=NOW,
        session=snapshot,
        providers={"report": callback, "valuation": callback},
    )
    assert snap.resolve("stocks_data.601088.roe").value == 0
    assert snap.resolve("placements.601088.approved").value is False
    assert snap.resolve("dividend_events").value == []
    assert snap.resolve("dividend_events").status == "available"
    callback.assert_not_called()


def test_unprepared_session_defaults_remain_unknown():
    snap = load_trigger_facts(
        {"dividend_events"},
        CONFIG,
        as_of=NOW,
        session=SessionContext(session_id="unit"),
    )
    assert snap.resolve("dividend_events").status == "unknown"


def test_free_history_and_valuation_are_grouped_and_no_report_is_generated():
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-07-01", periods=90),
            "close": range(40, 130),
            "open": range(40, 130),
            "high": range(41, 131),
            "low": range(39, 129),
            "volume": [1000] * 90,
        }
    )
    history = Mock(return_value=frame)
    valuation = Mock(return_value={"pe_ratio": 7.2, "pb_ratio": 0.9})
    snap = load_trigger_facts(
        {
            "stocks_data.601088.ma60",
            "stocks_data.601088.rsi",
            "stocks_data.601088.pe_ratio",
        },
        CONFIG,
        as_of=NOW,
        providers={"history": history, "valuation": valuation},
    )
    assert snap.resolve("stocks_data.601088.ma60").status == "available"
    assert snap.resolve("stocks_data.601088.pe_ratio").value == 7.2
    assert snap.resolve("stocks_data.601088.pe_ratio").as_of is None
    history.assert_called_once_with("601088", 400)
    valuation.assert_called_once_with("601088")


def test_single_quote_history_fallback_cannot_fabricate_mature_technical_values():
    frame = pd.DataFrame(
        [
            {
                "date": "2026-10-29",
                "close": 49.0,
                "open": 49.0,
                "high": 50.0,
                "low": 48.0,
                "volume": 1000,
            }
        ]
    )
    paths = {
        f"stocks_data.601088.{name}"
        for name in ("ma60", "wma20", "macd", "adx", "boll_pct_b", "close")
    }
    snap = load_trigger_facts(
        paths, CONFIG, as_of=NOW, providers={"history": lambda code, days: frame}
    )
    assert snap.resolve("stocks_data.601088.close").value == 49
    for name in ("ma60", "wma20", "macd", "adx", "boll_pct_b"):
        assert snap.resolve(f"stocks_data.601088.{name}").status == "unknown"


def test_generic_sec_quarterly_missing_period_keeps_event_coverage_unknown():
    row = announcement(
        stock_code="AAPL", title="10-Q · 季度报告", type="10-Q", exchange="sec"
    )
    snap = load_trigger_facts(
        {"events.announcement.q3_report"},
        {"stocks": ["AAPL"]},
        as_of=NOW,
        providers={
            "announcements": lambda codes, start, end: statuses(
                codes, start, end, rows=[row]
            )
        },
    )
    assert snap.events == []
    assert snap.resolve("events.announcement.q3_report").status == "unknown"


def test_missing_paid_research_and_unknown_fields_never_launch_full_report():
    snap = load_trigger_facts(
        {"stocks_data.*.dividend_per_share", "backtest.hk.sharpe"}, CONFIG, as_of=NOW
    )
    assert all(
        item.status == "unknown"
        for item in snap.resolve("stocks_data.*.dividend_per_share").value
    )
    assert snap.resolve("backtest.hk.sharpe").status == "unknown"


@pytest.mark.parametrize(
    "title",
    [
        "中国神华2026年年度报告",
        "2026年半年度报告",
        "2026年第一季度报告",
        "2026年第三季度业绩预告",
        "2026年第三季度报告董事会决议",
        "2026 Interim Report",
    ],
)
def test_non_q3_reports_and_forecasts_do_not_become_events(title):
    assert (
        normalize_q3_event(announcement(title=title), "601088", observed_at=NOW) is None
    )


def test_q3_original_publication_period_and_stable_id_survive_refetch():
    first = normalize_q3_event(announcement(), "601088", observed_at=NOW)
    second = normalize_q3_event(
        announcement(), "601088", observed_at=NOW + timedelta(days=1)
    )
    assert first.id == second.id
    assert first.report_period == "2026Q3"
    assert first.publication_date == date(2026, 10, 29)
    assert first.published_at == datetime(2026, 10, 29, 20, 30, tzinfo=ZONE)
    assert second.observed_at != first.observed_at


def test_sec_generic_10q_needs_disclosed_report_period():
    row = announcement(
        stock_code="AAPL", title="10-Q · 季度报告", type="10-Q", exchange="sec"
    )
    with pytest.raises(ValueError, match="period unavailable"):
        normalize_q3_event(row, "AAPL", observed_at=NOW)
    row["reportDate"] = "2026-09-30"
    assert normalize_q3_event(row, "AAPL", observed_at=NOW).report_period == "2026Q3"


@pytest.mark.parametrize(
    "title",
    [
        "截至二零二六年九月三十日止三個月及九個月業績公佈",
        "截至2026年9月30日止九个月业绩公告",
        "2026年第三季度業績公告",
        "Unaudited Results for the Nine Months Ended September 30, 2026",
    ],
)
def test_q3_financial_results_and_hk_chinese_period_titles(title):
    event = normalize_q3_event(announcement(title=title), "601088", observed_at=NOW)
    assert event.report_period == "2026Q3"


def test_q3_conflicting_disclosed_period_is_rejected():
    with pytest.raises(ValueError, match="conflicts"):
        normalize_q3_event(
            announcement(report_period="2026Q2"), "601088", observed_at=NOW
        )


def test_same_day_full_report_summary_and_translation_are_one_business_event():
    rows = [
        announcement(),
        announcement(
            title="中国神华2026年第三季度报告摘要",
            url="https://static.cninfo.com.cn/unit/summary.pdf",
        ),
        announcement(
            title="2026 Third Quarter Report",
            url="https://static.cninfo.com.cn/unit/english.pdf",
        ),
    ]
    snap = load_trigger_facts(
        {"events.announcement.q3_report"},
        {"stocks": ["601088"]},
        as_of=NOW,
        providers={
            "announcements": lambda codes, start, end: statuses(
                codes, start, end, rows=rows
            )
        },
    )
    assert len(snap.events) == 1
    assert len(snap.events[0].source_urls) == 3
    assert len(snap.events[0].source_documents) == 3
    assert len(snap.events[0].document_ids) == 3


def test_strict_event_dependency_loads_two_calendar_days_without_price_or_llm():
    get_announcements = Mock(
        side_effect=lambda codes, start, end: statuses(
            codes, start, end, rows=[announcement()]
        )
    )
    snap = load_trigger_facts(
        {"events.announcement.q3_report"},
        {"stocks": ["601088"]},
        as_of=NOW,
        providers={"announcements": get_announcements},
    )
    get_announcements.assert_called_once_with(
        ["601088"], date(2026, 10, 29), date(2026, 10, 30)
    )
    assert len(snap.events) == 1
    assert snap.resolve("events.announcement.q3_report").status == "available"
    assert snap.events[0].observed_at == snap.observed_at


@pytest.mark.parametrize(
    "response",
    [
        {"available": False, "error": "HTTP 503"},
        {"available": True, "items": []},
        {
            "available": True,
            "coverage_start": date(2026, 10, 29),
            "coverage_end": date(2026, 10, 30),
            "items": None,
        },
    ],
)
def test_failed_missing_schema_or_incomplete_coverage_is_unknown(response):
    snap = load_trigger_facts(
        {"events.announcement.q3_report"},
        {"stocks": ["601088"]},
        as_of=NOW,
        providers={"announcements": lambda *args: {"601088": response}},
    )
    assert snap.events == []
    assert snap.resolve("events.announcement.q3_report").status == "unknown"


def test_explicit_successful_empty_window_is_available():
    snap = load_trigger_facts(
        {"events.announcement.q3_report"},
        CONFIG,
        as_of=NOW,
        providers={"announcements": statuses},
    )
    assert snap.events == []
    assert snap.resolve("events.announcement.q3_report").status == "available"


def test_safe_resolve_never_invokes_arbitrary_attributes():
    snap = FactSnapshot(NOW, NOW, {"x": FactValue(value=False, status="available")})
    assert snap.resolve("x.__class__").status == "unknown"
    assert snap.resolve("missing").value is None


def test_runtime_completion_clock_accepts_quotes_newer_than_poll_start():
    completed = NOW + timedelta(seconds=20)
    snap = load_trigger_facts(
        {"quotes.601088.last"},
        CONFIG,
        as_of=NOW,
        clock=lambda: completed,
        providers={
            "quote": lambda code: quote(
                quote_time=(NOW + timedelta(seconds=15)).isoformat()
            ),
            "market_session": open_session,
        },
    )
    assert snap.as_of == completed and snap.observed_at == completed
    assert snap.resolve("quotes.601088.last").status == "available"
    assert snap.resolve("quotes.601088.last").observed_at == completed


def test_runtime_completion_clock_ages_earlier_symbols_at_same_final_instant():
    completed = NOW + timedelta(seconds=20)
    snap = load_trigger_facts(
        {"quotes.*.last"},
        CONFIG,
        as_of=NOW,
        clock=lambda: completed,
        providers={
            "quote": lambda code: quote(
                quote_time=(NOW - timedelta(seconds=110)).isoformat()
            ),
            "market_session": open_session,
        },
    )
    assert {item.status for item in snap.resolve("quotes.*.last").value} == {"stale"}


def test_naive_completion_clock_is_rejected():
    with pytest.raises(ValueError, match="aware"):
        load_trigger_facts(
            set(), CONFIG, as_of=NOW, clock=lambda: NOW.replace(tzinfo=None)
        )


def test_pre_cancelled_collection_calls_no_quote_history_valuation_or_announcement_provider():
    callback = Mock(side_effect=AssertionError("cancelled source must not be called"))
    snap = load_trigger_facts(
        {
            "quotes.601088.last",
            "stocks_data.601088.ma60",
            "stocks_data.601088.pe_ratio",
            "events.announcement.q3_report",
        },
        CONFIG,
        as_of=NOW,
        cancelled=lambda: True,
        providers={
            name: callback
            for name in ("quote", "history", "valuation", "announcements", "report")
        },
    )
    callback.assert_not_called()
    assert snap.resolve("quotes.601088.last").status == "unknown"
    assert snap.resolve("stocks_data.601088.ma60").status == "unknown"
    assert snap.resolve("events.announcement.q3_report").status == "unknown"
    assert "cancelled" in snap.resolve("events.announcement.q3_report").error


def test_monotonic_budget_keeps_successful_quote_and_unknown_remaining_without_wait(
    monkeypatch,
):
    elapsed = [0.0]
    monkeypatch.setattr("src.triggers.facts.time.monotonic", lambda: elapsed[0])
    calls = []

    def get_quote(code):
        calls.append(code)
        elapsed[0] = 2.0
        return quote(close=49)

    snap = load_trigger_facts(
        {"quotes.*.last"},
        CONFIG,
        as_of=NOW,
        budget_seconds=1,
        providers={"quote": get_quote, "market_session": open_session},
    )
    assert calls == ["601088"]
    assert snap.resolve("quotes.601088.last").status == "available"
    assert snap.resolve("quotes.00700.last").status == "unknown"
    assert "budget exhausted" in snap.resolve("quotes.00700.last").error


def test_etf_lookthrough_valuation_requires_snapshot_and_never_starts_default_crawler(
    monkeypatch,
):
    constructor = Mock(side_effect=AssertionError("expensive ETF producer"))
    monkeypatch.setattr("src.data.web_crawler.StockWebCrawler", constructor)
    snap = load_trigger_facts(
        {"stocks_data.VOO.pe_ratio"}, {"stocks": ["VOO"]}, as_of=NOW
    )
    constructor.assert_not_called()
    assert snap.resolve("stocks_data.VOO.pe_ratio").status == "unknown"
    assert "snapshot" in snap.resolve("stocks_data.VOO.pe_ratio").error


def test_dotted_us_snapshot_identity_preserves_original_source_time():
    snap = load_trigger_facts(
        {"stocks_data.*.close"},
        {"stocks": ["BRK.B"]},
        as_of=NOW,
        session={
            "stocks_data": {
                "BRK.B": {"close": 500.0, "date": "2026-10-29T16:00:00-04:00"}
            }
        },
    )
    fact = snap.resolve("stocks_data.BRK-B.close")
    assert fact.status == "available"
    assert fact.as_of.isoformat() == "2026-10-29T16:00:00-04:00"


def test_calendar_next_day_event_is_loaded_on_weekend():
    saturday = datetime(2026, 10, 31, 10, tzinfo=ZONE)
    event_row = announcement(published_at="2026-10-30T20:30:00+08:00")
    snap = load_trigger_facts(
        {"events.announcement.q3_report"},
        {"stocks": ["601088"]},
        as_of=saturday,
        providers={
            "announcements": lambda codes, start, end: statuses(
                codes, start, end, rows=[event_row]
            )
        },
    )
    assert len(snap.events) == 1
    assert snap.events[0].publication_date == date(2026, 10, 30)
