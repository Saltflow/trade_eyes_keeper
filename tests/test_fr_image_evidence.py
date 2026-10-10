"""Offline source-image replay; no assertion of investment-research approval."""

import hashlib
import json
from pathlib import Path

import pytest

from src.fundamental_quant.fr_image_evidence import load_reviewed_image_facts

ROOT = Path("data/fundamental_quant/longitudinal_research_20261010")
MANIFEST = ROOT / "source_image_review/cccc_core_review.json"


def documents():
    if not MANIFEST.exists() or not (ROOT / "construction/packet.json").exists():
        pytest.skip("original reviewed image fixtures unavailable")
    return json.loads((ROOT / "construction/packet.json").read_text(encoding="utf8"))[
        "documents"
    ]


def test_actual_scanned_core_values_signs_and_periods():
    result = load_reviewed_image_facts(MANIFEST, documents())
    assert result["issues"] == []
    assert len(result["observations"]) == 54
    assert len(result["external_evidence"]) == 11
    current = {
        f["metric"]: f for f in result["observations"] if f["period"] == "2026-06-30"
    }
    assert current["cfo"]["value"] == -60866126040
    assert current["capex"]["value"] == 11408963134
    assert current["capex"]["reported_amount"] == "-11408963134"
    assert current["net_profit_total"]["value"] == 9553847976
    assert current["parent_equity"]["value"] == 313476830982
    assert all(f["origin"] == "agent_verified_image" for f in result["observations"])
    assert all(
        ref in result["external_evidence"]
        for f in result["observations"]
        for ref in f["refs"]
    )
    restated = [f for f in result["observations"] if f["period"] == "2023-12-31"]
    assert restated and all(
        f["restatement_basis"]["method"] == "reviewed_image_restatement_header/v1"
        for f in restated
    )


def test_changed_review_manifest_does_not_reuse_facts(tmp_path):
    docs = documents()
    target = tmp_path / "changed.json"
    target.write_bytes(MANIFEST.read_bytes() + b" ")
    target.with_suffix(".receipt.json").write_bytes(
        MANIFEST.with_suffix(".receipt.json").read_bytes()
    )
    result = load_reviewed_image_facts(target, docs)
    assert not result["observations"]
    assert result["issues"][0]["reason"] == "image_review_manifest_changed"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("issuer_id", "reviewed_pdf_identity_or_period_mismatch"),
        ("image_sha256", "reviewed_image_changed"),
    ],
)
def test_invalid_identity_or_image_is_rejected_even_with_new_manifest_receipt(
    tmp_path, mutation, expected
):
    docs = documents()
    payload = json.loads(MANIFEST.read_text(encoding="utf8"))
    payload["pages"] = [payload["pages"][0]]
    payload["pages"][0][mutation] = "wrong"
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(payload), encoding="utf8")
    target.with_suffix(".receipt.json").write_text(
        json.dumps(
            {"manifest_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        ),
        encoding="utf8",
    )
    result = load_reviewed_image_facts(target, docs)
    assert not result["observations"]
    assert result["issues"][0]["reason"] == expected
