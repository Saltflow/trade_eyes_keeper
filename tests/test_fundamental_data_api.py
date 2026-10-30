"""Freeze and single-attempt acceptance before any live paid demonstration."""

import json
from copy import deepcopy
from itertools import combinations
from types import SimpleNamespace

import pytest
import requests

from src.fundamental_data import api_client, api_pipeline
from src.fundamental_data.api_client import strict_json
from src.fundamental_data.api_format import FIELDS, METRICS, TOPICS, validate
from src.fundamental_data.api_io import check_seal, prepare, sha, write_once


def write_pdf(path, text):
    """Portable one-page fixture; no optional PDF authoring package required."""
    stream = f"BT /F1 12 Tf 30 750 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    raw, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(raw)
    raw += b"xref\n0 6\n0000000000 65535 f \n"
    raw += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    raw += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
    path.write_bytes(raw)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-not-a-live-key")
    documents = []
    for code in ("000001", "000002", "000003"):
        for year in (2023, 2024, 2025):
            path = tmp_path / f"{code}_{year}.pdf"
            write_pdf(
                path,
                f"Company {code} annual report {year}; amount 123.456; table current year, consolidated financials.",
            )
            documents.append(
                {
                    "path": str(path),
                    "issuer_id": "a_share:" + code,
                    "period_end": f"{year}-12-31",
                    "company_name": "Company " + code,
                    "sha256": sha(path),
                }
            )
    source = tmp_path / "inputs.json"
    source.write_text(
        json.dumps(
            {
                "documents": documents,
                "expected_companies": [
                    "a_share:" + c for c in ("000001", "000002", "000003")
                ],
                "expected_periods": [f"{y}-12-31" for y in (2023, 2024, 2025)],
            }
        ),
        encoding="utf8",
    )
    return source


def extraction(doc_id, expected):
    ref = {"document_id": doc_id, "page": 1, "quote": "amount 123.456"}
    return {
        "issuer_id": expected["issuer_id"],
        "company_name": expected["company_name"],
        "period_end": expected["period_end"],
        "facts": {
            k: {
                "status": "obtained",
                "amount": "123.456",
                "currency": None,
                "scope": "consolidated",
                "unit": "元",
                "evidence": [deepcopy(ref)],
            }
            for k in METRICS
        },
        "mda": {"text": "Summary", "evidence": [ref]},
        "topics": {k: {"status": "not_located", "evidence": []} for k in TOPICS},
        "fcf_boundary": {"definition": "proxy only", "adjustments": [], "evidence": []},
        "company": {
            k: {"status": "not_located", "value": None, "evidence": []} for k in FIELDS
        },
        "gaps": [],
    }


