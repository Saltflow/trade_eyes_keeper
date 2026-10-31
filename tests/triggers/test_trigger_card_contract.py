"""Schema-only transport contract using real reminder/brief/daily builders.

All market rows below are explicitly synthetic unit fixtures. No report job,
optimizer, HTML, production profile or live delivery is run. The binding guard
is deliberately bypassed: authenticated SQLite/run binding has separate tests
in test_trigger_runtime.py; this file checks the exact card at the HTTP boundary.
"""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from src.notification.feishu_notifier import (
    _build_interactive_card,
    _build_native_table,
)
from src.triggers import delivery

REASON = "合同夹具：触发器 #3，季度报告披露当天"


def report_session():
    """Exercise all supplied unit rows; no business universe is selected."""
    rows = []
    for code, name, close in (
        ("601088", "合成A股", 40.0),
        ("00700", "合成港股", 50.0),
        ("VOO", "合成美股", 60.0),
    ):
        rows.append(
            {
                "stock_code": code,
                "stock_name": name,
                "date": pd.Timestamp.today().normalize(),
                "open": close - 1,
                "close": close,
                "high": close + 1,
                "low": close - 2,
                "ma60": close + 2,
                "wma20": close + 3,
                "wma30": close + 4,
                "wma50": close + 5,
                "volume": 10000,
                "amount": close * 10000,
                "dividend_per_share": 1.0,
                "dividend_yield": 2.0,
                "pe_ratio": 10.0,
                "pb_ratio": 1.0,
                "roe": 10.0,
                "amplitude": 3.0,
                "change_pct": -1.0,
                "turnover": 0.5,
                "rsi": 45.0,
                "macd_hist": 0.2,
                "vol_ratio": 1.1,
                "adx": 20.0,
                "boll_pct_b": 0.4,
            }
        )
    frame = pd.DataFrame(rows)
    return SimpleNamespace(
        get_all_dataframe=lambda: frame.copy(deep=True),
        get_alerts_as_dicts=list,
        signal_scan=None,
        backtest=None,
        portfolio_results=None,
        evaluation_reports={},
        placements={},
        dividend_events=[],
        announcements={
            "601088": [{"date": "2026-10-31", "title": "合成公告正文必须保留"}]
        },
        ref_portfolio_status={
            "a_share": {
                "_label": "合成参考组合",
                "holdings": [],
                "nav": 10000,
                "cash": 10000,
                "nav_return_pct": 0.0,
            }
        },
    )


@pytest.fixture
def schema_transport(monkeypatch):
    """Mock Feishu rejects forbidden v1 placement even when content is valid JSON."""
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        Mock(side_effect=AssertionError("network forbidden in card contract")),
    )
    notifier = delivery.TriggerChatNotifier(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "allowed_chat_ids": ["fixture-chat"],
                    "app_id": "fixture-app",
                    "app_secret": "fixture-secret",
                }
            },
            "_trigger_context": {
                "number": 3,
                "chat_id": "fixture-chat",
                "reason": REASON,
            },
        }
    )
    # Pure card/transport test, not an authenticated-binding acceptance test.
    monkeypatch.setattr(notifier, "_guard", Mock(return_value=None))
    originals, transmitted, violations = [], [], []
    real_post = notifier._post_card

    def tracked_post(payload):
        original = deepcopy(payload)
        originals.append(original["card"])
        try:
            return real_post(payload)
        finally:
            assert payload == original, (
                "transport must not mutate the builder's input card"
            )

    monkeypatch.setattr(notifier, "_post_card", tracked_post)

    def strict_http(url, **kwargs):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"code": 0, "tenant_access_token": "fixture-token"},
            )
        assert url == delivery.FEISHU_API + "/im/v1/messages?receive_id_type=chat_id"
        assert kwargs["json"]["receive_id"] == "fixture-chat"
        assert kwargs["json"]["msg_type"] == "interactive"
        card = json.loads(kwargs["json"]["content"])
        transmitted.append(card)
        errors = []
        if card.get("schema") != "2.0":
            errors.append("schema 2.0 required")
        if "elements" in card:
            errors.append("schema 2.0 forbids top-level elements; use body.elements")
        if not isinstance(card.get("body"), dict) or not isinstance(
            card["body"].get("elements"), list
        ):
            errors.append("body.elements must be an array")
        reason = notifier.binding.get("reason", "")
        if reason and card.get("body", {}).get("elements", [None])[0] != {
            "tag": "markdown",
            "content": reason,
        }:
            errors.append("trigger reason must be body.elements[0]")
        violations.extend(errors)
        return SimpleNamespace(
            status_code=400 if errors else 200,
            json=lambda: {"code": 0, "data": {"message_id": "fixture-message"}},
        )

    http = Mock(side_effect=strict_http)
    monkeypatch.setattr(delivery.requests, "post", http)
    return SimpleNamespace(
        notifier=notifier,
        originals=originals,
        transmitted=transmitted,
        violations=violations,
        http=http,
    )


