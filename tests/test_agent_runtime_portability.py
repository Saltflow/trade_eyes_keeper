"""Use the actual native tool loop without importing the financial/Bot host."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_runtime import AgentRuntimeError, ConversationHarness


def settings():
    return SimpleNamespace(
        conversation_timeout_seconds=5,
        harness_backend="native",
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
                            "function": {"name": "read_evidence", "arguments": "{}"},
                        }
                    ],
                }
            assert "original body" in str(messages)
            return {"role": "assistant", "content": "evidence-based result"}

    client = Client()
    agent = ConversationHarness(client, settings(), retrieval_tools={"read_evidence"})
    events = []
    answer = agent.run(
        [{"role": "user", "content": "read"}],
        [{"function": {"name": "read_evidence"}}],
        lambda name, args: {"text": "original body"},
        lambda name, fields: events.append((name, fields)),
        lambda value: value,
        lambda answer: [],
        require_retrieval=True,
    )
    assert answer == "evidence-based result"
    assert client.calls == 2
    assert events[-1][1]["evidence_ready"]


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