def responder(monkeypatch, damage=None):
    calls = []

    def post(url, *, json, headers, timeout, allow_redirects):
        payload = json
        calls.append(payload)
        assert url == "https://api.deepseek.com/v1/chat/completions"
        assert allow_redirects is False
        stage = payload["messages"][0]["content"].split("本次执行阶段：")[-1]
        content = payload["messages"][1]["content"]
        if stage == "extract":
            source = strict_json(content[0]["text"])
            result = extraction(source["document_id"], source["expected_identity"])
            if damage == "timeout" and len(calls) == 1:
                raise requests.Timeout("test timeout")
            if damage == "http" and len(calls) == 1:
                return SimpleNamespace(
                    status_code=500, content=b'{"error":{"type":"upstream"}}'
                )
            if damage == "amount":
                result["facts"]["cfo"]["amount"] = "99999"
        elif stage == "history":
            source = strict_json(content)
            result = {
                "documents": list(source["records"]) + source["missing_documents"],
                "common_policies": [],
                "changes": [],
                "management": [],
                "gaps": source["missing_documents"],
            }
            if damage == "oversize":
                result["gaps"] = ["x" * 30000]
        else:
            source = strict_json(content)
            result = {
                "companies": [
                    {
                        "issuer_id": c,
                        "metadata": {
                            k: {"status": "not_located", "value": None, "evidence": []}
                            for k in FIELDS
                        },
                        "missing": list(FIELDS),
                        "conflicts": [],
                    }
                    for c in source["companies"]
                ],
                "peer_pairs": [
                    {"issuers": [a, b], "relation": "undetermined", "evidence": []}
                    for a, b in combinations(sorted(source["companies"]), 2)
                ],
                "coverage_limits": ["input only"],
            }
        raw = __import__("json").dumps(result, ensure_ascii=False)
        if damage == "malformed" and stage == "extract":
            raw = '{"incomplete":'
        body = {
            "choices": [{"finish_reason": "stop", "message": {"content": raw}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }
        if damage == "length" and len(calls) == 1:
            body["choices"][0]["finish_reason"] = "length"
        return SimpleNamespace(
            status_code=200, content=__import__("json").dumps(body).encode()
        )

    monkeypatch.setattr(api_client.requests, "post", post)
    return calls


def test_one_run_executes_nine_three_one_and_preserves_raw_outputs(
    inputs, tmp_path, monkeypatch
):
    calls = responder(monkeypatch)
    out = tmp_path / "result"
    result = api_pipeline.run(inputs, out, paid=True)
    assert len(calls) == result["calls"] == 13
    assert result["status"] == "checks_passed_semantic_review_pending"
    assert len(list((out / "extract").glob("*/data.json"))) == 9
    assert len(list((out / "history").glob("*/data.json"))) == 3
    assert len(list((out / "metadata").glob("*/data.json"))) == 1
    assert len((out / "financials.csv").read_text(encoding="utf8").splitlines()) == 28
    for body_path in out.glob("*/*/response.body"):
        body = strict_json(body_path.read_bytes())
        assert (
            body_path.with_name("content.txt").read_text(encoding="utf8")
            == body["choices"][0]["message"]["content"]
        )
    hashes = strict_json((out / "output_hashes.json").read_bytes())
    assert all(sha(out / path) == digest for path, digest in hashes.items())
    with pytest.raises(FileExistsError):
        api_pipeline.run(inputs, out, paid=True)
    assert len(calls) == 13


@pytest.mark.parametrize(
    "damage", ["timeout", "malformed", "amount", "oversize", "http", "length"]
)
def test_failed_first_results_are_not_retried_or_repaired(
    inputs, tmp_path, monkeypatch, damage
):
    calls = responder(monkeypatch, damage)
    out = tmp_path / damage
    result = api_pipeline.run(inputs, out, paid=True)
    assert len(calls) == 13
    assert result["status"] == "needs_review"
    summary = strict_json((out / "summary.json").read_bytes())
    assert (
        summary["automatic_retries"]
        == summary["automatic_repairs"]
        == summary["manual_modifications"]
        == 0
    )
    if damage == "timeout":
        assert sum(c["status"] == "failed" for c in summary["calls"]) == 1
    if damage == "malformed":
        assert all(
            p.read_text(encoding="utf8") == '{"incomplete":'
            for p in out.glob("extract/*/content.txt")
        )
    if damage == "http":
        assert summary["usage_totals"]["calls_missing_usage"] == 1
    if damage == "amount":
        assert any(
            "amount_not_in_quoted_row:cfo" in issue
            for issues in summary["checks"].values()
            for issue in issues
        )
    if damage == "oversize":
        assert all(
            "history_context_exceeds_character_limit" in issues
            for key, issues in summary["checks"].items()
            if key.startswith("history_")
        )


def test_offline_preflight_cannot_call_api(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(
        api_client.requests,
        "post",
        lambda *a, **k: pytest.fail("paid call during preflight"),
    )
    out = tmp_path / "preflight"
    result = api_pipeline.run(inputs, out)
    assert result["pdfs"] == 9 and result["max_calls"] == 13
    assert not list(out.glob("extract/*/attempt.json"))
    assert (out / "freeze.json").exists()


def test_original_hash_mismatch_blocks_before_api(inputs, tmp_path):
    value = strict_json(inputs.read_bytes())
    value["documents"][0]["sha256"] = "0" * 64
    inputs.write_text(json.dumps(value), encoding="utf8")
    with pytest.raises(ValueError, match="hash_mismatch"):
        prepare(inputs, tmp_path / "bad_hash")


def test_immutable_writer_and_seal_detect_changes(tmp_path):
    target = tmp_path / "immutable.json"
    write_once(target, {"original": True})
    hashes = {str(target): sha(target)}
    check_seal(hashes)
    with pytest.raises(FileExistsError):
        write_once(target, {"overwritten": True})
    target.write_text("tampered", encoding="utf8")
    with pytest.raises(ValueError, match="frozen_code_or_input_changed"):
        check_seal(hashes)


@pytest.mark.parametrize(
    "raw", ['{"a":1,"a":1}', '{"a":NaN}', '{"a":Infinity}', "```json\n{}\n```"]
)
def test_output_json_is_never_repaired(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_unread_page_or_forged_quote_fails_source_acceptance():
    data = extraction(
        "D_test",
        {"issuer_id": "a_share:1", "company_name": "test", "period_end": "2023-12-31"},
    )
    sources = {"D_test": {"pages": {"1": "amount 123.456"}}}
    assert validate("extract", data, sources) == []
    data["facts"]["cfo"]["evidence"][0]["page"] = 2
    assert "invalid_source_reference" in validate("extract", data, sources)
    data["facts"]["cfo"]["evidence"][0].update(page=1, quote="invented 123.456")
    assert "quote_not_literal:D_test:1" in validate("extract", data, sources)


def test_one_cli_command_runs_all_three_modules(inputs, tmp_path, monkeypatch):
    import sys

    from scripts import build_fundamental_data

    calls = responder(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "data",
            "run",
            "--source",
            str(inputs),
            "--output",
            str(tmp_path / "cli"),
            "--paid",
        ],
    )
    assert build_fundamental_data.main() == 0
    assert len(calls) == 13


def test_invalid_context_budget_blocks_before_input_or_api(
    inputs, tmp_path, monkeypatch
):
    calls = responder(monkeypatch)
    with pytest.raises(ValueError, match="positive"):
        api_pipeline.run(inputs, tmp_path / "invalid", paid=True, max_chars=-1)
    assert calls == []


def test_missing_or_duplicate_pdf_never_triggers_paid_calls(
    inputs, tmp_path, monkeypatch
):
    calls = responder(monkeypatch)
    source = strict_json(inputs.read_bytes())
    source["documents"].pop()
    inputs.write_text(json.dumps(source), encoding="utf8")
    with pytest.raises(ValueError, match="matrix_mismatch"):
        api_pipeline.run(inputs, tmp_path / "incomplete", paid=True)
    source["documents"][1] = source["documents"][0]
    inputs.write_text(json.dumps(source), encoding="utf8")
    with pytest.raises(ValueError, match="duplicate_original_pdf"):
        api_pipeline.run(inputs, tmp_path / "duplicate", paid=True)
    assert calls == []
