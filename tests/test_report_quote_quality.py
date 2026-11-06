"""Offline price/time contracts; all prices below are synthetic test values."""

import json
from datetime import datetime
from unittest.mock import Mock

import pandas as pd
import pytest

from src.core.report_quotes import assess_quote, prepare_report_quotes
from src.models.schemas import SessionContext, StockPriceData
from src.notification.report_builders import report_data_readiness


def clock(value):
    return datetime.fromisoformat(value)


def quote(stamp="2026-11-06T09:54:00+08:00", **changes):
    return dict(
        {
            "quote_time": stamp,
            "close": 10,
            "open": 9,
            "prev_close": 8,
            "high": 11,
            "low": 8,
            "date": stamp[:10],
        },
        **changes,
    )


@pytest.fixture(autouse=True)
def prevent_live_http(monkeypatch):
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda *args, **kwargs: pytest.fail("quote acceptance attempted live HTTP"),
    )


@pytest.mark.parametrize(
    "code,observed,stamp,expected",
    [
        (
            "601088",
            "2026-11-06T09:55:00+08:00",
            "2026-11-06T09:54:00+08:00",
            "realtime",
        ),
        ("00883", "2026-11-06T09:55:00+08:00", "2026-11-06T09:40:00+08:00", "delayed"),
        (
            "601088",
            "2026-11-06T09:55:00+08:00",
            "2026-11-05T15:00:00+08:00",
            "unavailable",
        ),
        (
            "601088",
            "2026-11-06T09:55:00+08:00",
            "2026-11-06T09:34:00+08:00",
            "unavailable",
        ),
        ("601088", "2026-11-06T12:20:00+08:00", "2026-11-06T11:29:00+08:00", "delayed"),
        (
            "601088",
            "2026-11-06T12:20:00+08:00",
            "2026-11-06T12:10:00+08:00",
            "unavailable",
        ),
        (
            "601088",
            "2026-11-07T09:55:00+08:00",
            "2026-11-06T15:00:00+08:00",
            "last_close",
        ),
        (
            "601088",
            "2026-11-06T16:00:00+08:00",
            "2026-11-06T10:00:00+08:00",
            "unavailable",
        ),
        (
            "00883",
            "2026-11-06T19:00:00+08:00",
            "2026-11-06T16:08:00+08:00",
            "last_close",
        ),
        ("VOO", "2026-11-06T09:55:00+08:00", "2026-11-05T16:00:00-05:00", "last_close"),
        ("VOO", "2026-07-06T09:55:00+08:00", "2026-07-02T16:00:00-04:00", "last_close"),
        (
            "VOO",
            "2026-11-06T09:55:00+08:00",
            "2026-11-05T18:00:00-05:00",
            "unavailable",
        ),
        (
            "601088",
            "2026-10-05T09:55:00+08:00",
            "2026-09-30T15:00:00+08:00",
            "last_close",
        ),
    ],
)
def test_exchange_sessions_holidays_lunch_and_dst(code, observed, stamp, expected):
    assert assess_quote(code, quote(stamp), clock(observed))["quote_status"] == expected


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"quote_time": None}, "源时间"),
        ({"quote_time": "invalid"}, "源时间"),
        ({"quote_time": "2026-11-06T09:54:00"}, "源时间"),
        ({"quote_time": "2026-11-06T09:56:00+08:00"}, "未来"),
        ({"close": float("inf")}, "价格"),
        ({"close": float("nan")}, "价格"),
        ({"close": 0}, "价格"),
        ({"close": -1}, "价格"),
        ({"low": 20}, "高低价"),
        ({"stock_code": "600000"}, "标的不匹配"),
    ],
)
def test_unknown_invalid_or_future_quotes_fail_closed(changes, reason):
    result = assess_quote(
        "601088", quote(**changes), clock("2026-11-06T09:55:00+08:00")
    )
    assert result["quote_status"] == "unavailable"
    assert reason in result["quote_reason"]


def test_declared_delayed_feed_never_becomes_realtime():
    result = assess_quote(
        "C38U.SI",
        quote(delay_seconds=120, timestamp_kind="delayed_market_snapshot"),
        clock("2026-11-06T09:55:00+08:00"),
    )
    assert result["quote_status"] == "delayed"


