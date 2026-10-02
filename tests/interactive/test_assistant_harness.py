"""Agent-loop budgets, actual Pi subprocess protocol and cancellation."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.interactive.assistant.client import AssistantAPIError
from src.interactive.assistant.harness import ConversationHarness, compact_messages
from src.interactive.assistant.settings import AssistantSettings

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_repository_file",
            "description": "Read approved source",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]


def call(path="src/example.py", name="read_repository_file"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "test-call",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps({"path": path})},
            }
        ],
    }


def run_loop(
    tmp_path, replies, *, backend="native", invoke=None, check=None, **settings
):
    client = Mock()
    client.complete.side_effect = replies
    harness = ConversationHarness(
        client,
        AssistantSettings(harness_backend=backend, **settings),
        tmp_path,
        threading.Event(),
    )
    invoke = invoke or Mock(return_value={"path": "src/example.py", "text": "example"})
    audit = Mock()
    answer = harness.run(
        [{"role": "user", "content": "Explain source"}],
        TOOLS,
        invoke,
        audit,
        lambda x: x,
        check or (lambda _: []),
    )
    return answer, client, invoke, audit, harness


def test_native_reads_then_answers(tmp_path):
    answer, client, invoke, _, _ = run_loop(
        tmp_path, [call(), {"role": "assistant", "content": "answer"}]
    )
    assert answer == "answer"
    invoke.assert_called_once_with("read_repository_file", {"path": "src/example.py"})
    assert client.complete.call_count == 2
    assert client.complete.call_args.args[0][-1]["role"] == "tool"


def test_last_round_is_reserved_for_no_tool_summary(tmp_path):
    answer, client, invoke, _, _ = run_loop(
        tmp_path,
        [call(), {"role": "assistant", "content": "partial evidence"}],
        max_tool_rounds=2,
    )
    assert answer == "partial evidence"
    assert client.complete.call_args.args[1] == []
    invoke.assert_called_once()


def test_unknown_tool_and_budget_cannot_execute(tmp_path):
    invoke = Mock()
    with pytest.raises(AssistantAPIError, match="预算"):
        run_loop(tmp_path, [call(name="bash")], invoke=invoke, max_tool_rounds=1)
    invoke.assert_not_called()


def test_citation_failure_is_repaired_before_delivery(tmp_path):
    check = Mock(side_effect=[[{"citation": "unknown.py:100"}], []])
    answer, client, _, audit, _ = run_loop(
        tmp_path,
        [
            {"role": "assistant", "content": "unknown.py:100"},
            {"role": "assistant", "content": "correct evidence"},
        ],
        check=check,
    )
    assert answer == "correct evidence"
    assert client.complete.call_count == 2
    assert any(c.args[0] == "citation_rejected" for c in audit.call_args_list)


def test_repeated_bad_citations_fail_closed(tmp_path):
    with pytest.raises(AssistantAPIError, match="引用未通过"):
        run_loop(
            tmp_path,
            [{"role": "assistant", "content": "bad"}] * 2,
            check=lambda _: [{"citation": "bad.py:1"}],
        )


def test_context_compaction_preserves_tool_pairs_and_valid_json():
    messages = [
        {"role": "user", "content": "question"},
        call(),
        {"role": "tool", "tool_call_id": "test-call", "content": "x" * 20000},
    ]
    compacted = compact_messages(messages, 5000)
    assert compacted[1] == messages[1]
    assert compacted[2]["tool_call_id"] == "test-call"
    assert json.loads(compacted[2]["content"])["compacted"] is True
    assert len(messages[2]["content"]) == 20000


def test_unshrinkable_context_fails_without_truncating_user_or_code():
    with pytest.raises(AssistantAPIError, match="上下文"):
        compact_messages([{"role": "user", "content": "x" * 20000}], 1000)


@pytest.fixture
def pi_node(monkeypatch):
    path = os.environ.get("FEISHU_PI_TEST_NODE")
    if not path:
        pytest.skip("Set FEISHU_PI_TEST_NODE to a Node >=22.19 executable")
    monkeypatch.setattr(
        "src.interactive.assistant.harness.shutil.which", lambda _: path
    )
    return path


def test_real_pi_bridge_round_trip_and_no_credential_environment(pi_node, monkeypatch):
    import subprocess

    actual_popen = subprocess.Popen
    environments = []

    def capture(*args, **kwargs):
        environments.append(kwargs["env"])
        return actual_popen(*args, **kwargs)

    monkeypatch.setattr("src.interactive.assistant.harness.subprocess.Popen", capture)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "credential-must-stay-in-python")
    monkeypatch.setenv("NODE_OPTIONS", "--invalid-ambient-option")
    root = Path(__file__).resolve().parents[2]
    answer, client, invoke, _, harness = run_loop(
        root, [call(), {"role": "assistant", "content": "Pi final"}], backend="pi"
    )
    assert answer == "Pi final"
    assert client.complete.call_count == 2
    invoke.assert_called_once()
    assert "DEEPSEEK_API_KEY" not in environments[0]
    assert "NODE_OPTIONS" not in environments[0]
    assert harness._process is None


def test_pi_stop_cleans_owned_process(pi_node, tmp_path):
    bridge = tmp_path / "tools/feishu-pi/bridge.mjs"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("setInterval(() => {}, 1000);", encoding="utf-8")
    stopped = threading.Event()
    harness = ConversationHarness(
        Mock(), AssistantSettings(harness_backend="pi"), tmp_path, stopped
    )
    errors = []

    def work():
        try:
            harness.run([], TOOLS, Mock(), Mock(), lambda x: x, lambda _: [])
        except AssistantAPIError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=work)
    worker.start()
    deadline = time.monotonic() + 5
    while harness._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    process = harness._process
    assert process is not None
    stopped.set()
    harness.stop()
    worker.join(5)
    assert not worker.is_alive()
    assert process.poll() is not None
    assert errors
