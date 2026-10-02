"""Real SQLite claims and mocked provider responses; no external delivery."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest

from src.interactive.assistant.client import AssistantAPIError, DeepSeekAssistantClient
from src.interactive.assistant.settings import AssistantSettings
from src.interactive.assistant.store import ProposalStore


def test_confirmation_is_owner_bound_and_claimed_once(tmp_path):
    store = ProposalStore(tmp_path / "state.sqlite3")
    action = store.create("config", "chat", "owner", {"changes": []})
    with pytest.raises(ValueError):
        store.claim(action["id"], "chat", "owner")
    store.activate(action["id"])
    with pytest.raises(ValueError):
        store.claim(action["id"], "chat", "other")
    with pytest.raises(ValueError):
        store.claim(action["id"], "other-chat", "owner")

    def claim(_):
        try:
            return store.claim(action["id"], "chat", "owner")["status"]
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert results.count("queued") == 1
    assert results.count("rejected") == 3


def test_expired_and_modified_payloads_cannot_execute(tmp_path):
    store = ProposalStore(tmp_path / "state.sqlite3")
    expired = store.create("config", "chat", "owner", {}, ttl=-1)
    store.activate(expired["id"])
    with pytest.raises(ValueError):
        store.claim(expired["id"], "chat", "owner")
    changed = store.create("config", "chat", "owner", {"x": 1})
    store.activate(changed["id"])
    with store.connection() as db:
        db.execute(
            "UPDATE actions SET payload=? WHERE id=?", ('{"x":2}', changed["id"])
        )
    with pytest.raises(ValueError, match="变化"):
        store.claim(changed["id"], "chat", "owner")


def test_restart_preserves_results_receipts_and_interrupts_unfinished_jobs(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = ProposalStore(path)
    assert store.receipt("message-1")
    done = store.create("config", "chat", "owner", {})
    running = store.create("research", "chat", "owner", {"job_dir": "/work"})
    store.update(done["id"], "succeeded", result={"applied": True})
    store.activate(running["id"])
    store.claim(running["id"], "chat", "owner")
    restored = ProposalStore(path)
    assert not restored.receipt("message-1")
    recovered = restored.recover()
    assert [item["id"] for item in recovered] == [running["id"]]
    assert restored.get(running["id"], "chat", "owner")["status"] == "interrupted"
    assert restored.get(done["id"], "chat", "owner")["result"] == {"applied": True}


def api_response(
    code=200, *, content="answer", calls=None, finish="stop", reasoning_content="checked"
):
    response = Mock(status_code=code)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.json.return_value = {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                    "tool_calls": calls or [],
                },
            }
        ]
    }
    return response


def test_deepseek_tools_use_configured_high_thinking_and_preserve_reasoning():
    settings = AssistantSettings(request_retries=0)
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "secret-key", "base_url": "https://api.deepseek.com/v1/"}},
        settings,
    )
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "query_tasks", "arguments": "{}"},
    }
    with patch(
        "requests.post", return_value=api_response(calls=[call], finish="tool_calls")
    ) as post:
        result = client.complete([], [])
        assert result["tool_calls"] == [call]
        assert result["reasoning_content"] == "checked"
    kwargs = post.call_args.kwargs
    assert post.call_args.args[0].endswith("/v1/chat/completions")
    assert kwargs["json"]["thinking"] == {"type": "enabled"}
    assert kwargs["json"]["reasoning_effort"] == "high"
    assert kwargs["json"]["model"] == "deepseek-flash"
    assert kwargs["timeout"] == (5, 60)


def test_thinking_mode_fills_reasoning_only_for_synthetic_tool_messages():
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test-key"}}, AssistantSettings(request_retries=0)
    )
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "local"}]},
        {"role": "tool", "tool_call_id": "local", "content": "context"},
        {"role": "assistant", "content": "prior answer"},
    ]
    with patch("requests.post", return_value=api_response()) as post:
        client.complete(messages, [])
    sent = post.call_args.kwargs["json"]["messages"]
    assert sent[0]["reasoning_content"] == ""
    assert "reasoning_content" not in sent[2]
    assert "reasoning_content" not in messages[0]


def test_api_error_never_echoes_credentials_or_provider_body():
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "secret-key"}}, AssistantSettings(request_retries=0)
    )
    response = api_response(401)
    response.text = "secret-key credential diagnostic"
    with patch("requests.post", return_value=response), pytest.raises(
        AssistantAPIError
    ) as exc:
        client.complete([], [])
    assert "401" in str(exc.value)
    assert "secret-key" not in str(exc.value)


def test_truncated_tool_response_cannot_be_used():
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test-key"}}, AssistantSettings(request_retries=0)
    )
    with patch(
        "requests.post", return_value=api_response(finish="length")
    ), pytest.raises(AssistantAPIError):
        client.complete([], [])


def test_api_retry_is_bounded_and_stop_interrupts_backoff():
    client = DeepSeekAssistantClient(
        {"llm": {"api_key": "test-key"}}, AssistantSettings(request_retries=2)
    )
    client.stopped = Mock(spec=threading.Event)
    client.stopped.is_set.return_value = False
    client.stopped.wait.return_value = False
    with patch("requests.post", return_value=api_response(503)) as post, pytest.raises(
        AssistantAPIError
    ):
        client.complete([], [])
    assert post.call_count == 3
