"""Frozen original-page regressions: source gaps are not parser successes."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import extract_field, locate_sections
from src.fundamental_data.disclosure import gaps as review
from src.fundamental_data.disclosure.contract import FIELDS

FIXTURE = (
    Path(__file__).parent / "fixtures/fr_liquor_scope/moutai_disclosure_blanks.json"
)


def frozen():
    return json.loads(FIXTURE.read_text(encoding="utf8"))


def run(value):
    source, layout = value["source"], value["layout"]
    sections = locate_sections(source["pages"])
    fields = {
        m: extract_field(m, spec, sections, source["metadata"], layout)
        for m, spec in FIELDS.items()
    }
    health = review.review_disclosure_gaps(fields, sections, source, layout)
    return fields, health


def test_real_blank_keeps_coordinates_full_note_range_and_missing_amount(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    value = frozen()
    fields, _health = run(value)
    field = fields["goodwill"]
    assert field["status"] == "not_disclosed"
    assert field["raw_extraction_status"] == "parse_failed"
    evidence = field["disclosure_review"]
    assert evidence["state"] == "verified_blank"
    assert evidence["statement_evidence"]["coordinate_evidence"]["current_cell_bbox"]
    assert evidence["searched_note_blocks"][0]["pages"][0] == 87
    assert evidence["searched_note_blocks"][0]["pages"][-1] == 119
    assert "amount" not in field


@pytest.mark.parametrize("obstruction", ["image", "glyph", "old_layout"])
def test_no_verified_blank_with_image_unknown_glyph_or_missing_geometry(
    obstruction, monkeypatch, tmp_path
):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    value = frozen()
    fields, _ = run(value)
    box = fields["goodwill"]["disclosure_review"]["statement_evidence"][
        "coordinate_evidence"
    ]["current_cell_bbox"]
    page = value["layout"]["pages"]["59"]
    if obstruction == "image":
        page["image_bounds"].append(box)
    elif obstruction == "glyph":
        page["lines"][0]["cell_glyphs"].append({"text": "X", "bbox": box})
    else:
        page.pop("image_bounds")
    fields, _ = run(value)
    assert fields["goodwill"]["status"] != "not_disclosed"


def test_inline_major_heading_without_space_preserves_amount_note_scope():
    pages = {
        "1": "六、税项\n2、税收优惠\n税。七、合并财务报表项目注释\n1、货币资金\n单位：元\n项目 期末余额 期初余额\n合计 1.00 2.00"
    }
    ss = locate_sections(pages)
    assert any(s["kind"] == "notes" and s["title"] == "1、货币资金" for s in ss)


def receipt_fixture(tmp_path):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"receipt-validation-fixture")
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    source = {
        "metadata": {
            "issuer_id": "a_share:600519",
            "period_end": "2023-12-31",
            "sha256": sha,
            "path": str(pdf),
        },
        "pages": {"1": "项目 本期 上期\n商誉"},
    }
    receipt = {
        "schema_version": review.RECEIPT_VERSION,
        "method_version": review.RECEIPT_METHOD,
        "issuer_id": "a_share:600519",
        "period_end": "2023-12-31",
        "pdf_sha256": sha,
        "reviewer": "independent-offline-review",
        "fields": {
            "goodwill": {
                "classification": "statement_blank_verified",
                "reason": "original cell blank",
                "evidence": [{"page": 1, "quote": "商誉"}],
            }
        },
    }
    path = tmp_path / (sha + ".json")
    path.write_text(json.dumps(receipt), encoding="utf8")
    return source, receipt, path


@pytest.mark.parametrize("invalid", ["quote", "period", "hash", "method", "field"])
def test_review_receipt_rejects_forgery_atomically(invalid, monkeypatch, tmp_path):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    source, receipt, path = receipt_fixture(tmp_path)
    if invalid == "quote":
        receipt["fields"]["goodwill"]["evidence"][0]["quote"] = "商誉123"
    elif invalid == "period":
        receipt["period_end"] = "2024-12-31"
    elif invalid == "hash":
        receipt["pdf_sha256"] = "wrong"
    elif invalid == "method":
        receipt["method_version"] = "old"
    else:
        receipt["fields"]["cfo"] = deepcopy(receipt["fields"]["goodwill"])
    path.write_text(json.dumps(receipt), encoding="utf8")
    fields = {"goodwill": {"status": "parse_failed", "candidates": []}}
    health = {"details": {"goodwill": {"state": "unresolved_extraction"}}}
    review.apply_review_receipt(health, fields, source)
    assert health["receipt_errors"]
    assert fields["goodwill"]["status"] == "parse_failed"


def test_review_receipt_hash_changes_and_never_fills_or_excludes(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    source, receipt, path = receipt_fixture(tmp_path)
    before = review.disclosure_review_hash(source)
    fields = {"goodwill": {"status": "parse_failed", "candidates": []}}
    health = {"details": {"goodwill": {"state": "unresolved_extraction"}}}
    review.apply_review_receipt(health, fields, source)
    assert health["details"]["goodwill"]["state"] == "manual_verified_disclosure_gap"
    assert fields["goodwill"]["status"] == "not_disclosed"
    assert "amount" not in fields["goodwill"]
    assert "reviewed_exclusions" not in health
    receipt["fields"]["goodwill"]["reason"] = "updated review"
    path.write_text(json.dumps(receipt), encoding="utf8")
    assert review.disclosure_review_hash(source) != before


def test_conflicts_cannot_disappear_from_health(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    value = frozen()
    source = value["source"]
    fields = {
        "goodwill": {
            "status": "conflict",
            "candidates": [],
            "reason": "conflicting figures",
        }
    }
    health = review.review_disclosure_gaps(
        fields, locate_sections(source["pages"]), source, value["layout"]
    )
    assert health["status"] == "requires_review"
    assert health["details"]["goodwill"]["state"] == "conflict"
    assert fields["goodwill"]["status"] == "conflict"


def test_receipt_tampering_blocks_overall_health_even_with_verified_blanks(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    value = frozen()
    sha = value["source"]["metadata"]["sha256"]
    (tmp_path / (sha + ".json")).write_text(
        '{"method_version":"tampered"}', encoding="utf8"
    )
    _, health = run(value)
    assert health["receipt_errors"]
    assert health["status"] == "requires_review"


def test_partial_label_blank_does_not_hide_unresolved_amount(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "RECEIPT_DIR", tmp_path)
    value = frozen()
    source = value["source"]
    sections = locate_sections(source["pages"])
    field = extract_field("goodwill", FIELDS["goodwill"], sections, source["metadata"])
    c = deepcopy(field["candidates"][0])
    c["numbers"] = ["100.00"]
    field["candidates"].append(c)
    health = review.review_disclosure_gaps(
        {"goodwill": field}, sections, source, value["layout"]
    )
    assert health["details"]["goodwill"]["state"] == "unresolved_extraction"
    assert field["status"] == "parse_failed"


def test_image_health_closes_exact_validated_fields_only():
    doc = {
        "extraction_health": {
            "details": {
                "cfo": {"state": "unresolved_extraction"},
                "capex": {"state": "conflict"},
            },
            "unresolved_fields": ["cfo", "capex"],
            "source_limited_fields": [],
        },
        "gate": {
            "passed": True,
            "image_evidence_fields": {"cfo": ["image:1"], "capex": ["rejected:1"]},
            "usable_fields": ["cfo"],
            "missing_fields": {"capex": "conflict"},
        },
    }
    review.merge_verified_image_health(doc)
    assert doc["extraction_health"]["unresolved_fields"] == ["capex"]
    assert doc["extraction_health"]["status"] == "requires_review"
    assert doc["text_extraction_health"]["unresolved_fields"] == ["cfo", "capex"]
    assert doc["extraction_health"]["details"]["cfo"]["image_evidence_refs"] == [
        "image:1"
    ]
