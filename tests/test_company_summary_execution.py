"""Mock transport acceptance before paid execution; no external request allowed."""

import io
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import run_company_summary as run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Unexpected network request")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def plan():
    root = Path(__file__).resolve().parents[1]
    return {
        "peak_rates": {"input": 0.3, "output": 1.2},
        "execution_rates": {"input": 0.15, "cached_input": 0.003, "output": 0.6},
        "input_cost_safety_factor": "1.25",
        "tokenizer": str(root / "data/fundamental_quant/tokenizer/tokenizer.json"),
        "pricing_utc_dates": [run.datetime.now(run.timezone.utc).date().isoformat()],
    }


def packet(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


def test_sse_preserves_invalid_model_json_unicode_and_tail_usage():
    files = [io.BytesIO() for _ in range(4)]
    stream = run.Stream(*files)
    chunks = [
        packet(
            {
                "choices": [
                    {"index": 0, "delta": {"reasoning_content": "private thinking"}}
                ]
            }
        ),
        packet({"choices": [{"delta": {"content": '{"值":"5–8%",'}}]}),
        packet(
            {
                "choices": [
                    {
                        "delta": {"content": "\r\n未闭合，照常保留"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ),
        packet(
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        ),
        b"data: [DONE]\n\n",
    ]
    wire = b"".join(chunks)
    for index in range(0, len(wire), 7):
        stream.feed(wire[index : index + 7])
    stream.end()
    assert files[0].getvalue() == wire
    assert files[1].getvalue().decode() == '{"值":"5–8%",\r\n未闭合，照常保留'
    assert files[2].getvalue() == b"private thinking"
    assert stream.usage["completion_tokens"] == 50
    assert stream.finish == "stop" and stream.done and not stream.errors


def test_transport_bad_frame_does_not_drop_later_content():
    files = [io.BytesIO() for _ in range(4)]
    stream = run.Stream(*files)
    stream.feed(
        b"data: INVALID\n\n"
        + packet({"choices": [{"delta": {"content": "still kept"}}]})
    )
    stream.end()
    assert stream.errors and files[1].getvalue() == b"still kept"


def test_binding_is_single_pass_and_preserves_crlf_and_partial_status(tmp_path):
    for name, content in (("a", "raw\r\n{{RECOVERY_CONTENT:b}}"), ("b", "B")):
        folder = tmp_path / "jobs" / name
        run.save(folder / "content.md", content)
        run.save(
            folder / "receipt.json",
            {"status": "partial", "http_status": 200, "finish_reason": "length"},
        )
    template = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "{{RECOVERY_CONTENT:a}}|{{RECOVERY_STATUS:a}}|{{RECOVERY_CONTENT:b}}",
                    }
                ],
            }
        ]
    }
    result = run.bind(template, ["a", "b"], tmp_path)
    assert result["messages"][0]["content"][0]["text"].startswith(
        "raw\r\n{{RECOVERY_CONTENT:b}}|"
    )
    assert "finish_reason=length" in result["messages"][0]["content"][0]["text"]
    assert template["messages"][0]["content"][0]["text"].startswith(
        "{{RECOVERY_CONTENT:a}}"
    )


def test_no_visible_response_bound_as_execution_gap(tmp_path):
    run.save(tmp_path / "jobs/a/content.md", "")
    run.save(
        tmp_path / "jobs/a/receipt.json",
        {"status": "failed", "error_type": "ConnectionError"},
    )
    template = {
        "messages": [{"content": [{"type": "text", "text": "{{RECOVERY_CONTENT:a}}"}]}]
    }
    text = run.bind(template, ["a"], tmp_path)["messages"][0]["content"][0]["text"]
    assert "执行缺口" in text and "不能解释为原文未披露" in text