@pytest.mark.parametrize(
    "code,stamp,status",
    [
        ("601088", "2026-11-06T16:14:39+08:00", "last_close"),
        ("601088", "2026-11-06T16:15:01+08:00", "unavailable"),
        ("VOO", "2026-11-05T16:00:02-05:00", "last_close"),
        ("BRK.B", "2026-11-05T16:04:49-05:00", "last_close"),
        ("BRK.B", "2026-11-05T16:05:01-05:00", "unavailable"),
        ("VOO", "2026-11-05T18:00:00-05:00", "unavailable"),
    ],
)
def test_tencent_close_publication_window_is_bounded(code, stamp, status):
    result = assess_quote(
        code, quote(stamp, source="tencent"), clock("2026-11-06T20:00:00+08:00")
    )
    assert result["quote_status"] == status


def test_publication_grace_does_not_accept_yesterdays_quote_during_open():
    result = assess_quote(
        "601088",
        quote("2026-11-05T16:14:39+08:00", source="tencent"),
        clock("2026-11-06T09:55:00+08:00"),
    )
    assert result["quote_status"] == "unavailable"


def test_calendar_failure_is_an_explicit_gap(monkeypatch):
    monkeypatch.setattr(
        "src.core.report_quotes.resolve_market_session", Mock(side_effect=ValueError())
    )
    result = assess_quote("601088", quote(), clock("2026-11-06T09:55:00+08:00"))
    assert result["quote_status"] == "unavailable"
    assert "日历" in result["quote_reason"]


@pytest.mark.parametrize(
    "cached_at", ["2026-11-05T15:00:00+08:00", "2026-11-06T09:40:00+08:00"]
)
def test_cached_snapshot_refreshes_without_changing_history_or_retrying_gap(cached_at):
    session = SessionContext(
        session_id="synthetic", config={"stocks": ["601088", "00883"]}
    )
    old = StockPriceData(
        stock_code="601088",
        date=clock(cached_at),
        quote_time=cached_at,
        close=99,
        open=98,
        high=100,
        low=97,
        pe_ratio=12,
    )
    session.stocks_data["601088"] = old
    cached_history = pd.DataFrame(
        [{"date": pd.Timestamp(cached_at), "close": 99, "adjustment_type": "qfq"}]
    )
    history = {"601088": cached_history.copy(deep=True)}
    object.__setattr__(session, "_historical", history)
    provider = Mock(side_effect=[quote(), None, quote()])
    lookup = prepare_report_quotes(
        session, provider, clock=lambda: clock("2026-11-06T09:55:00+08:00")
    )
    assert set(session.stocks_data) == {"601088", "00883"}
    assert session.stocks_data["601088"].close == 10
    assert session.stocks_data["601088"].pe_ratio == 12
    assert session.stocks_data["00883"].close is None
    assert session._historical is history and old.close == 99
    pd.testing.assert_frame_equal(session._historical["601088"], cached_history)
    assert session.stocks_data["601088"].quote_status == "realtime"
    assert lookup("00883") is None and lookup("00883") is None
    assert provider.call_count == 2
    assert lookup("600036")["close"] == 10
    lookup("600036")
    assert provider.call_count == 3
    assert report_data_readiness(session)["status"] == "not_ready"


@pytest.mark.parametrize("price", [0.05, 0.0001, 10.123456])
def test_missing_history_does_not_prevent_valid_quote_and_low_price(price):
    session = SessionContext(session_id="low", config={"stocks": ["00883"]})
    prepare_report_quotes(
        session,
        lambda _: quote(
            close=price, open=price, high=price, low=price, prev_close=price
        ),
        clock=lambda: clock("2026-11-06T09:55:00+08:00"),
    )
    assert session.stocks_data["00883"].close == price
    assert report_data_readiness(session)["status"] == "ready"


def test_invalid_optional_volume_does_not_abort_other_quotes():
    session = SessionContext(
        session_id="volume", config={"stocks": ["601088", "00883"]}
    )
    prepare_report_quotes(
        session,
        lambda _: quote(volume="bad", amount=float("inf"), turnover=-1),
        clock=lambda: clock("2026-11-06T09:55:00+08:00"),
    )
    assert len(session.stocks_data) == 2
    for row in session.stocks_data.values():
        assert row.close == 10
        assert row.volume is None and row.amount is None and row.turnover is None


