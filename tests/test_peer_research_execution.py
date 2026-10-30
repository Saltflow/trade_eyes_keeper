"""Offline, mocked one-request execution acceptance; never send a real request."""

import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts import run_peer_research as run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Unexpected network call")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)

    class FrozenDate(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(run, "datetime", FrozenDate)
    monkeypatch.setattr(run.transport, "datetime", FrozenDate)


@pytest.fixture
def prepared(tmp_path, synthetic_peer_package):
    repo, source = synthetic_peer_package
    output = tmp_path / "run"
    plan = run.prepare(repo, source, output, Decimal(2))
    return source, output, plan


def packet(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode("utf8") + b"\n\n"


class Response:
    status_code = 200

    def __init__(self, partial):
        self.partial = partial
        self.headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, **kwargs):
        yield packet(
            {
                "id": "synthetic-offline-only",
                "choices": [{"delta": {"reasoning_content": "mock thinking"}}],
            }
        )
        yield packet(
            {"choices": [{"delta": {"content": '# 同行\r\n22.51%\n{"未闭合":'}}]}
        )
        if self.partial:
            raise run.transport.requests.exceptions.ChunkedEncodingError(
                "mock interrupted"
            )
        yield packet(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100000,
                    "completion_tokens": 50000,
                    "prompt_cache_hit_tokens": 90000,
                    "prompt_cache_miss_tokens": 10000,
                    "completion_tokens_details": {"reasoning_tokens": 20000},
                },
            }
        )
        yield b"data: [DONE]\n\n"


def test_preparation_preserves_approved_request_and_budget(prepared):
    source, output, plan = prepared
    assert (output / "request.json").read_bytes() == (
        source / "request.preview.json"
    ).read_bytes()
    assert plan["maximum_calls"] == 1 and plan["automatic_retries"] == 0
    assert Decimal(plan["reserved_usd_peak"]) == Decimal("0.4718592")
    assert Decimal(plan["reserved_usd_peak"]) < Decimal(plan["authorized_usd"])
    assert not (output / "jobs").exists()
    # The repository's pinned tokenizer: 29 text tokens + 4096 message + 1024 image.
    assert plan["input_tokens_estimate"] == 5149
    frozen = run.transport.load(output / "freeze.json")["files"]
    assert all(
        p in frozen
        for p in run.transport.load(source / "调用规划.json")["source_hashes"]
    )


@pytest.mark.parametrize("partial", [False, True])
def test_one_request_raw_retention_cost_and_no_repeat(prepared, partial):
    _, output, plan = prepared
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        assert kwargs["data"] == (output / "request.json").read_bytes()
        assert kwargs["allow_redirects"] is False
        return Response(partial)

    result = run.execute(run.REPO, output, post=post, key="offline-mock")
    assert len(calls) == 1
    assert result["status"] == ("partial" if partial else "complete")
    assert (
        output / "完整分析.md"
    ).read_bytes() == '# 同行\r\n22.51%\n{"未闭合":'.encode()
    assert (output / "完整分析.md").read_bytes() == (
        output / "jobs/peer_research/content.md"
    ).read_bytes()
    assert (
        result["analysis_bytes_match_response"]
        and result["request_bytes_match_approved"]
    )
    if partial:
        assert result["cost"]["usd"] is None
        assert result["unknown_usage_reservation_usd"] == plan["reserved_usd_peak"]
    else:
        assert Decimal(result["cost"]["usd"]) == Decimal("0.03177")
        assert result["output_tokens_actual_including_thinking"] == 50000
        assert result["reasoning_tokens_actual"] == 20000
    with pytest.raises(FileExistsError):
        run.execute(run.REPO, output, post=post, key="offline-mock")
    assert len(calls) == 1


def test_budget_and_request_drift_stop_before_transport(prepared, tmp_path):
    source, output, _ = prepared
    with pytest.raises(ValueError, match="reservation_exceeds_budget"):
        run.prepare(run.REPO, source, tmp_path / "too_small", Decimal("0.01"))
    (output / "request.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="frozen_file_changed"):
        run.execute(
            run.REPO,
            output,
            post=lambda *a, **k: pytest.fail("must not send"),
            key="offline-mock",
        )
    assert not (output / "execution_started.json").exists()


def test_protected_source_drift_stops_before_preparation(
    synthetic_peer_package, tmp_path
):
    repo, source = synthetic_peer_package
    (source / "source_evidence.txt").write_bytes(b"changed after approval")

    with pytest.raises(ValueError, match="frozen_file_changed"):
        run.prepare(repo, source, tmp_path / "must_not_prepare", Decimal(2))

    assert not (tmp_path / "must_not_prepare").exists()
