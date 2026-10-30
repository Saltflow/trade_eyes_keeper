"""Complete, read-only and failure-safe catalog metadata publication."""

import hashlib
import json
import sqlite3

import pytest

from scripts import export_dataset_catalog as module
from src.data.dataset_catalog import DatasetCatalog


@pytest.fixture
def catalog(tmp_path):
    for number in range(121):
        path = tmp_path / f"data/analysis/run_{number:03d}/result.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "code": "000001",
                    "title": "Public report",
                    "source_url": "https://example.test/report.pdf",
                }
            ),
            encoding="utf-8",
        )
    value = DatasetCatalog(tmp_path)
    value.build()
    job = value.documents()["items"][0]["document_id"]
    for name, text in (("original.pdf", "%PDF-1.4"), ("original.txt", "BODY_PRIVATE")):
        relative = "data/dataset_documents/report/" + name
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        value.record_document(job, relative)
    with sqlite3.connect(value.db_path) as db:
        db.execute(
            "UPDATE documents SET source_url=?,title=?,error=?",
            (
                (
                    "https://alice:PASSWORD_PRIVATE@example.test/report.pdf"
                    "?id=123&token=TOKEN_PRIVATE#fragment"
                ),
                "TITLE_PRIVATE",
                "ERROR_PRIVATE",
            ),
        )
        db.execute(
            "CREATE TABLE document_downloads(job_id TEXT PRIMARY KEY,status TEXT,"
            "attempts INTEGER,error TEXT,result TEXT)"
        )
        db.execute(
            "INSERT INTO document_downloads VALUES(?,?,?,?,?)",
            (job, "failed", 2, "WORKER_ERROR_PRIVATE", '{"text":"RESULT_PRIVATE"}'),
        )
    return value


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_has_all_datasets_files_and_references_without_source_contents(catalog):
    before = hashlib.sha256(catalog.db_path.read_bytes()).hexdigest()
    result = module.export_catalog(catalog.root)
    target = catalog.db_path.parent
    datasets = json.loads((target / "datasets.json").read_text(encoding="utf-8"))
    assert result["datasets"] == datasets["total"] == len(datasets["datasets"]) == 122
    assert result["exported_rows"] == {
        "files.jsonl": 123,
        "documents.jsonl": 1,
        "document_sources.jsonl": 123,
        "document_artifacts.jsonl": 2,
    }
    for name, count in result["exported_rows"].items():
        rows = _lines(target / name)
        assert len(rows) == count
        assert all(row["export_id"] == result["export_id"] for row in rows)
        assert all(row["build_id"] == result["build_id"] for row in rows)
        assert all(row["exported_at"] == result["exported_at"] for row in rows)
    document = _lines(target / "documents.jsonl")[0]
    assert document["source_url"] == "https://example.test/report.pdf"
    assert document["source_url_sanitized"] is True
    assert len(document["original_source_url_sha256"]) == 64
    assert document["download_status"] == "failed"
    assert document["download_attempts"] == 2
    assert document["download_error_present"] == 1
    assert document["error_present"] == 1
    for name in module.EXPORT_NAMES:
        text = (target / name).read_text(encoding="utf-8")
        assert "_PRIVATE" not in text
        assert result["export_id"] in text
        assert result["build_id"] in text
    assert hashlib.sha256(catalog.db_path.read_bytes()).hexdigest() == before


def test_export_uses_one_read_only_snapshot_while_catalog_changes(catalog, monkeypatch):
    original = module._write_rows
    called = False

    def concurrent_write(db, query, destination, metadata):
        nonlocal called
        if not called:
            called = True
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("UPDATE files SET present=0")
            with sqlite3.connect(catalog.db_path) as writer:
                writer.execute("UPDATE files SET present=0")
        return original(db, query, destination, metadata)

    monkeypatch.setattr(module, "_write_rows", concurrent_write)
    result = module.export_catalog(catalog.root)
    assert result["datasets"] == 122
    assert result["exported_rows"]["files.jsonl"] == 123
    assert result["exported_rows"]["document_sources.jsonl"] == 123
    assert catalog.files()["total"] == 0


@pytest.mark.parametrize("phase", ["generation", "publication"])
def test_failure_preserves_every_previous_export(catalog, monkeypatch, phase):
    module.export_catalog(catalog.root)
    target = catalog.db_path.parent
    previous = {name: (target / name).read_bytes() for name in module.EXPORT_NAMES}
    if phase == "generation":
        original = module._write_rows

        def fail_write(db, query, destination, metadata):
            if destination.name == "documents.jsonl":
                raise OSError("fixture generation failure")
            return original(db, query, destination, metadata)

        monkeypatch.setattr(module, "_write_rows", fail_write)
    else:
        original = module.os.replace
        failed = False

        def fail_replace(source, destination):
            nonlocal failed
            if destination.name == "document_sources.jsonl" and not failed:
                failed = True
                raise OSError("fixture publication failure")
            return original(source, destination)

        monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture"):
        module.export_catalog(catalog.root)
    assert {
        name: (target / name).read_bytes() for name in module.EXPORT_NAMES
    } == previous
    assert not list(target.glob(".export*"))


def test_export_without_worker_table_and_during_new_build(catalog):
    with sqlite3.connect(catalog.db_path) as db:
        db.execute("DROP TABLE document_downloads")
        db.execute(
            "INSERT INTO builds VALUES('new-running',999999999999,NULL,'running','{}')"
        )
    result = module.export_catalog(catalog.root)
    assert result["index"]["status"] == "running"
    assert result["index"]["latest_build_id"] == "new-running"
    assert result["build_id"] != "new-running"
    assert (
        _lines(catalog.db_path.parent / "documents.jsonl")[0]["download_status"] is None
    )