def test_missing_previous_close_is_not_repaired_from_old_bar():
    session = SessionContext(session_id="missing", config={"stocks": ["601088"]})
    session.stocks_data["601088"] = StockPriceData(
        stock_code="601088", close=99, prev_close=88
    )
    prepare_report_quotes(
        session,
        lambda _: quote(prev_close=None),
        clock=lambda: clock("2026-11-06T09:55:00+08:00"),
    )
    row = session.stocks_data["601088"]
    assert row.close == 10 and row.prev_close is None and row.change_pct is None
    assert report_data_readiness(session)["status"] == "not_ready"


def test_brief_channels_keep_closed_markets_gaps_and_source_previous_close(
    monkeypatch, tmp_path
):
    from src.notification.email_notifier import EmailNotifier
    from src.notification.feishu_notifier import FeishuNotifier
    from src.notification.telegram_notifier import TelegramNotifier

    pool = ["601728", "VOO", "00883"]
    session = SessionContext(session_id="channels", config={"stocks": pool})
    quotes = {
        "601728": quote(close=6.1, open=6, high=6.2, low=5.9, prev_close=5.97),
        "VOO": quote("2026-11-05T16:00:00-05:00"),
        "00883": None,
    }
    prepare_report_quotes(
        session, quotes.get, clock=lambda: clock("2026-11-06T09:55:00+08:00")
    )
    assert set(session.get_all_dataframe()["stock_code"]) == set(pool)
    assert session.stocks_data["VOO"].quote_status == "last_close"
    assert session.stocks_data["00883"].close is None

    email = EmailNotifier({"email": {"archive_dir": str(tmp_path / "mail")}})
    send = Mock(return_value={"status": "sent"})
    monkeypatch.setattr(email, "_send_email", send)
    monkeypatch.setattr(email, "_get_server_info", dict)
    email.send_brief_report(session, {"label": "版式测试"})
    html = send.call_args.args[1]
    assert "<th>前收</th>" in html and "<td>5.97</td>" in html

    feishu = FeishuNotifier({})
    send_card = Mock(return_value=(True, "offline"))
    monkeypatch.setattr(feishu, "_send_card", send_card)
    feishu.send_brief_report(session, {"label": "版式测试"})
    card = send_card.call_args.args[0]
    elements = card["card"]["body"]["elements"]
    table = next(element for element in elements if element["tag"] == "table")
    columns = {column["display_name"]: column["name"] for column in table["columns"]}
    rows = {row[columns["代码"]]: row for row in table["rows"]}
    assert set(rows) == set(pool)
    assert rows["601728"][columns["前收"]] == "5.97"
    assert rows["00883"][columns["前收"]] == "—"

    for output in (
        html,
        json.dumps(card, ensure_ascii=False),
        str(TelegramNotifier({})._prepare_brief_report(session, {})),
    ):
        assert all(code in output for code in pool)
        assert "最近收盘" in output and "缺口" in output
        assert "前收" in output and "5.97" in output


def test_assessment_uses_end_of_collection_clock():
    session = SessionContext(session_id="slow", config={"stocks": ["601088", "00883"]})
    clocks = iter(
        [
            clock("2026-11-06T09:55:00+08:00"),
            clock("2026-11-06T10:20:00+08:00"),
            clock("2026-11-06T10:20:00+08:00"),
        ]
    )
    prepare_report_quotes(session, lambda _: quote(), clock=lambda: next(clocks))
    assert all(
        row.quote_status == "unavailable" for row in session.stocks_data.values()
    )


def test_all_missing_is_failed_but_delivery_acknowledgement_is_retained():
    from main import _report_delivery_result

    session = SessionContext(session_id="empty", config={"stocks": ["601088"]})
    prepare_report_quotes(
        session, lambda _: None, clock=lambda: clock("2026-11-06T09:55:00+08:00")
    )
    result = _report_delivery_result(
        {"status": "succeeded", "deliveries": {"email": {"status": "sent"}}}, session
    )
    assert result.status == "failed"
    assert result.deliveries["email"]["status"] == "sent"
