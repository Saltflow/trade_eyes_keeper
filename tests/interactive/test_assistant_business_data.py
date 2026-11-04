"""Business reads use authenticated adapters and the existing Pi tool loop."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.interactive.assistant.data_tools import DATA_GUIDE
from src.interactive.assistant.harness import ConversationHarness
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.triggers.snapshots import capture_report_snapshot


@pytest.fixture
def business_bot(tmp_path):
    active = []

    def create(capabilities=frozenset({"personal"})):
        root = tmp_path / str(len(active))
        root.mkdir()
        runtime = ExecutionRuntime(ExecutionStore(root / "assistant.sqlite3"))
        bot = FeishuAssistant(
            {"storage": {"data_dir": str(root / "data")}, "stocks": ["01339"]},
            Mock(return_value=(True, "ok")),
            project_root=root,
            client=Mock(),
            configuration=Mock(),
            research=Mock(),
            store=ProposalStore(runtime.store.path),
            execution_runtime=runtime,
            start_workers=False,
            capabilities=capabilities,
        )
        bot.harness.run = Mock(return_value="已说明查询范围。")
        active.append((bot, runtime))
        return bot

    yield create
    for bot, runtime in active:
        bot.stop()
        runtime.stop(timeout=3)


@pytest.mark.parametrize("capabilities", [{"personal"}, {"triggers"}, {"research"}, None])
def test_business_tools_are_available_in_each_capability_scope(
    business_bot, capabilities
):
    bot = business_bot(capabilities)
    names = {tool["function"]["name"] for tool in bot.tool_schema()}
    assert {"data_find", "data_read"} <= names


@pytest.mark.parametrize("text", ["你好", "查一下公开业务数据"])
def test_each_conversation_includes_business_reading_guide(business_bot, text):
    bot = business_bot()
    bot.handle("chat", "owner", text, "guide")
    messages = bot.harness.run.call_args.args[0]
    assert DATA_GUIDE in messages[0]["content"]


def test_fundamental_as_of_schema_accepts_and_forwards_a_real_date(business_bot):
    bot = business_bot(None)
    schema = next(
        tool["function"] for tool in bot.tool_schema()
        if tool["function"]["name"] == "query_stock_fundamentals"
    )
    pattern = schema["parameters"]["properties"]["as_of"]["pattern"]
    assert re.fullmatch(pattern, "2026-11-04")
    bot.research.query_stock_fundamentals.return_value = {"selected": None}
    bot.call_tool("chat", "owner", "query_stock_fundamentals", {
        "code": "BRK.B", "as_of": "2026-11-04",
    })
    bot.research.query_stock_fundamentals.assert_called_once_with(
        "BRK.B", as_of="2026-11-04", dataset_id=None, _allow_backfill=False,
    )
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        bot.call_tool("chat", "owner", "query_stock_fundamentals", {
            "code": "BRK.B", "as_of": "2026/11/04",
        })


def test_personal_read_binds_identity_and_retains_only_authenticated_positions(
    business_bot,
):
    bot = business_bot()
    context = {
        "sender": "owner", "chat": "chat",
        "id": bot.store.begin_turn("chat", "owner", "private-read", "读取个人数据"),
    }
    bot._turn_context.current = context
    bot.personal.invoke = Mock(return_value={
        "positions": [{"code": "01339", "quantity": 100}],
        "owner": "private-identity", "chat_id": "private-chat",
    })
    result = bot.call_tool("chat", "owner", "data_read", {
        "data_id": "personal:status",
    })
    bot.personal.invoke.assert_called_once_with("owner", "chat", "status", {}, context)
    assert result["items"][0]["positions"][0]["quantity"] == 100
    assert "private-identity" not in json.dumps(result)
    assert "private-chat" not in json.dumps(result)
    for chat, owner in (("other-chat", "owner"), ("chat", "other-owner")):
        with pytest.raises(PermissionError, match="认证"):
            bot.call_tool(chat, owner, "data_read", {"data_id": "personal:status"})
    with pytest.raises(TypeError):
        bot.call_tool("chat", "owner", "data_find", {"owner": "other-owner"})


def test_research_only_capability_cannot_read_personal_monitor(business_bot):
    bot = business_bot({"research"})
    with pytest.raises(PermissionError, match="个人数据"):
        bot.call_tool("chat", "owner", "data_read", {"data_id": "personal:status"})


def test_report_execution_reads_respect_snapshot_owner_and_chat(business_bot):
    bot = business_bot()
    runs = {}
    for name, binding in (
        ("public", None),
        ("own", {"owner": "owner", "chat_id": "chat"}),
        ("foreign_owner", {"owner": "other", "chat_id": "chat"}),
        ("foreign_chat", {"owner": "owner", "chat_id": "other"}),
    ):
        snapshot = {"email": {"password": "must-never-leak"}}
        if binding:
            snapshot["_trigger_context"] = binding
        runs[name] = bot.execution_runtime.store.submit(
            "reports.daily", {"config_snapshot": snapshot}, source="test",
        )
    found = bot.call_tool("chat", "owner", "data_find", {"domain": "executions"})
    ids = {item["data_id"] for item in found["items"]}
    assert ids == {"report_run:" + runs[name] for name in ("public", "own")}
    for name in ("public", "own"):
        result = bot.call_tool("chat", "owner", "data_read", {
            "data_id": "report_run:" + runs[name],
        })
        assert result["items"][0]["run_id"] == runs[name]
        assert "config_snapshot" not in json.dumps(result)
        assert "must-never-leak" not in json.dumps(result)
    for name in ("foreign_owner", "foreign_chat"):
        with pytest.raises(PermissionError):
            bot.call_tool("chat", "owner", "data_read", {
                "data_id": "report_run:" + runs[name],
            })


def _tool_call(name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call-" + name,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def test_real_pi_reads_public_dividend_then_creates_once_daily_rule(
    business_bot, real_pi_bridge
):
    bot = business_bot()
    capture_report_snapshot(bot._fundamental_config, {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dividend_schedule": {
            "events": [{"code": "01339", "payment_date": "2026-11-06",
                        "source": "fixture_public_schedule"}],
            "monitored_symbols": ["01339"],
            "complete": False,
            "coverage": "fixture_not_live_data",
        },
    }, "daily")
    actual = ConversationHarness(
        bot.client, bot.settings, Path(__file__).resolve().parents[2], bot._stop,
    )
    actual.node_executable = os.environ.get("FEISHU_PI_TEST_NODE")
    bot.harness.run = actual.run

    def read_after_find(messages, _tools, **_kwargs):
        assert "dividend:public" in json.dumps(messages, ensure_ascii=False)
        return _tool_call("data_read", {"data_id": "dividend:public"})

    def create_after_read(messages, _tools, **_kwargs):
        assert "2026-11-06" in json.dumps(messages, ensure_ascii=False)
        return _tool_call("trigger_manage", {"action": "create", "spec": {
            "label": "监控池下一次公开派息日",
            "event": {"kind": "dividend.next_payment"},
            "actions": [{"kind": "daily"}],
        }})

    replies = iter([
        _tool_call("data_find", {"domain": "dividends"}),
        read_after_find, create_after_read,
        {"role": "assistant", "content": "已按公开派息日创建完整日报触发器。"},
    ])

    def complete(messages, tools, **kwargs):
        reply = next(replies)
        return reply(messages, tools, **kwargs) if callable(reply) else reply

    bot.client.complete.side_effect = complete
    bot.handle("chat", "owner", "监控池下一笔公开派息日触发完整日报", "pi-dividend")
    rules = bot.trigger_store.list("owner")
    assert len(rules) == 1
    spec = rules[0]["spec"]
    assert spec["event"]["kind"] == "dividend.next_payment"
    assert spec["match_policy"] == "once"
    assert spec["actions"][0]["kind"] == "daily"
    assert spec["actions"][0]["payload"]["chat_id"] == "chat"
    assert bot.execution_runtime.store.list_runs(job_id="reports.daily") == []
    assert bot.client.complete.call_count == 4
    assert "已保存触发器 1" in bot.send_message.call_args.args[1]
    assert actual._process is None
