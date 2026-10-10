"""Portability manifest boundaries; no raw artifact copies or model calls."""

import hashlib
import json
from pathlib import Path

import pytest

from src.fundamental_quant.longitudinal_export import export_sources


def test_export_lists_relative_targets_and_relocates_only_review_paths(
    tmp_path, monkeypatch
):
    data = tmp_path / "data"
    data.mkdir()
    pdf = data / "original.pdf"
    pdf.write_bytes(b"original PDF fixture bytes")
    image = data / "page.png"
    image.write_bytes(b"original image fixture bytes")
    review = data / "review.json"
    payload = {
        "pages": [
            {
                "image_path": str(image),
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "fields": [{"current": "42", "metric": "cfo"}],
            }
        ]
    }
    review.write_text(json.dumps(payload))
    review.with_suffix(".receipt.json").write_text(
        json.dumps({"manifest_sha256": hashlib.sha256(review.read_bytes()).hexdigest()})
    )
    before = review.read_bytes()
    doc = {
        "issuer_id": "a_share:1",
        "period_end": "2025-12-31",
        "published_at": "2026-04-01",
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "path": str(pdf),
    }
    cfg = {
        "groups": [
            {
                "group_id": "test",
                "companies": [],
                "ledger_issuers": [],
                "image_evidence_paths": [str(review)],
            }
        ]
    }
    result = export_sources(
        cfg, "2026-10-10", data / "export", lambda *a: [doc], project_root=tmp_path
    )
    assert review.read_bytes() == before
    assert not result["transfer_research_ledgers"]
    assert not result["transfer_derived_packets"]
    assert all(
        not Path(item["target"]).is_absolute()
        and ".." not in Path(item["target"]).parts
        for item in result["files"]
    )
    config = json.loads((tmp_path / result["config"]).read_text())
    assert "ledger_issuers" not in config["groups"][0]
    relocated = json.loads(
        (tmp_path / config["groups"][0]["image_evidence_paths"][0]).read_text()
    )
    assert relocated["pages"][0]["image_path"] == "data/page.png"
    assert relocated["pages"][0]["fields"] == payload["pages"][0]["fields"]
    assert not (data / "export" / "original.pdf").exists()


def test_export_rejects_non_data_dependencies(tmp_path, monkeypatch):
    secret = tmp_path / "secret.env"
    secret.write_text("not a source")
    cfg = {
        "groups": [
            {
                "group_id": "test",
                "companies": [],
                "supplemental_facts_paths": [str(secret)],
            }
        ]
    }
    with pytest.raises(ValueError, match="not_data"):
        export_sources(
            cfg,
            "2026-10-10",
            tmp_path / "data/export",
            lambda *a: [],
            project_root=tmp_path,
        )


def test_pit_snapshot_is_isolated_and_keeps_origin_hash(tmp_path):
    store = tmp_path / "data/point_in_time/fundamentals"
    store.mkdir(parents=True)
    original = store / "600039.statements.json"
    original.write_text("[]")
    cfg = {
        "groups": [
            {
                "group_id": "construction",
                "companies": [{"issuer_id": "a_share:600039"}],
                "valuation_store_root": str(store.parent),
            }
        ]
    }
    result = export_sources(
        cfg,
        "2026-10-10",
        tmp_path / "data/export",
        lambda *a: [],
        project_root=tmp_path,
    )
    assert all(
        not row["target"].startswith("data/point_in_time/") for row in result["files"]
    )
    config = json.loads((tmp_path / result["config"]).read_text())
    snapshot = tmp_path / config["groups"][0]["valuation_store_root"]
    assert (
        snapshot / "fundamentals/600039.statements.json"
    ).read_bytes() == original.read_bytes()
    provenance = json.loads((snapshot / "provenance.json").read_text())["files"][0]
    assert provenance["original_path"] == str(original.resolve())
    assert provenance["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