def test_cost_cache_and_reasoning_not_double_charged():
    usage = {
        "prompt_tokens": 100000,
        "completion_tokens": 50000,
        "prompt_cache_hit_tokens": 90000,
        "prompt_cache_miss_tokens": 10000,
        "completion_tokens_details": {"reasoning_tokens": 20000},
    }
    result = run.cost(usage, plan())
    assert Decimal(result["usd"]) == Decimal("0.03177")
    assert result["kind"] == "usage_at_published_rates"
    assert run.cost(None, plan())["usd"] is None
    usage["prompt_cache_miss_tokens"] = 99999
    assert run.cost(usage, plan())["kind"] == "usage_cache_unknown_upper_bound"
    usage["prompt_tokens_details"] = "bad transport metadata"
    assert run.cost(usage, plan())["kind"] == "usage_cache_unknown_upper_bound"
    usage["prompt_cache_miss_tokens"] = 10000
    assert Decimal(run.cost(usage, plan())["usd"]) == Decimal("0.03177")


class Response:
    status_code = 200

    def __init__(self, interrupted=False):
        self.interrupted = interrupted
        self.headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, **kwargs):
        yield packet(
            {
                "id": "mock",
                "choices": [{"delta": {"content": "# 七问\n\n原有数据 22.51%\n"}}],
            }
        )
        if self.interrupted:
            raise run.requests.exceptions.ChunkedEncodingError("synthetic interruption")
        yield packet(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                },
            }
        )
        yield b"data: [DONE]\n\n"


@pytest.mark.parametrize("interrupted", [False, True])
def test_mock_call_saves_before_parsing_and_never_retries(tmp_path, interrupted):
    run.save(tmp_path / "freeze.json", {"files": {}})
    job = {"id": "MOCK", "dependencies": [], "reserved_usd_peak": "0.1"}
    attempts = []

    def post(*args, **kwargs):
        attempts.append(kwargs)
        assert kwargs["allow_redirects"] is False
        return Response(interrupted)

    receipt = run.perform(tmp_path, job, {"messages": []}, plan(), "FAKE", post=post)
    assert len(attempts) == 1 and receipt["automatic_retries"] == 0
    assert (
        tmp_path / "jobs/MOCK/content.md"
    ).read_bytes().decode() == "# 七问\n\n原有数据 22.51%\n"
    if interrupted:
        assert receipt["status"] == "partial" and receipt["usage"] is None
        assert receipt["reserved_usd_peak"] == "0.1" and receipt["cost"]["usd"] is None
    else:
        assert (
            receipt["status"] == "complete"
            and receipt["usage"]["completion_tokens"] == 20
        )
    with pytest.raises(FileExistsError):
        run.perform(tmp_path, job, {"messages": []}, plan(), "FAKE", post=post)
    assert len(attempts) == 1


def test_report_keeps_all_summary_and_recovery_with_unknown_usage(tmp_path):
    settings = plan()
    settings.update(
        jobs=[
            {"id": "TEST_recovery", "kind": "recovery", "reserved_usd_peak": "0.2"},
            {"id": "TEST_summary", "kind": "summary", "reserved_usd_peak": "0.3"},
        ],
        authorized_usd="2",
    )
    run.save(tmp_path / "freeze.json", {"files": {}})
    for job in settings["jobs"]:
        root = tmp_path / "jobs" / job["id"]
        run.save(root / "content.md", "# 原文\n\n| A | B |\n|---|---|\n| 甲 | 5–8% |\n")
        run.save(root / "attempt.json", {})
        run.save(
            root / "receipt.json",
            {
                "id": job["id"],
                "status": "partial",
                "usage": None,
                "cost": {"usd": None, "kind": "usage_unknown"},
                "reserved_usd_peak": job["reserved_usd_peak"],
            },
        )
    run.report(tmp_path, settings)
    result = run.load(tmp_path / "执行结果.json")
    assert result["unknown_usage_reservation_usd"] == "0.5"
    assert result["requests_sent"] == 2 and not result["all_usage_known"]
    assert "5–8%" in (tmp_path / "完整摘要.md").read_text(encoding="utf8")
    assert "5–8%" in (tmp_path / "完整恢复附录.md").read_text(encoding="utf8")
    assert "<table>" in (tmp_path / "完整摘要.html").read_text(encoding="utf8")


