"""Offline render/receipt contracts; synthetic rows, no report job or delivery."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

from src.notification.feishu_notifier import FeishuNotifier
from src.notification.telegram_notifier import TelegramNotifier


class ReportSession:
    def __init__(self):
        self.created_at = datetime(2026, 11, 19, tzinfo=timezone.utc)
        self.config = {}
        self.placements = {}
        self.dividend_events = []
        self.announcements = {
            "600036": [{"date": "2026-11-19", "title": "合成事件应完整保留"}],
        }

    @property
    def strategy_return_curves(self):
        raise AssertionError("retired curve data must never be accessed")

    def get_all_dataframe(self):
        return pd.DataFrame(
            [
                {
                    "stock_code": "600036",
                    "stock_name": "合成A",
                    "close": 12.3,
                    "quote_status": "last_close",
                    "quote_source_time": "2026-11-18T15:00:00+08:00",
                },
                {
                    "stock_code": "00700",
                    "stock_name": "合成H",
                    "close": None,
                    "quote_status": "unavailable",
                    "quote_reason": "合成缺口",
                },
            ]
        )


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize("kind", ["daily", "brief"])
def test_price_rows_and_events_send_without_accessing_curve_data(
    monkeypatch, channel, kind
):
    unexpected = Mock(side_effect=AssertionError("external transport forbidden"))
    monkeypatch.setattr("requests.sessions.Session.request", unexpected)
    notifier = FeishuNotifier({}) if channel == "feishu" else TelegramNotifier({})
    payloads = []

    def accepted(*args, **kwargs):
        payloads.append({"args": args, "kwargs": kwargs})
        return {
            "status": "sent",
            "reason": "provider_accepted",
            "message_id": len(payloads),
        }

    monkeypatch.setattr(notifier, "_send", accepted)
    if channel == "feishu":
        monkeypatch.setattr(notifier, "_send_card", accepted)
    receipt = (
        notifier.send_daily_report_from_session(ReportSession())
        if kind == "daily"
        else notifier.send_brief_report(ReportSession(), {"label": "合成简报"})
    )
    assert receipt["status"] == "sent", receipt
    blob = json.dumps(payloads, ensure_ascii=False)
    assert all(code in blob for code in ("600036", "00700"))
    assert "—" in blob
    assert not any(text in blob for text in ("收益曲线", "Ke策略", "curve"))
    if kind == "daily":
        assert "合成事件应完整保留" in blob
        assert [piece["label"] for piece in receipt["pieces"]] == [
            "价格与基本面",
            "事件速览",
        ]
        assert [piece["message_id"] for piece in receipt["pieces"]] == [1, 2]
    assert len(payloads) == (2 if kind == "daily" else 1)
    unexpected.assert_not_called()


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_failed_first_section_keeps_only_real_remaining_section_receipt(
    monkeypatch, channel, status
):
    notifier = FeishuNotifier({}) if channel == "feishu" else TelegramNotifier({})
    send = Mock(
        return_value={"status": status, "reason": "controlled_transport_failure"}
    )
    monkeypatch.setattr(notifier, "_send", send)
    receipt = notifier.send_daily_report_from_session(ReportSession())
    assert receipt["status"] == status
    assert [piece["status"] for piece in receipt["pieces"]] == [status, "skipped"]
    assert [piece["label"] for piece in receipt["pieces"]] == [
        "价格与基本面",
        "事件速览",
    ]
    assert receipt["pieces"][1]["attempted"] is False
    send.assert_called_once()


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
def test_event_preparation_failure_remains_visible_without_curve_placeholder(
    monkeypatch, channel
):
    notifier = FeishuNotifier({}) if channel == "feishu" else TelegramNotifier({})
    monkeypatch.setattr(
        "src.notification.report_builders.build_daily_events_text",
        Mock(side_effect=ValueError("controlled malformed event")),
    )
    send = Mock(return_value={"status": "sent", "reason": "provider_accepted"})
    monkeypatch.setattr(notifier, "_send", send)
    receipt = notifier.send_daily_report_from_session(ReportSession())
    assert receipt["status"] == "partial"
    assert [piece["label"] for piece in receipt["pieces"]] == [
        "价格与基本面",
        "事件速览",
    ]
    assert receipt["pieces"][1]["status"] == "failed"
    assert receipt["pieces"][1]["reason"] == "preparation_failed"
    assert receipt["pieces"][1]["attempted"] is False
    send.assert_called_once()


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
def test_no_events_means_one_actual_price_section(monkeypatch, channel):
    notifier = FeishuNotifier({}) if channel == "feishu" else TelegramNotifier({})
    session = ReportSession()
    session.announcements = {}
    send = Mock(return_value={"status": "sent", "reason": "provider_accepted"})
    monkeypatch.setattr(notifier, "_send", send)
    receipt = notifier.send_daily_report_from_session(session)
    assert receipt["status"] == "sent"
    assert [piece["label"] for piece in receipt["pieces"]] == ["价格与基本面"]
    send.assert_called_once()
