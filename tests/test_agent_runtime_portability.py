"""Use the actual Pi loop without importing the financial/Bot host."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_runtime import AgentRuntimeError, ConversationHarness


def settings():
    return SimpleNamespace(
        conversation_timeout_seconds=5,
        harness_backend="pi",
        max_tool_rounds=3,
        max_tool_calls=3,
        max_context_chars=10000,
        model="offline-boundary",
        reasoning_effort="high",
    )


def test_agent_does_not_need_project_or_financial_imports():
    program = (
        "import sys; from src.agent_runtime import ConversationHarness; "
        "assert not any(n.startswith(('src.interactive','src.research_pipeline',"
        "'src.fundamental_data','scripts.')) for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_host_supplies_tool_evidence_policy_and_model_port():
    class Client:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {"name": "read_evidence", "arguments": "{}"},
                        }
                    ],
                }
            assert "original body" in str(messages)
            return {"role": "assistant", "content": "evidence-based result"}

    client = Client()
    config = settings()
    # This successful protocol round trip starts the real Node bridge. Allow
    # startup under suite load; short deadline tests keep their own budgets.
    config.conversation_timeout_seconds = 30
    agent = ConversationHarness(
        client,
        config,
        bridge_path=Path(__file__).resolve().parents[1] / "tools/feishu-pi/bridge.mjs",
        node_executable=os.environ.get("FEISHU_PI_TEST_NODE") or shutil.which("node"),
        retrieval_tools={"read_evidence"},
    )
    events = []
    answer = agent.run(
        [{"role": "user", "content": "read"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_evidence",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ],
        lambda name, args: {"text": "original body"},
        lambda name, fields: events.append((name, fields)),
        lambda value: value,
        lambda answer: [],
        require_retrieval=True,
    )
    assert answer == "evidence-based result"
    assert client.calls == 2
    assert events[-1][1]["evidence_ready"]


def test_native_configuration_is_rejected_before_any_model_call():
    config = settings()
    config.harness_backend = "native"
    with pytest.raises(AgentRuntimeError, match="仅支持 Pi"):
        ConversationHarness(object(), config)


def test_shared_pi_accepts_existing_research_context_and_tool_batch():
    body = (
        "调查原文" * 180000
    )  # Over 2 MiB in UTF-8, within the existing 1M char budget.

    class Client:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            assert any(m["role"] == "user" and m["content"] == body for m in messages)
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{n}",
                            "type": "function",
                            "function": {
                                "name": "read_evidence",
                                "arguments": f'{{"index":{n}}}',
                            },
                        }
                        for n in range(9)
                    ],
                }
            assert len([m for m in messages if m["role"] == "tool"]) == 9
            return {"role": "assistant", "content": "complete"}

    config = settings()
    config.max_context_chars = 1000000
    config.max_tool_calls = 64
    config.conversation_timeout_seconds = 15
    invoked = []
    client = Client()
    agent = ConversationHarness(
        client,
        config,
        bridge_path=Path(__file__).resolve().parents[1] / "tools/feishu-pi/bridge.mjs",
        node_executable=os.environ.get("FEISHU_PI_TEST_NODE") or shutil.which("node"),
    )
    answer = agent.run(
        [{"role": "user", "content": body}],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_evidence",
                    "parameters": {
                        "type": "object",
                        "properties": {"index": {"type": "integer"}},
                        "required": ["index"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        lambda name, args: invoked.append(args["index"]) or {"ok": True},
        lambda *args: None,
        lambda value: value,
        lambda _: [],
    )
    assert answer == "complete"
    assert invoked == list(range(9)) and client.calls == 2


def test_pi_without_explicit_bridge_fails_visibly():
    class Client:
        def complete(self, messages, tools):
            pytest.fail("missing Pi bridge must not fall back to a model call")

    config = settings()
    config.harness_backend = "pi"
    with pytest.raises(AgentRuntimeError, match="Pi harness"):
        ConversationHarness(Client(), config).run(
            [], [], lambda *args: None, lambda *args: None, lambda x: x, lambda _: []
        )