def test_missing_done_is_partial_but_content_survives(tmp_path):
    run.save(tmp_path / "freeze.json", {"files": {}})
    job = {"id": "MOCK", "dependencies": [], "reserved_usd_peak": "0.1"}

    class MissingDone(Response):
        def iter_content(self, **kwargs):
            yield packet(
                {
                    "choices": [
                        {
                            "delta": {"content": "complete words"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )

    receipt = run.perform(
        tmp_path,
        job,
        {"messages": []},
        plan(),
        "FAKE",
        post=lambda *a, **k: MissingDone(),
    )
    assert receipt["status"] == "partial" and receipt["finish_reason"] == "stop"
    assert "stream_missing_DONE" in receipt["transport_notes"]
    assert (tmp_path / "jobs/MOCK/content.md").read_text() == "complete words"


def test_attempt_without_receipt_is_not_claimed_unsent(tmp_path):
    settings = plan()
    settings.update(
        jobs=[{"id": "MOCK_summary", "kind": "summary", "reserved_usd_peak": "0.3"}],
        authorized_usd="2",
    )
    run.save(tmp_path / "freeze.json", {"files": {}})
    run.save(tmp_path / "jobs/MOCK_summary/attempt.json", {})
    run.save(tmp_path / "jobs/MOCK_summary/content.md", "partial survives")
    run.report(tmp_path, settings)
    result = run.load(tmp_path / "执行结果.json")
    assert result["receipts"][0]["status"] == "interrupted_receipt_missing"
    assert result["unknown_usage_reservation_usd"] == "0.3"


def test_full_approved_plan_mock_before_any_paid_call(
    tmp_path, monkeypatch, synthetic_summary_package
):
    repo, package = synthetic_summary_package
    attempts = []

    def fake_post(self, *args, **kwargs):
        request = json.loads(kwargs["data"])
        attempts.append(request)
        assert request["stream_options"]["include_usage"] is True
        assert request["response_format"] == {"type": "text"}
        return Response()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "FAKE_OFFLINE_ONLY")
    monkeypatch.setattr(run.requests.Session, "post", fake_post)

    class FrozenDate(run.datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 10, 24, 9, tzinfo=run.timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(run, "datetime", FrozenDate)
    run.prepare(repo, package, tmp_path / "mock_approved_execution", Decimal(2))
    run.execute(repo, tmp_path / "mock_approved_execution")
    assert len(attempts) == 8
    result = run.load(tmp_path / "mock_approved_execution/执行结果.json")
    assert result["requests_sent"] == 8 and result["protected_files_unchanged"]
    assert result["all_costs_precise_from_usage"]
    assert all(r["automatic_retries"] == 0 for r in result["receipts"])
    for job in result["receipts"]:
        content = tmp_path / "mock_approved_execution/jobs" / job["id"] / "content.md"
        assert content.read_bytes() == "# 七问\n\n原有数据 22.51%\n".encode()
    assert all(
        "{{RECOVERY_" not in json.dumps(request, ensure_ascii=False)
        for request in attempts
    )
    before = len(attempts)
    with pytest.raises(FileExistsError):
        run.execute(repo, tmp_path / "mock_approved_execution")
    assert len(attempts) == before


@pytest.mark.parametrize("changed", ["protected_source", "approved_template"])
def test_approved_summary_inputs_cannot_drift(
    tmp_path, synthetic_summary_package, changed
):
    repo, package = synthetic_summary_package
    target = (
        package / "source_evidence.txt"
        if changed == "protected_source"
        else package / "TESTA_recovery_1/request.template.json"
    )
    target.write_bytes(b"changed after approval")

    with pytest.raises(
        ValueError, match="frozen_file_changed|approved_request_template_changed"
    ):
        run.prepare(repo, package, tmp_path / "must_not_execute", Decimal(2))

    assert not (tmp_path / "must_not_execute").exists()
