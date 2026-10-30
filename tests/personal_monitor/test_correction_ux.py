"""Authenticated corrections and reversible holdings through actual tool APIs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

from src.interactive.assistant.personal_tools import (
    PersonalTools,
    history_only_action,
    history_requested,
)
from src.personal_monitor import MonitorStore

ZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def personal(tmp_path):
    store = MonitorStore(tmp_path / "positions.sqlite3")
    store.ensure_account("owner", "account")
    return PersonalTools(store)


def context(message_id, text, *, when=None):
    return {
        "id": message_id,
        "message_id": message_id,
        "original_text": text,
        "effective_time": when or datetime.now(ZONE),
    }


def test_same_day_correction_is_not_rejected_as_midnight(personal):
    now = datetime.now(ZONE)
    earlier = now - timedelta(minutes=5)
    personal.store.import_snapshot(
        "owner",
        "account",
        [{"instrument": "00728", "quantity": 2000}],
        earlier.isoformat(),
        0,
        "manual",
    )
    result = personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "account",
            "as_of": now.date().isoformat(),
            "expected_revision": 1,
            "rows": [{"instrument": "00728", "quantity": 4000}],
        },
        context("correct", "账户account，00728 当前4000股，之前数量作废", when=now),
    )["result"]
    assert result["applied"] == ["00728"]
    state = personal.store.status("owner")["accounts"][0]
    assert state["positions"][0]["quantity"] == 4000
    assert datetime.fromisoformat(state["positions"][0]["as_of"]) == now


def test_complete_authenticated_correction_survives_waiting_for_repair(personal):
    now = datetime.now(ZONE)
    source_time = now - timedelta(minutes=25)
    personal.remember_user_message(
        "owner",
        "chat",
        "original-correction",
        "账户account，00728 当前4000股，之前数量作废",
        source_time,
    )
    result = personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "account",
            "expected_revision": 0,
            "rows": [{"instrument": "00728", "quantity": 4000}],
            "source_message_ids": ["original-correction"],
        },
        context("continue", "按刚才更正执行", when=now),
    )["result"]
    assert result["applied"] == ["00728"]
    row = personal.store.status("owner")["accounts"][0]["positions"][0]
    assert datetime.fromisoformat(row["as_of"]) == source_time
    assert row["manual_evidence"]["quantity_message_id"] == "original-correction"


def test_waiting_correction_cannot_overwrite_a_newer_update(personal):
    now = datetime.now(ZONE)
    source_time = now - timedelta(minutes=25)
    personal.remember_user_message(
        "owner",
        "chat",
        "original-correction",
        "账户account，00728 当前4000股，之前数量作废",
        source_time,
    )
    personal.store.import_snapshot(
        "owner",
        "account",
        [{"instrument": "00728", "quantity": 5000}],
        (now - timedelta(minutes=1)).isoformat(),
        0,
        "manual",
    )
    result = personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "account",
            "expected_revision": 1,
            "rows": [{"instrument": "00728", "quantity": 4000}],
            "source_message_ids": ["original-correction"],
        },
        context("continue", "按刚才更正执行", when=now),
    )["result"]
    assert result["applied"] == []
    assert result["rejected"][0]["reason"] == "older_account_snapshot"
    assert (
        personal.store.status("owner")["accounts"][0]["positions"][0]["quantity"]
        == 5000
    )


def test_referenced_historical_date_is_not_promoted_to_message_time(personal):
    now = datetime.now(ZONE)
    personal.remember_user_message(
        "owner",
        "chat",
        "history-fact",
        "账户account，2020-01-01 的00728持仓4000股",
        now,
    )
    personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "account",
            "expected_revision": 0,
            "rows": [{"instrument": "00728", "quantity": 4000}],
            "source_message_ids": ["history-fact"],
        },
        context("history", "按刚才记录执行", when=now),
    )
    row = personal.store.status("owner")["accounts"][0]["positions"][0]
    assert (
        datetime.fromisoformat(row["as_of"]).astimezone(ZONE).date().isoformat()
        == "2020-01-01"
    )


def test_omitted_tool_date_cannot_hide_future_date_in_human_source(personal):
    now = datetime.now(ZONE)
    personal.remember_user_message(
        "owner",
        "chat",
        "future-fact",
        "账户account，2099-01-01 的00728持仓4000股",
        now,
    )
    with pytest.raises(ValueError, match="未来"):
        personal.invoke(
            "owner",
            "chat",
            "import_manual",
            {
                "account": "account",
                "expected_revision": 0,
                "rows": [{"instrument": "00728", "quantity": 4000}],
                "source_message_ids": ["future-fact"],
            },
            context("future", "按刚才记录执行", when=now),
        )
    assert personal.store.status("owner")["accounts"][0]["positions"] == []


def test_tool_undo_redo_and_receipt_use_actual_quantities(personal):
    initial = context("initial", "账户account，00728 持仓2000股")
    personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "account",
            "expected_revision": 0,
            "rows": [{"instrument": "00728", "quantity": 2000}],
        },
        initial,
    )
    correction = context("correct", "账户account，00728 当前4000股，之前数量作废")
    changed = personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "account",
            "expected_revision": 1,
            "rows": [{"instrument": "00728", "quantity": 4000}],
        },
        correction,
    )["result"]
    assert changed["revision"] == 2
    assert "2000 → 4000" in personal.render_receipts(correction)
    assert "撤销上次持仓修改" in personal.render_receipts(correction)
    undo = context("undo", "撤销上次持仓修改")
    reversed_ = personal.invoke(
        "owner",
        "chat",
        "undo_positions",
        {"account": "account", "expected_revision": 2},
        undo,
    )["result"]
    assert reversed_["revision"] == 3
    assert "4000 → 2000" in personal.render_receipts(undo)
    redo = context("redo", "重做")
    repeated = personal.invoke(
        "owner",
        "chat",
        "redo_positions",
        {"account": "account", "expected_revision": 3},
        redo,
    )["result"]
    assert repeated["revision"] == 4
    assert (
        personal.store.status("owner")["accounts"][0]["positions"][0]["quantity"]
        == 4000
    )
    # Same authenticated platform message cannot execute another history action.
    duplicate = personal.invoke(
        "owner",
        "chat",
        "redo_positions",
        {"account": "account", "expected_revision": 4},
        context("redo", "重做"),
    )["result"]
    assert duplicate["duplicate"]
    assert personal.store.status("owner")["accounts"][0]["revision"] == 4


@pytest.mark.parametrize(
    "text",
    [
        "不要撤销",
        "不需要撤销上次持仓修改",
        "拒绝撤销持仓修改",
        "禁止撤销持仓修改",
        "如果撤销上次持仓修改",
        "怎么撤销持仓修改",
        "导入这张截图",
    ],
)
def test_undo_needs_actual_human_instruction(personal, text):
    with pytest.raises(ValueError, match="明确说"):
        personal.invoke(
            "owner",
            "chat",
            "undo_positions",
            {"account": "account", "expected_revision": 0},
            context("no-undo", text),
        )


@pytest.mark.parametrize(
    ("text", "action", "expected"),
    [
        ("帮我撤销刚才的持仓修改", "undo_positions", True),
        ("不要撤销，我要重做", "redo_positions", True),
        ("撤销上次持仓修改，不改提醒", "undo_positions", True),
        ("如果撤销只是例子", "undo_positions", False),
        ("如何重做", "redo_positions", False),
    ],
)
def test_history_intent_is_clause_scoped(text, action, expected):
    assert history_requested(text, action) is expected


def test_compact_status_keeps_holdings_and_user_facts_visible(personal):
    now = datetime.now(ZONE)
    personal.store.import_snapshot(
        "owner",
        "account",
        [
            {
                "instrument": "00728",
                "quantity": 2000,
                "field_sources": {"quantity": ["oversized-diagnostic" * 2000]},
                "quote_evidence": {"raw": "very large" * 2000},
            }
        ],
        now.isoformat(),
        0,
    )
    personal.remember_user_message(
        "owner",
        "chat",
        "fact",
        "账户account，00728 当前4000股",
        now,
    )
    state = personal.invoke(
        "owner", "chat", "status", {}, context("status", "查看持仓")
    )
    rendered = json.dumps(state, ensure_ascii=False)
    assert len(rendered) < 5000
    assert "00728" in rendered and "4000股" in rendered
    assert state["accounts"][0]["position_history"]["undo_available"] is False
    # Only the presentation is compacted; original audit data remains intact.
    assert personal.store.status("owner")["accounts"][0]["positions"][0][
        "field_sources"
    ]


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"raw": "very large" * 2000},
        {"raw": {"observations": ["nested diagnostic" * 1000] * 4}},
        {f"diagnostic_{index}": "large diagnostic" * 100 for index in range(40)},
        {"oversized_key" * 2000: "small value"},
    ],
)
def test_compact_quote_retains_verified_values_and_traceable_diagnostics(
    personal, diagnostics
):
    now = datetime.now(ZONE).isoformat()
    core = {
        "price": 50.2,
        "currency": "CNY",
        "provider_time": now,
        "source": "qq_raw_daily_close",
        "market_timezone": "Asia/Shanghai",
        "market_closed": True,
        "latest_trading_date": "2026-10-23",
        "session_close": "2026-10-23T15:00:00+08:00",
        "calendar_as_of": now,
        "session_verified": True,
    }
    original = {**core, **diagnostics}
    personal.store.import_snapshot(
        "owner",
        "account",
        [
            {
                "instrument": "601088",
                "quantity": 200,
                "price": 49.22,
                "currency": "CNY",
                "market_value": 9844,
                "quote_evidence": original,
            }
        ],
        now,
        0,
    )
    before = personal.store.status("owner")
    state = personal.invoke(
        "owner", "chat", "status", {}, context("status", "查看持仓")
    )
    row = state["accounts"][0]["positions"][0]
    assert (row["instrument"], row["quantity"], row["price"], row["market_value"]) == (
        "601088",
        200,
        49.22,
        9844,
    )
    quote = row["quote_evidence"]
    assert {key: quote[key] for key in core} == core
    summary = quote["diagnostics_summary"]
    encoded = json.dumps(
        diagnostics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert summary["sha256"] == hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    assert summary["characters"] == len(encoded)
    assert summary["preview"] == encoded[:320]
    assert summary["truncated"] is True
    assert 0 < len(summary["fields"]) <= 16
    assert all(len(field) <= 80 for field in summary["fields"])
    assert len(json.dumps(state, ensure_ascii=False)) < 5000
    assert personal.store.status("owner") == before
    assert before["accounts"][0]["positions"][0]["quote_evidence"] == original


def test_small_quote_diagnostics_keep_original_structure(personal):
    quote = {
        "price": 50.2,
        "currency": "CNY",
        "source": "qq_raw_daily_close",
        "provider_time": datetime.now(ZONE).isoformat(),
        "diagnostics": {"price_difference_pct": 1.95, "note": "raw unadjusted close"},
    }
    personal.store.import_snapshot(
        "owner",
        "account",
        [{"instrument": "601088", "quantity": 200, "quote_evidence": quote}],
        datetime.now(ZONE).isoformat(),
        0,
    )
    result = personal.invoke(
        "owner", "chat", "status", {}, context("small", "查看持仓")
    )
    assert result["accounts"][0]["positions"][0]["quote_evidence"] == quote


def correction_source(personal, *, text=None, age=25):
    now = datetime.now(ZONE)
    personal.store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "00728", "quantity": 2000},
            {"instrument": "01339", "quantity": 19000},
        ],
        (now - timedelta(hours=1)).isoformat(),
        0,
        "manual",
    )
    personal.remember_user_message(
        "owner",
        "chat",
        "full-correction",
        text
        or "账户account，H股更正为：00728 中国电信4000股、01339 中国人民保险集团20000股，之前那组数量作废。",
        now - timedelta(minutes=age),
    )
    return now


def test_validated_candidate_discovers_complete_facts_after_many_followups(personal):
    now = correction_source(personal)
    for index in range(15):
        personal.remember_user_message(
            "owner",
            "chat",
            f"followup-{index}",
            "处理到哪了",
            now - timedelta(seconds=20 - index),
        )
    before = personal.store.status("owner")["accounts"][0]
    result = personal.invoke(
        "owner", "chat", "status", {}, context("read", "按刚才更正执行", when=now)
    )
    (candidate,) = result["validated_manual_candidates"]
    assert candidate["applicable"] is True
    assert candidate["rows"] == [
        {"instrument": "00728", "quantity": 4000},
        {"instrument": "01339", "quantity": 20000},
    ]
    assert candidate["source_message_id"] == "full-correction"
    assert candidate["fact_time"] == (now - timedelta(minutes=25)).isoformat()
    assert len(result["authenticated_messages"]) == 10
    assert all(
        item["id"] != "full-correction" for item in result["authenticated_messages"]
    )
    assert personal.store.status("owner")["accounts"][0] == before
    changed = personal.invoke(
        "owner",
        "chat",
        "import_manual",
        candidate["tool_arguments"],
        context("resume", "按刚才更正执行", when=now),
    )["result"]
    assert changed["revision"] == 2
    assert len(changed["changes"]) == 2
    assert personal.store.position_history("owner", "account", chat_scope="chat")[
        "undo_available"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "账户account，00728 当前5000股，01339 当前22000股",
        "账户account，00728 5000股，01339 数量不清楚",
        "刚才那组更正作废",
        "不要按刚才更正执行",
    ],
)
def test_newer_uncommitted_fact_or_retraction_blocks_old_candidate(personal, text):
    now = correction_source(personal)
    source = personal.user_messages("owner", "chat")[0]
    old_args = {
        "account": "account",
        "expected_revision": 1,
        "rows": [
            {"instrument": "00728", "quantity": 4000},
            {"instrument": "01339", "quantity": 20000},
        ],
        "source_message_ids": [source["id"]],
    }
    personal.remember_user_message(
        "owner", "chat", "new-fact", text, now - timedelta(minutes=1)
    )
    with pytest.raises(ValueError, match="新的持仓事实或撤回"):
        personal.invoke(
            "owner",
            "chat",
            "import_manual",
            old_args,
            context("resume", "按刚才更正执行", when=now),
        )
    assert personal.store.status("owner")["accounts"][0]["revision"] == 1


@pytest.mark.parametrize("foreign", ["owner", "chat"])
def test_validated_candidates_never_cross_user_or_chat(personal, foreign):
    now = correction_source(personal)
    rows = personal.validated_manual_candidates(
        "other" if foreign == "owner" else "owner",
        "other-chat" if foreign == "chat" else "chat",
        personal.store.status("owner")["accounts"],
    )
    assert rows == []
    assert now


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("账户account，2099-01-01更正为00728持仓4000股", "future_fact_time"),
        ("账户account，2020-01-01更正为00728持仓4000股", "older_account_snapshot"),
    ],
)
def test_validated_candidates_do_not_promote_future_or_historical_time(
    personal, text, reason
):
    correction_source(personal, text=text)
    (candidate,) = personal.validated_manual_candidates(
        "owner", "chat", personal.store.status("owner")["accounts"]
    )
    assert candidate["applicable"] is False
    assert candidate["blocked_reason"] == reason


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("撤销上次持仓修改", "undo_positions"),
        ("帮我撤销刚才的持仓修改。", "undo_positions"),
        ("重做", "redo_positions"),
        ("请重做上次持仓修改", "redo_positions"),
        ("不要撤销上次持仓修改", None),
        ("撤销提醒", None),
        ("如何重做", None),
        ("撤销上次持仓修改，再把00728改成3000股", None),
    ],
)
def test_pure_history_commands_are_bounded(text, action):
    assert history_only_action(text) == action


@pytest.mark.parametrize("text", ["撤销上次持仓修改", "重做"])
@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("import_image", {"account": "account", "batch_id": "untrusted-image"}),
        (
            "import_manual",
            {
                "account": "account",
                "expected_revision": 0,
                "rows": [{"instrument": "00728", "quantity": 4000}],
            },
        ),
        ("create_reminder", {"text": "not authorized"}),
    ],
)
def test_history_turn_cannot_create_new_import_or_unrelated_write(
    personal, text, action, args
):
    before = personal.store.status("owner")
    with pytest.raises(ValueError, match="仅授权持仓历史操作"):
        personal.invoke("owner", "chat", action, args, context("history", text))
    assert personal.store.status("owner") == before


def test_correction_context_reaches_model_and_history_tools_are_restricted(tmp_path):
    from unittest.mock import Mock

    from src.interactive.assistant.service import FeishuAssistant

    client = Mock()
    assistant = FeishuAssistant(
        {"interactive": {"feishu": {"assistant": {"enabled": True}}}},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        client=client,
        configuration=Mock(),
        research=Mock(),
        capabilities={"personal"},
        start_workers=False,
    )
    assistant.personal.store.ensure_account("owner", "account")
    correction_source(assistant.personal)
    observed = []

    def complete(messages, tools, **kwargs):
        statuses = [
            json.loads(item["content"])
            for item in messages
            if item.get("role") == "tool"
            and item.get("tool_call_id") == "personal_correction_context"
        ]
        assert len(statuses) == 1
        observed.append({tool["function"]["name"] for tool in tools})
        assert (
            statuses[0]["validated_manual_candidates"][0]["rows"][0]["quantity"] == 4000
        )
        return {"role": "assistant", "content": "没有可重做的持仓历史。"}

    client.complete.side_effect = complete
    try:
        assistant.handle("chat", "owner", "重做", "history-only")
        assert observed == [
            {"personal_monitor_status", "personal_monitor_redo_positions"}
        ]
        assert assistant.personal.store.status("owner")["accounts"][0]["revision"] == 1
    finally:
        assistant.stop()