def assert_preserved(transport, *, reason=REASON):
    assert transport.violations == []
    assert transport.transmitted
    assert len(transport.originals) == len(transport.transmitted)
    for original, sent in zip(transport.originals, transport.transmitted):
        expected = deepcopy(original)
        if reason:
            expected["body"]["elements"].insert(
                0, {"tag": "markdown", "content": reason}
            )
        assert sent == expected, (
            "reason insertion must preserve header, full body and native tables"
        )


def test_real_card_builder_uses_schema2_body_for_markdown_and_table():
    table = _build_native_table(
        [("code", "代码", "left", "text")], [{"code": "fixture"}]
    )
    card = _build_interactive_card("合同标题", "合同正文", extra_elements=[table])
    assert card["schema"] == "2.0" and "elements" not in card
    assert card["body"]["elements"] == [
        {"tag": "markdown", "content": "合同正文"},
        table,
    ]


@pytest.mark.parametrize("kind", ["reminder", "brief", "daily"])
def test_real_report_builders_reach_strict_schema2_http_boundary(
    schema_transport, kind
):
    transport = schema_transport
    notifier = transport.notifier
    session = report_session()
    if kind == "reminder":
        receipt = notifier.send_reminder("合成逆回购到期正文")
    elif kind == "brief":
        receipt = notifier.send_brief_report(session, {"label": "合同完整简报"})
    else:
        receipt = notifier.send_daily_report_from_session(session)
    assert_preserved(transport)
    assert receipt["status"] == "sent", receipt
    blob = json.dumps(transport.transmitted, ensure_ascii=False)
    if kind == "reminder":
        assert "合成逆回购到期正文" in blob
    else:
        tables = [
            element
            for card in transport.transmitted
            for element in card["body"]["elements"]
            if element["tag"] == "table"
        ]
        assert tables
        assert all(
            code in json.dumps(tables, ensure_ascii=False)
            for code in ("601088", "00700", "VOO")
        )
        assert "合成参考组合" in blob
    if kind == "daily":
        assert len(transport.transmitted) >= 4
        assert all(
            label in blob
            for label in ("价格", "基本面", "技术", "合成公告正文必须保留")
        )


def test_reused_input_card_is_immutable_and_reason_never_accumulates(schema_transport):
    transport = schema_transport
    extra = _build_native_table(
        [("code", "代码", "left", "text")], [{"code": "fixture"}]
    )
    card = _build_interactive_card("共享卡片", "原有正文", extra_elements=[extra])
    original = deepcopy(card)
    payload = {"msg_type": "interactive", "card": card}
    first = transport.notifier._post_card(payload)
    second = transport.notifier._post_card(payload)
    assert card == original
    assert_preserved(transport)
    assert first["status"] == second["status"] == "sent"
    assert transport.transmitted[0] == transport.transmitted[1]


def test_empty_reason_keeps_real_builder_card_unchanged(schema_transport):
    transport = schema_transport
    transport.notifier.binding["reason"] = ""
    card = _build_interactive_card("无需附加原因", "原有正文")
    receipt = transport.notifier._post_card({"card": card})
    assert_preserved(transport, reason="")
    assert receipt["status"] == "sent"
