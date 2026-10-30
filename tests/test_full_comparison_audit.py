"""Offline audit checks, never model/provider tests."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "full_comparison_audit",
    Path(__file__).parents[1] / "scripts/check_full_comparison.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def ready(tmp_path):
    pdf = tmp_path / "source" / "original.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"fixture-pdf")
    text = "--- PAGE 1 ---Revenue 123.45 CNY"
    pdf.with_name("document.txt").write_text(text, encoding="utf-8")
    doc = {
        "short_id": "D01",
        "document_id": "source-id",
        "issuer_id": "a_share:1",
        "company_name": "Company",
        "path": str(pdf),
        "page_count": 1,
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    pages = MODULE.original_pages(text)
    put(tmp_path / "manifest.json", [doc])
    put(tmp_path / "sources/D01.json", {"metadata": doc, "pages": pages})
    note = {
        "company": "Company",
        "document_id": "source-id",
        "coverage": "page 1",
        "facts": [],
        "image_checks_needed": [],
    }
    put(tmp_path / "notes/D01.json", note)
    put(
        tmp_path / "calls/read_D01/request.json",
        {"messages": [{"role": "user", "content": MODULE.source_payload(doc, pages)}]},
    )
    put(tmp_path / "calls/read_D01/result.json", {"content": note})
    put(tmp_path / "calls/read_D01/state.json", {"status": "completed"})
    report = {
        "title": "Report",
        "executive_summary": "Read [D01 p1]",
        "sections": [{"title": "cash", "paragraphs": ["Uncertain [D01 p1]"]}],
        "conclusions": [
            {
                "key": key,
                "status": "insufficient",
                "basis": "uncertain",
                "refs": ["[D01 p1]"],
            }
            for key in sorted(MODULE.KEYS)
        ],
    }
    put(tmp_path / "report.json", report)
    put(tmp_path / "calls/synthesis/request.json", {"messages": []})
    review = {"verdict": "pass", "issues": [], "checks": ["[D01 p1]"]}
    for name, filename in [("review_1", "review_1"), ("review_summary", "review")]:
        put(tmp_path / f"{filename}.json", review)
        put(
            tmp_path / f"calls/{name}/request.json",
            {"messages": [{"role": "user", "content": json.dumps({"report": report})}]},
        )
        put(tmp_path / f"calls/{name}/result.json", {"content": review})
        put(tmp_path / f"calls/{name}/state.json", {"status": "completed"})
    return tmp_path


def check(root):
    return MODULE.audit(root, pdf_page_counter=lambda path: 1)


def codes(result):
    return {e["code"] for e in result["errors"]}


def test_mechanical_ready_never_certifies_user_acceptance(ready):
    result = check(ready)
    assert result["mechanical_ready"], result["errors"]
    assert not result["user_acceptance_complete"]
    assert result["documents"][0]["full_text_delivered"]
    assert result["documents"][0]["model_declared_coverage"] == "page 1"


def test_summary_material_issue_blocks_even_with_pass(ready):
    for path in [ready / "review.json", ready / "calls/review_summary/result.json"]:
        data = MODULE.read(path)
        value = data.get("content", data)
        value["issues"] = [{"severity": "material", "reason": "bad units"}]
        put(path, data)
    assert "material_review_issue" in codes(check(ready))


def test_report_change_breaks_review_binding_and_bad_ref_rejected(ready):
    report = MODULE.read(ready / "report.json")
    report["executive_summary"] = "Altered claim [D01 p2]"
    report["conclusions"].pop()
    put(ready / "report.json", report)
    found = codes(check(ready))
    assert {
        "review_report_binding_mismatch",
        "invalid_source_reference",
        "six_conclusion_keys_required",
    } <= found


def test_source_mutation_and_false_coverage_are_detected(ready):
    request = ready / "calls/read_D01/request.json"
    put(request, {"messages": [{"role": "user", "content": "summary only"}]})
    (ready / "source/document.txt").write_text("changed", encoding="utf-8")
    found = codes(check(ready))
    assert {
        "text_hash_mismatch",
        "source_pages_differ_from_frozen_text",
        "complete_source_not_in_request",
    } <= found


def test_suggested_images_surface_without_automatic_failure(ready):
    note = MODULE.read(ready / "notes/D01.json")
    note["image_checks_needed"] = [{"page": "p1", "reason": "suggested grid check"}]
    put(ready / "notes/D01.json", note)
    put(ready / "calls/read_D01/result.json", {"content": note})
    result = check(ready)
    assert result["mechanical_ready"]
    assert result["image_requirements"][0]["pending_human_judgment"]
    doc = MODULE.read(ready / "manifest.json")[0]
    put(
        ready / "image_checks.json",
        [
            {
                "document_id": "D01",
                "page": 1,
                "status": "not_relied_upon",
                "rationale": "Metric not used in report",
                "report_section": "cash",
                "source_sha256": doc["sha256"],
            }
        ],
    )
    result = check(ready)
    assert result["image_requirements"][0]["recorded_not_relied_upon"]


def test_note_identity_and_paid_result_are_bound(ready):
    note = MODULE.read(ready / "notes/D01.json")
    note.update(company="Other", document_id="D99")
    put(ready / "notes/D01.json", note)
    assert {
        "note_company_mismatch",
        "note_document_mismatch",
        "note_not_bound_to_paid_result",
    } <= codes(check(ready))


def test_revision_mapping_binds_current_report_and_preserves_old_calls(ready):
    report = MODULE.read(ready / "report.json")
    report["executive_summary"] = "Corrected report [D01 p1]"
    put(ready / "report.json", report)
    mappings = {"review_1": "review_1_v1", "review_summary": "review_summary_v1"}
    put(ready / "review_call_mapping.json", mappings)
    originals = {}
    for logical, actual in mappings.items():
        original = ready / "calls" / logical
        originals[logical] = {p.name: p.read_bytes() for p in original.iterdir()}
        for filename in ["result.json", "state.json"]:
            put(ready / "calls" / actual / filename, MODULE.read(original / filename))
        put(
            ready / "calls" / actual / "request.json",
            {"messages": [{"role": "user", "content": json.dumps({"report": report})}]},
        )
    result = check(ready)
    assert result["mechanical_ready"], result["errors"]
    assert {b["call_id"] for b in result["review_bindings"]} == set(mappings.values())
    for logical in mappings:
        assert originals[logical] == {
            p.name: p.read_bytes() for p in (ready / "calls" / logical).iterdir()
        }
    review = MODULE.read(ready / "review_1.json")
    review["checks"].append("An unreviewed addition")
    put(ready / "review_1.json", review)
    assert "review_artifact_differs_from_paid_output" in codes(check(ready))


def test_explicit_read_replay_ignores_preserved_failed_original(ready):
    original = ready / "calls/read_D01"
    replacement = ready / "calls/read_D01_output32k"
    for path in original.iterdir():
        put(replacement / path.name, MODULE.read(path))
    put(
        original / "state.json",
        {"status": "failed", "error_code": "incomplete_response"},
    )
    put(original / "result.json", {})
    put(ready / "replay.json", {"read_D01": "read_D01_output32k"})
    result = check(ready)
    assert result["mechanical_ready"], result["errors"]
    assert result["documents"][0]["full_text_delivered"]
    assert MODULE.read(original / "state.json")["status"] == "failed"


def test_review_mapping_cannot_escape_calls_directory(ready):
    put(ready / "review_call_mapping.json", {"review_1": "../outside"})
    assert "invalid_review_call_id" in codes(check(ready))
