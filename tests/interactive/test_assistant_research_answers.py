"""Evidence admission and real Pi execution for factual research answers."""

import json
import os
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.interactive.assistant.client import AssistantAPIError
from src.interactive.assistant.harness import ConversationHarness
from src.interactive.assistant.settings import AssistantSettings


def tool(name="search_web"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Read evidence",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def request(name="search_web"):
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": "需要核对来源。",
        "tool_calls": [
            {
                "id": "read-1",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps({"query": "evidence"}),
                },
            }
        ],
    }


def answer(text="有来源的分析"):
    return {"role": "assistant", "content": text, "reasoning_content": "已核对。"}


def harness(root, replies):
    client = Mock()
    client.complete.side_effect = replies
    instance = ConversationHarness(
        client, AssistantSettings(max_tool_rounds=6), root, threading.Event()
    )
    return instance, client


def run(instance, invoke, audit, *, evidence_ready=False, require_retrieval=True):
    return instance.run(
        [{"role": "user", "content": "查询最新公告并分析"}],
        [tool(), tool("repository_map")],
        invoke,
        audit,
        lambda x: x,
        lambda _: [],
        require_retrieval=require_retrieval,
        evidence_ready=evidence_ready,
    )


def test_unsupported_answer_is_withheld_until_real_retrieval(
    tmp_path, stub_pi_transport
):
    bot, client = harness(tmp_path, [answer("未经检索的结论"), request(), answer()])
    invoke = Mock(return_value={"results": [{"url": "https://example.test/source"}]})
    audit = Mock()
    assert run(bot, invoke, audit) == "有来源的分析"
    assert client.complete.call_count == 3
    invoke.assert_called_once_with("search_web", {"query": "evidence"})
    assert any(c.args[0] == "retrieval_required" for c in audit.call_args_list)
    assert audit.call_args_list[0].args == ("harness_start", {"backend": "pi"})


def test_second_unsupported_answer_fails_without_delivery(tmp_path, stub_pi_transport):
    bot, client = harness(tmp_path, [answer("猜测一"), answer("猜测二")])
    invoke = Mock()
    with pytest.raises(AssistantAPIError, match="未完成资料检索"):
        run(bot, invoke, Mock())
    assert client.complete.call_count == 2
    invoke.assert_not_called()


def test_repository_map_does_not_satisfy_evidence_requirement(
    tmp_path, stub_pi_transport
):
    bot, _ = harness(tmp_path, [request("repository_map"), answer(), answer()])
    with pytest.raises(AssistantAPIError, match="未完成资料检索"):
        run(bot, Mock(return_value={"files": 12}), Mock())


def test_failed_search_is_given_back_to_model_for_limited_answer(
    tmp_path, stub_pi_transport
):
    bot, client = harness(tmp_path, [request(), answer("检索失败，无法核实最新公告。")])
    assert run(bot, Mock(side_effect=RuntimeError("HTTP 503")), Mock()) == (
        "检索失败，无法核实最新公告。"
    )
    messages = client.complete.call_args.args[0]
    assert any("实际尝试和失败" in m.get("content", "") for m in messages)
    assert any(m["role"] == "tool" and "503" in m["content"] for m in messages)


def test_current_prefetched_data_is_analyzed_without_duplicate_lookup(
    tmp_path, stub_pi_transport
):
    bot, client = harness(tmp_path, [answer()])
    invoke = Mock()
    assert run(bot, invoke, Mock(), evidence_ready=True) == "有来源的分析"
    assert client.complete.call_count == 1
    invoke.assert_not_called()


def test_pi_unavailable_does_not_fall_back_to_native(tmp_path, monkeypatch):
    bot, client = harness(tmp_path, [answer("不应发出")])
    monkeypatch.setattr(
        "src.interactive.assistant.harness.shutil.which", lambda _: None
    )
    with pytest.raises(AssistantAPIError, match="Pi harness 未安装完整"):
        run(bot, Mock(), Mock())
    client.complete.assert_not_called()


def test_mutating_tool_runtime_failure_retains_original_abort_semantics(tmp_path):
    bot, _ = harness(tmp_path, [request("propose_configuration")])
    invoke = Mock(side_effect=RuntimeError("write result uncertain"))
    with pytest.raises(RuntimeError, match="write result uncertain"):
        bot.run(
            [{"role": "user", "content": "生成配置提案"}],
            [tool("propose_configuration")],
            invoke,
            Mock(),
            lambda x: x,
            lambda _: [],
        )


def test_actual_pi_loop_performs_retrieval_then_analysis(monkeypatch):
    executable = os.environ.get("FEISHU_PI_TEST_NODE")
    if not executable:
        pytest.skip("Set FEISHU_PI_TEST_NODE for real Pi bridge acceptance")
    monkeypatch.setattr(
        "src.interactive.assistant.harness.shutil.which", lambda _: executable
    )
    root = Path(__file__).resolve().parents[2]
    bot, client = harness(root, [answer("先猜测"), request(), answer()])
    invoke = Mock(return_value={"results": [{"title": "fixture source"}]})
    audit = Mock()
    assert run(bot, invoke, audit) == "有来源的分析"
    assert client.complete.call_count == 3
    invoke.assert_called_once()
    assert bot._process is None
    finished = next(
        c.args[1] for c in audit.call_args_list if c.args[0] == "harness_finished"
    )
    assert finished["backend"] == "pi"
    assert finished["model_rounds"] == 3
    assert finished["retrieval_attempts"] == 1
