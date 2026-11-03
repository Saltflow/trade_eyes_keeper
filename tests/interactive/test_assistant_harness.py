"""Agent-loop budgets, actual Pi subprocess protocol and cancellation."""

import json
import threading
from unittest.mock import Mock

import pytest

from src.interactive.assistant.client import AssistantAPIError
from src.interactive.assistant.harness import ConversationHarness, compact_messages
from src.interactive.assistant.settings import AssistantSettings

pytestmark = pytest.mark.usefixtures("stub_pi_transport")

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
    tmp_path, replies, *, backend="pi", invoke=None, check=None, tools=TOOLS, **settings
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
        tools,
        invoke,
        audit,
        lambda x: x,
        check or (lambda _: []),
    )
    return answer, client, invoke, audit, harness


def test_pi_reads_then_answers(tmp_path):
    answer, client, invoke, _, _ = run_loop(
        tmp_path, [call(), {"role": "assistant", "content": "answer"}]
    )
    assert answer == "answer"
    invoke.assert_called_once_with("read_repository_file", {"path": "src/example.py"})
    assert client.complete.call_count == 2
    assert client.complete.call_args.args[0][-1]["role"] == "tool"


def test_old_native_setting_requires_explicit_configuration_change():
    with pytest.raises(ValueError, match="harness_backend.*pi"):
        AssistantSettings.from_config(
            {"interactive": {"feishu": {"assistant": {"harness_backend": "native"}}}}
        )


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


def test_real_pi_bridge_round_trip_and_no_credential_environment(tmp_path, monkeypatch):
    import subprocess

    actual_popen = subprocess.Popen
    environments = []

    def capture(*args, **kwargs):
        environments.append(kwargs["env"])
        return actual_popen(*args, **kwargs)

    monkeypatch.setattr("src.interactive.assistant.harness.subprocess.Popen", capture)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "credential-must-stay-in-python")
    monkeypatch.setenv("NODE_OPTIONS", "--invalid-ambient-option")
    answer, client, invoke, _, harness = run_loop(
        tmp_path, [call(), {"role": "assistant", "content": "Pi final"}]
    )
    assert answer == "Pi final"
    assert client.complete.call_count == 2
    invoke.assert_called_once()
    assert "DEEPSEEK_API_KEY" not in environments[0]
    assert "NODE_OPTIONS" not in environments[0]
    assert harness._process is None


def test_pi_stop_cleans_owned_process(tmp_path):
    stopped = threading.Event()
    model_entered = threading.Event()
    release_model = threading.Event()

    def model(*args):
        model_entered.set()
        assert release_model.wait(5)
        return {"role": "assistant", "content": "stopped before delivery"}

    client = Mock()
    client.complete.side_effect = model
    harness = ConversationHarness(client, AssistantSettings(), tmp_path, stopped)
    errors = []

    def work():
        try:
            harness.run(
                [{"role": "user", "content": "read"}],
                TOOLS,
                Mock(),
                Mock(),
                lambda x: x,
                lambda _: [],
            )
        except (AssistantAPIError, OSError) as exc:
            errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    assert model_entered.wait(5)
    process = harness._process
    assert process is not None
    stopped.set()
    harness.stop()
    release_model.set()
    worker.join(5)
    assert not worker.is_alive()
    assert process.poll() is not None
    assert len(errors) == 1
    assert isinstance(errors[0], AssistantAPIError), str(errors[0])


def test_tool_exception_is_returned_to_pi_for_a_limited_answer(tmp_path):
    invoke = Mock(side_effect=RuntimeError("source unavailable"))
    answer, client, _, audit, _ = run_loop(
        tmp_path,
        [call(), {"role": "assistant", "content": "read failed"}],
        invoke=invoke,
    )
    assert answer == "read failed"
    result = next(m for m in client.complete.call_args.args[0] if m["role"] == "tool")
    assert "source unavailable" in result["content"]
    invoke.assert_called_once()
    assert any(c.args[0] == "tool_error" for c in audit.call_args_list)


def test_normal_tool_data_with_error_field_remains_successful(tmp_path):
    invoke = Mock(return_value={"error": "diagnostic column", "rows": [1]})
    business_tool = {
        **TOOLS[0],
        "function": {**TOOLS[0]["function"], "name": "read_business_data"},
    }
    answer, client, _, _, _ = run_loop(
        tmp_path,
        [
            call(name="read_business_data"),
            {"role": "assistant", "content": "data returned"},
        ],
        invoke=invoke,
        tools=[business_tool],
    )
    assert answer == "data returned"
    result = next(m for m in client.complete.call_args.args[0] if m["role"] == "tool")
    assert json.loads(result["content"])["rows"] == [1]
    invoke.assert_called_once()


@pytest.mark.parametrize("after_tool", [False, True])
def test_model_520_is_not_retried_and_does_not_repeat_tools(tmp_path, after_tool):
    client = Mock()
    client.complete.side_effect = ([call()] if after_tool else []) + [
        AssistantAPIError("HTTP 520")
    ]
    invoke = Mock(return_value={"text": "source"})
    harness = ConversationHarness(
        client, AssistantSettings(), tmp_path, threading.Event()
    )
    with pytest.raises(AssistantAPIError, match="HTTP 520"):
        harness.run(
            [{"role": "user", "content": "read"}],
            TOOLS,
            invoke,
            Mock(),
            lambda x: x,
            lambda _: [],
        )
    assert client.complete.call_count == (2 if after_tool else 1)
    assert invoke.call_count == int(after_tool)
    assert harness._process is None
