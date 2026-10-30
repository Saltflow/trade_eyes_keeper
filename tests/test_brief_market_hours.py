"""Scheduled briefs use exchange sessions and source timestamps, not fetch dates."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from src.notification.brief_market import active_brief_codes, has_open_brief_market


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


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("2026-10-21T09:49:00+08:00", ("601728",)),
        ("2026-10-20T14:59:00+08:00", ()),
        ("2026-10-21T09:25:00+08:00", ()),
        ("2026-10-21T09:51:00+08:00", ()),
        ("2026-10-21T09:49:00", ()),
        (None, ()),
        ("invalid", ()),
    ],
)
def test_source_quote_must_belong_to_current_open_session(quote, expected):
    frame = pd.DataFrame(
        [{"stock_code": "601728", "date": "2026-10-21", "quote_time": quote}]
    )
    assert (
        active_brief_codes(frame, datetime.fromisoformat("2026-10-21T09:50:00+08:00"))
        == expected
    )


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


def test_open_day_with_only_cached_quotes_exits_before_scan(monkeypatch):
    import main

    frozen = Mock()
    frozen.now.return_value = datetime.fromisoformat("2026-10-21T09:50:00+08:00")
    monkeypatch.delenv("BOT_FORCE", raising=False)
    monkeypatch.setattr(main, "datetime", frozen)
    monkeypatch.setattr(
        main, "load_config", lambda: {"stocks": ["601728"], "scheduler": {}}
    )
    frame = pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "date": "2026-10-21",
                "quote_time": "2026-10-20T15:00:00+08:00",
            }
        ]
    )
    session = SimpleNamespace(
        stocks_data={"601728": object()},
        get_all_dataframe=lambda: frame,
        session_id="offline",
    )
    manager = Mock()
    manager.create_session.return_value = session
    monkeypatch.setattr(main, "SessionManager", Mock(return_value=manager))
    monkeypatch.setattr(main, "StockDataFetcher", Mock())
    scan, notifier = Mock(), Mock()
    monkeypatch.setattr(main, "_get_active_group_context", scan)
    monkeypatch.setattr(main, "NotifierManager", notifier)
    main.run_brief_report()
    scan.assert_not_called()
    notifier.assert_not_called()


def test_channel_rows_use_the_same_active_codes_and_previous_close():
    from src.models.schemas import StockPriceData
    from src.notification.feishu_notifier import FeishuNotifier
    from src.notification.report_builders import (
        build_brief_entries,
        get_brief_dataframe,
    )
    from src.notification.telegram_notifier import TelegramNotifier

    frame = pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "stock_name": "电信",
                "date": "2026-10-21",
                "open": 6.0,
                "high": 6.2,
                "low": 5.9,
                "close": 6.1,
                "prev_close": 5.97,
            },
            {"stock_code": "VOO", "date": "2026-10-20", "close": 500.0},
        ]
    )
    model_row = frame.iloc[0].copy()
    model_row["date"] = pd.Timestamp(model_row["date"])
    model = StockPriceData.from_dataframe_row(model_row, "601728")
    assert model.to_dict()["prev_close"] == 5.97
    session = SimpleNamespace(
        get_all_dataframe=lambda: frame, brief_active_codes=("601728",)
    )
    selected = get_brief_dataframe(session)
    # Fixture models the existing local calendar/date filter, not an instant.
    today = datetime(2026, 10, 21)  # noqa: DTZ001
    assert build_brief_entries(selected, today)[0]["prev_close"] == 5.97
    for output in [
        FeishuNotifier._build_brief_rows(selected, today),
        TelegramNotifier._build_brief_blocks(selected, today),
    ]:
        assert "昨收" in output and "5.97" in output and "VOO" not in output


def test_missing_previous_close_is_not_replaced_by_current_price():
    from src.notification.report_builders import build_brief_entries

    frame = pd.DataFrame([{"stock_code": "601728", "date": "2026-10-21", "close": 6.1}])
    # Fixture models the existing local calendar/date filter, not an instant.
    assert build_brief_entries(frame, datetime(2026, 10, 21))[0]["prev_close"] is None  # noqa: DTZ001


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


def test_actual_email_and_feishu_card_include_previous_close(monkeypatch):
    from src.notification.email_notifier import EmailNotifier
    from src.notification.feishu_notifier import FeishuNotifier

    frame = pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "stock_name": "电信",
                # Fixture models the existing local calendar/date filter, not an instant.
                "date": datetime.now(),  # noqa: DTZ005
                "open": 6.0,
                "close": 6.1,
                "prev_close": 5.97,
            }
        ]
    )
    session = SimpleNamespace(
        get_all_dataframe=lambda: frame,
        signal_scan=None,
        brief_active_codes=("601728",),
        ref_portfolio_status=None,
    )
    email = EmailNotifier({"email": {}})
    send = Mock()
    monkeypatch.setattr(email, "_send_email", send)
    email.send_brief_report(session, {"label": "版式测试"})
    assert "<th>昨收</th>" in send.call_args.args[1]
    assert "<td>5.97</td>" in send.call_args.args[1]
    feishu = FeishuNotifier({})
    send_card = Mock(return_value=(True, "offline"))
    monkeypatch.setattr(feishu, "_send_card", send_card)
    feishu.send_brief_report(session, {"label": "版式测试"})
    elements = send_card.call_args.args[0]["card"]["body"]["elements"]
    table = next(e for e in elements if e["tag"] == "table")
    assert any(c["display_name"] == "昨收" for c in table["columns"])
    assert table["rows"][0]["prev_close"] == "5.97"


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
