"""Native tool fragments remain durable, billable messages, never empty success."""

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from src.model_gateway.transport import Stream, estimate, perform, sha


def test_interleaved_native_tool_fragments_keep_names_arguments_and_reasoning():
    streams = [io.BytesIO() for _ in range(4)]
    parser = Stream(*streams)
    parser.envelope(
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "provider context",
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "b",
                                "type": "function",
                                "function": {
                                    "name": "read_source",
                                    "arguments": '{"start":',
                                },
                            },
                            {
                                "index": 0,
                                "id": "a",
                                "type": "function",
                                "function": {"name": "find_sources", "arguments": "{"},
                            },
                        ],
                    }
                }
            ]
        }
    )
    parser.envelope(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "}"}},
                            {"index": 1, "function": {"arguments": "1}"}},
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert parser.finish == "tool_calls"
    assert parser.tool_calls[0]["function"]["arguments"] == "{}"
    assert json.loads(parser.tool_calls[1]["function"]["arguments"]) == {"start": 1}
    assert streams[2].getvalue() == b"provider context"


@pytest.mark.parametrize("wire", ["json", "sse"])
def test_native_tool_only_message_is_complete_with_usage_and_integrity(tmp_path, wire):
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "provider context",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_source",
                    "arguments": '{"source_id":"evidence"}',
                },
            },
        ],
    }
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 100,
    }

    class Response:
        status_code = 200
        headers: ClassVar[dict] = {
            "Content-Type": "application/json"
            if wire == "json"
            else "text/event-stream"
        }

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, chunk_size):
            envelope = {
                "choices": [
                    {
                        "message" if wire == "json" else "delta": message,
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": usage,
            }
            raw = json.dumps(envelope).encode()
            yield raw if wire == "json" else b"data: " + raw + b"\n\ndata: [DONE]\n\n"

    (tmp_path / "freeze.json").write_text('{"files": {}}')
    request = {
        "messages": [{"role": "user", "content": "read"}],
        "tools": [{"type": "function", "function": {"name": "read_source"}}],
    }
    plan = {
        "_gateway_managed": True,
        "endpoint": "https://offline.invalid",
        "pricing_utc_dates": [datetime.now(timezone.utc).date().isoformat()],
        "tokenizer": str(
            Path(__file__).resolve().parents[1]
            / "data/fundamental_quant/tokenizer/tokenizer.json"
        ),
        "execution_rates": {"input": ".15", "output": ".6", "cached_input": ".003"},
    }
    receipt = perform(
        tmp_path,
        {"id": "fixture", "dependencies": [], "reserved_usd_peak": ".1"},
        request,
        plan,
        "fixture-not-a-key",
        post=lambda *a, **k: Response(),
    )
    assert receipt["status"] == "complete"
    assert receipt["finish_reason"] == "tool_calls"
    assert receipt["usage"] == usage
    captured = tmp_path / "jobs/fixture/assistant_message.json"
    assert sha(captured) == receipt["assistant_message_sha256"]
    restored = json.loads(captured.read_text("utf-8"))
    assert restored["tool_calls"] == message["tool_calls"]
    assert restored["reasoning_content"] == message["reasoning_content"]
    assert restored["content"] == ""


def test_native_request_context_counts_tools_arguments_and_reasoning():
    tokenizer = SimpleNamespace(encode=lambda value: SimpleNamespace(ids=list(value)))
    config = {"message_token_reserve": 0, "image_token_reserve_each": 0}
    request = {
        "tools": [{"name": "read"}],
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"arguments": "payload"}],
                "reasoning_content": "provider context",
            }
        ],
    }
    assert estimate(request, tokenizer, config) > len("payloadprovider context")
