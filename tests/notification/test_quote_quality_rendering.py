"""All report channels render the same frozen quote quality without re-dating it."""

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.notification import daily_pdf
from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.report_policy import (
    FUNDAMENTAL_COLUMNS,
    format_price,
    fundamental_rows,
    quote_presentation,
    quote_status_summary,
)
from src.notification.telegram_notifier import TelegramNotifier


@pytest.fixture
def assessed_report(tmp_path):
    # Labelled synthetic observations. The renderer consumes an assessment made
    # by the application, even if the source date is outside the old 3-day gate.
    records = []
    for code, status, stamp in (
        ("601088", "realtime", "2026-11-06T09:54:00+08:00"),
        ("00883", "delayed", "2026-11-06T09:35:11+08:00"),
        ("VOO", "last_close", "2026-10-30T16:00:00-04:00"),
        ("C38U.SI", "unavailable", None),
    ):
        records.append(
            {
                "stock_code": code,
                "stock_name": "合成验收标的",
                "date": "2026-10-30",
                "quote_status": status,
                "quote_as_of": stamp,
                "quote_reason": "报价时间缺失 & 待核实"
                if status == "unavailable"
                else "",
                "open": 11.25,
                "prev_close": 11.1,
                "close": 11.5 if status != "unavailable" else 9876.54,
                "pe_ratio": 15.6,
                "pb_ratio": 1.2,
                "roe": 9.8,
            }
        )
    frame = pd.DataFrame(records)
    config = {
        "stocks": [row["stock_code"] for row in records],
        "email": {"archive_dir": str(tmp_path / "archive")},
    }
    session = SimpleNamespace(
        config=config,
        created_at="2026-11-06T09:54:10+08:00",
        stocks_data={row["stock_code"]: row for row in records},
        errors=[],
        announcements={},
        placements={},
        dividend_events=[],
        strategy_return_curves=None,
        get_all_dataframe=lambda: frame,
    )
    return session, frame


def assert_quote_evidence(text):
    for value in (
        "601088",
        "00883",
        "VOO",
        "C38U.SI",
        "实时",
        "延迟采样",
        "最近收盘",
        "缺口",
        "2026-11-06 09:35:11+08:00",
        "2026-10-30 16:00:00-04:00",
        "源时间未知",
        "报价时间缺失",
    ):
        assert value in text
    assert "9876.54" not in text
    assert "今开" not in text and "昨收" not in text and "现价" not in text
    assert "开盘" in text and "前收" in text and "报价" in text


def test_frozen_summary_counts_and_legacy_rows_are_explicit(assessed_report):
    _, frame = assessed_report
    assert quote_status_summary(frame) == "实时 1 · 延迟采样 1 · 最近收盘 1 · 缺口 1"
    assert quote_presentation({"date": "2026-11-06", "close": 10})["text"] == (
        "时效未确认 · 源时间未知"
    )
    for stamp in (None, "2026-11-06", "invalid"):
        assert (
            quote_presentation({"quote_status": "realtime", "quote_as_of": stamp})[
                "status"
            ]
            == "unavailable"
        )
    rows = {
        row[0]: dict(zip((key for key, _ in FUNDAMENTAL_COLUMNS), row))
        for row in fundamental_rows(frame)
    }
    assert rows["C38U.SI"]["close"] == "—"
    assert rows["C38U.SI"]["pe_ratio"] == "15.60"
    assert rows["C38U.SI"]["roe"] == "9.80"


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, "1.00"),
        (0.6, "0.60"),
        (0.603, "0.603"),
        (0.0001, "0.0001"),
        (1.234567891, "1.23456789"),
        (0.00000001, "0.00000001"),
        (0.000000001, "<0.00000001"),
        (None, "—"),
        (0, "—"),
        (float("inf"), "—"),
        (float("nan"), "—"),
    ],
)
def test_price_format_retains_positive_precision(value, expected):
    assert format_price(value) == expected


def test_price_precision_is_shared_by_email_pdf_and_bot_tables(
    assessed_report, monkeypatch
):
    session, frame = assessed_report
    frame.loc[frame.stock_code == "601088", ["open", "prev_close", "close"]] = [
        0.603,
        0.0001,
        0.6031,
    ]
    notifier = EmailNotifier(session.config)
    sources = [
        notifier._build_daily_watchlist_section(notifier._daily_watchlist_rows(frame)),
        notifier._prepare_brief_report(session, {"label": "合成验收"})[1],
        json.dumps(FeishuNotifier({})._fundamental_table(frame), ensure_ascii=False),
        TelegramNotifier({})._prepare_brief_report(session, {})[1],
    ]
    compiled = []
    monkeypatch.setattr(
        daily_pdf,
        "_compile_pdf",
        lambda path: (
            compiled.append(path.read_text(encoding="utf-8")) or b"%PDF-synthetic"
        ),
    )
    assert daily_pdf.generate_daily_pdf(session, frame, get_server_info=dict)
    sources.extend(compiled)
    for source in sources:
        assert "0.603" in source and "0.0001" in source and "0.6031" in source


def test_daily_email_keeps_all_rows_and_independent_fundamentals(assessed_report):
    session, frame = assessed_report
    notifier = EmailNotifier(session.config)
    rows = notifier._daily_watchlist_rows(frame)
    body = notifier._build_daily_summary_section(rows)
    body += notifier._build_daily_watchlist_section(rows)
    assert_quote_evidence(body)
    assert "行情就绪" not in body
    assert "报价时间缺失 &amp; 待核实" in body
    unavailable = next(row for row in rows if row["code"] == "C38U.SI")
    assert unavailable["entry"] is None and unavailable["pe_ratio"] == 15.6
    assert next(row for row in rows if row["code"] == "VOO")["entry"]["close"] == 11.5


@pytest.mark.parametrize("channel", ["email", "feishu", "telegram"])
def test_brief_channels_share_per_row_status_and_source_clock(assessed_report, channel):
    session, _ = assessed_report
    cls = {
        "email": EmailNotifier,
        "feishu": FeishuNotifier,
        "telegram": TelegramNotifier,
    }[channel]
    result = cls(session.config)._prepare_brief_report(session, {"label": "合成验收"})
    text = json.dumps(result, ensure_ascii=False)
    assert_quote_evidence(text)
    if channel == "email":
        assert "可用报价：3/4" in text


@pytest.mark.parametrize("cls", [FeishuNotifier, TelegramNotifier])
def test_daily_bots_retain_gap_rows_and_quote_clock(assessed_report, monkeypatch, cls):
    session, _ = assessed_report
    notifier, sent = cls(session.config), []
    monkeypatch.setattr(
        notifier, "_send", lambda *a, **kw: sent.append((a, kw)) or {"status": "sent"}
    )
    assert notifier.send_daily_report_from_session(session)["status"] == "sent"
    assert_quote_evidence(json.dumps(sent, ensure_ascii=False))


def test_pdf_keeps_quote_source_clock_and_escapes_gap_reason(
    assessed_report, monkeypatch
):
    session, frame = assessed_report
    compiled = []
    monkeypatch.setattr(
        daily_pdf,
        "_compile_pdf",
        lambda path: (
            compiled.append(path.read_text(encoding="utf-8")) or b"%PDF-synthetic"
        ),
    )
    assert (
        daily_pdf.generate_daily_pdf(session, frame, get_server_info=dict)
        == b"%PDF-synthetic"
    )
    assert_quote_evidence(compiled[0])
    assert r"报价时间缺失 \& 待核实" in compiled[0]
    assert "报价缺口或字段不完整：4只" in compiled[0]
    assert "\r" not in compiled[0]
