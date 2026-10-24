"""Retired research remains readable without writes or execution dependencies."""

import hashlib
import json
import socket
import sqlite3
from datetime import date

import pytest

from src.fundamental_quant.reporting import bot_view, format_query, visible_company
from src.fundamental_quant.service import FundamentalQuantService


def service(root):
    return FundamentalQuantService(
        {"fundamental_quant": {"root": str(root)}}, today=date(2026, 10, 24)
    )


@pytest.fixture
def saved_archive(tmp_path):
    database = tmp_path / "research.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE companies(id TEXT, company TEXT, stage TEXT);
            CREATE TABLE publications(
                version TEXT, company_id TEXT, status TEXT, payload TEXT);
            CREATE TABLE longitudinal_publications(
                version TEXT, group_id TEXT, status TEXT, payload TEXT,
                html_path TEXT);
            CREATE TABLE pdf_research_sessions(
                id TEXT, company_id TEXT, state TEXT);
            CREATE TABLE research_report_staging(
                version TEXT, company_id TEXT, status TEXT, payload TEXT);
        """)
        company = {"id": "SH:600036", "code": "600036", "name": "历史公司"}
        db.execute("INSERT INTO companies VALUES(?,?,?)", (
            company["id"], json.dumps(company), "published"
        ))
        for version, status in (("v1", "revoked"), ("v2", "published")):
            payload = {"company": company, "conclusions": ["obsolete-buy"]}
            db.execute("INSERT INTO publications VALUES(?,?,?,?)", (
                version, company["id"], status, json.dumps(payload)
            ))
        group = {"packet": {"spec": {"companies": [company]}},
                 "state": {"approved_result": {"claim": "old-claim"}}}
        db.execute("INSERT INTO longitudinal_publications VALUES(?,?,?,?,?)", (
            "g1", "bank", "published", json.dumps(group), str(tmp_path / "old.html")
        ))
        db.execute("INSERT INTO pdf_research_sessions VALUES(?,?,?)", (
            "s1", company["id"], json.dumps({"phase": "review", "status": "paused"})
        ))
        db.execute("INSERT INTO research_report_staging VALUES(?,?,?,?)", (
            "draft1", company["id"], "staged", "{}"
        ))
    (tmp_path / "index.html").write_text("historical original", encoding="utf8")
    return tmp_path


def hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_archive_read_paths_do_not_change_files_or_use_network(saved_archive, monkeypatch):
    before = hashes(saved_archive)

    def no_network(*args, **kwargs):
        raise AssertionError("archive attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    reader = service(saved_archive)
    snapshot = reader.status()
    assert snapshot["status"] == "historical"
    assert reader.report() == snapshot
    assert reader.query("600036")["longitudinal_reports"][0]["version"] == "g1"
    assert reader.query("600036")["sessions"][0]["id"] == "s1"
    assert reader.query("600036")["sessions"][0]["original_status"] == "paused"
    assert "index.html" in snapshot["artifacts"]
    for result in (reader.run(), reader.notify_weekly()):
        assert result["status"] == "blocked"
        assert result["paid_model_calls"] == 0
        assert result["errors"][0]["code"] == "legacy_fundamental_workflow_retired"
    assert "obsolete-buy" not in format_query(snapshot)
    assert hashes(saved_archive) == before


def test_every_saved_version_and_original_status_remains_visible(saved_archive):
    result = service(saved_archive).query("600036")
    assert {(row["version"], row["original_status"]) for row in result["publications"]} == {
        ("v1", "revoked"), ("v2", "published"), ("draft1", "staged")
    }
    assert all(row["historical"] for row in result["publications"])
    assert result["publications"][0]["archive_table"] == "publications"
    assert "packet" not in json.dumps(result)
    assert service(saved_archive).query("000001")["companies"] == []


def test_missing_database_never_creates_directory(tmp_path):
    root = tmp_path / "missing" / "archive"
    reader = service(root)
    assert reader.status()["errors"] == [{"code": "research_not_started"}]
    assert reader.query("600036")["publications"] == []
    assert reader.report()["artifacts"] == {}
    reader.run()
    reader.notify_weekly()
    assert not root.exists()


@pytest.mark.parametrize("schema", [
    "CREATE TABLE unexpected(id TEXT)",
    "CREATE TABLE publications(version TEXT)",
])
def test_unknown_or_incomplete_schema_is_visible(tmp_path, schema):
    with sqlite3.connect(tmp_path / "research.sqlite3") as db:
        db.execute(schema)
    before = hashes(tmp_path)
    result = service(tmp_path).status()
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "archive_unreadable"
    assert "archive_schema_" in result["errors"][0]["detail"]
    assert hashes(tmp_path) == before


def test_malformed_database_is_visible_without_repair(tmp_path):
    database = tmp_path / "research.sqlite3"
    database.write_bytes(b"not a sqlite database")
    before = hashes(tmp_path)
    assert service(tmp_path).report()["status"] == "error"
    assert hashes(tmp_path) == before


@pytest.mark.parametrize("status", ["revoked", "stale", "published", "unverified"])
def test_bot_never_reveals_old_claims_even_from_mocked_inputs(status):
    raw = {
        "code": "600036", "errors": ["missing_original_report"],
        "conclusions": ["unverified-buy"], "metrics": {"target": "claim"},
        "reviewed_report": {"conclusions": ["unverified-buy"]},
        "publication": {"status": status, "version": "v1",
                        "conclusions": ["unverified-buy"], "packet": "secret"},
    }
    safe = visible_company(raw)
    text = json.dumps(bot_view({"companies": [raw]})) + format_query(raw)
    assert "unverified-buy" not in text
    assert "secret" not in text
    assert "missing_original_report" in text
    assert safe["publication"]["status"] == status
    assert safe["historical"] is True
