"""Offline catalog and original-report inventory acceptance."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.fundamental_quant.longitudinal_discovery import (
    archived_documents,
    original_report_coverage,
)


def test_catalog_discovery_is_read_only_and_verifies_original_hash(tmp_path):
    folder = tmp_path / "data/dataset_catalog"
    folder.mkdir(parents=True)
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"frozen original bytes for hash-boundary fixture")
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    dbpath = folder / "catalog.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.executescript("""
        CREATE TABLE documents(document_id,title,publication_date,source_url);
        CREATE TABLE document_sources(document_id,code);
        CREATE TABLE document_artifacts(document_id,file_id,kind);
        CREATE TABLE files(file_id,relative_path,sha256,present);
        """)
        db.execute(
            "INSERT INTO documents VALUES(?,?,?,?)",
            ("d1", "2022年年度报告", "2023-04-01", "https://static.cninfo.com.cn/example.pdf"),
        )
        db.execute("INSERT INTO document_sources VALUES(?,?)", ("d1", "600039"))
        db.execute("INSERT INTO document_artifacts VALUES(?,?,?)", ("d1", "f1", "pdf"))
        db.execute("INSERT INTO files VALUES(?,?,?,?)", ("f1", "original.pdf", sha, 1))
    before = hashlib.sha256(dbpath.read_bytes()).hexdigest()
    rows = archived_documents(
        [{"issuer_id": "a_share:600039", "name": "四川路桥"}], "2026-10-10", tmp_path
    )
    assert rows[0]["period_end"] == "2022-12-31"
    assert rows[0]["discovered_from"] == "dataset_catalog"
    assert rows[0]["url"] == "https://static.cninfo.com.cn/example.pdf"
    assert hashlib.sha256(dbpath.read_bytes()).hexdigest() == before
    assert (
        archived_documents(
            [{"issuer_id": "a_share:600039", "name": "四川路桥"}],
            "2023-03-31",
            tmp_path,
        )
        == []
    )
    pdf.write_bytes(b"changed source")
    with pytest.raises(RuntimeError, match="pdf_hash_mismatch"):
        archived_documents(
            [{"issuer_id": "a_share:600039", "name": "四川路桥"}],
            "2026-10-10",
            tmp_path,
        )


def test_comparative_numbers_cannot_supply_original_mda():
    spec = {
        "group_id": "x",
        "as_of": "2026-10-10",
        "history_years": 7,
        "companies": [{"issuer_id": "a_share:600039", "name": "四川路桥"}],
    }
    doc = {
        "issuer_id": "a_share:600039",
        "period_end": "2023-12-31",
        "published_at": "2024-04-01",
        "source_path": "original.pdf",
        "source_sha256": "a" * 64,
        "comparative_period_end": "2022-12-31",
    }
    result = original_report_coverage(spec, [doc])
    assert result["expected"] == 8
    assert result["available"] == 1
    by_period = {row["period_end"]: row for row in result["rows"]}
    assert by_period["2022-12-31"]["status"] == "missing_original"
    assert by_period["2019-12-31"]["status"] == "missing_original"
    assert by_period["2023-12-31"]["status"] == "archived_original"
    future = {**doc, "published_at": "2027-01-01"}
    assert original_report_coverage(spec, [future])["available"] == 0


def test_real_construction_inventory_keeps_missing_originals_visible():
    path = Path(
        "data/fundamental_quant/longitudinal_research_20261010/construction/packet.json"
    )
    if not path.exists():
        pytest.skip("frozen construction packet unavailable")
    packet = json.loads(path.read_text(encoding="utf8"))
    coverage = original_report_coverage(packet["spec"], packet["documents"])
    assert coverage["expected"] == 32
    assert len(packet["documents"]) >= 25
    actual = {(d["issuer_id"], d["period_end"]) for d in packet["documents"]}
    assert all(
        (row["status"] == "archived_original")
        == ((row["issuer_id"], row["period_end"]) in actual)
        for row in coverage["rows"]
    )
