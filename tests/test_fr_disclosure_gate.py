"""A historical gap receipt cannot erase a currently extracted value."""

import hashlib
import json

from src.fundamental_data.disclosure import gaps as disclosure
from src.fundamental_data.disclosure.contract import FIELDS


def specimen(tmp_path):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"frozen source identity for contract tests")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    meta = {
        "period_end": "2025-12-31",
        "issuer_id": "a_share:000001",
        "sha256": digest,
        "path": str(pdf),
    }
    fields = {
        key: {
            "metric": key,
            "value": "123.00",
            "unit": "元",
            "status": "obtained",
            "period_end": meta["period_end"],
            "scope": "consolidated",
            "pages": [1],
            "quote": "本期 123.00 上期 99.00",
        }
        for key in FIELDS
    }
    source = {
        "metadata": meta,
        "pages": {"1": "本期 123.00 上期 99.00\n商誉", "2": "完整合并附注"},
    }
    return fields, meta, source


def blank(field, meta):
    field.update(
        status="not_disclosed",
        value=None,
        raw_extraction_status="not_located",
        disclosure_review={
            "version": disclosure.VERSION,
            "state": "verified_blank",
            "pdf_sha256": meta["sha256"],
            "period_end": meta["period_end"],
            "statement_evidence": {
                "quote": "商誉",
                "source_ref": {"pages": [1]},
                "coordinate_evidence": {
                    "page": 1,
                    "pdf_sha256": meta["sha256"],
                    "method": "pdf_numeric_glyph_and_image_cell_check/1",
                    "current_cell_bbox": [10, 10, 20, 20],
                    "row_boxes": [[0, 10, 9, 20]],
                },
            },
            "searched_note_blocks": [{"section_id": "notes", "pages": [2]}],
            "note_numeric_candidates": [],
        },
    )










def test_obtained_value_is_not_erased_by_old_gap_receipt(tmp_path, monkeypatch):
    fields, meta, source = specimen(tmp_path)
    item = {
        "classification": "statement_blank_verified",
        "reason": "old blank",
        "evidence": [{"page": 1, "quote": "商誉"}],
    }
    receipt = {
        "schema_version": disclosure.RECEIPT_VERSION,
        "method_version": disclosure.RECEIPT_METHOD,
        "pdf_sha256": meta["sha256"],
        "issuer_id": meta["issuer_id"],
        "period_end": meta["period_end"],
        "reviewer": "source reviewer",
        "fields": {"goodwill": item},
    }
    (tmp_path / (meta["sha256"] + ".json")).write_text(
        json.dumps(receipt), encoding="utf8"
    )
    monkeypatch.setattr(disclosure, "RECEIPT_DIR", tmp_path)
    health = {"details": {}}
    disclosure.apply_review_receipt(health, fields, source)
    assert not health.get("receipt_errors")
    assert fields["goodwill"]["value"] == "123.00"
    assert fields["goodwill"]["status"] == "obtained"
