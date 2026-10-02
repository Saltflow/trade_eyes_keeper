"""Offline catalog integrity, provenance, query, and document-gap contracts."""

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from src.data import dataset_catalog as module
from src.data.dataset_catalog import (
    DatasetCatalog,
    iter_catalog_entries,
    iter_dataset_files,
)


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
    )
    return path


def pit(root, prefix="data/point_in_time"):
    write(
        root,
        prefix + "/market/000001.csv",
        "date,close,stock_code\n2022-01-04,1.0,000001\n2024-01-02,2.0,000001\n",
    )
    write(
        root,
        prefix + "/fundamentals/000001.statements.json",
        {
            "code": "000001",
            "contract": "fixture-contract",
            "statements": [
                {
                    "period_end": "2022-12-31",
                    "published_at": "2023-04-01",
                    "source_url": "https://example.test/report.pdf",
                    "revenue": 100,
                },
                {
                    "period_end": "2023-12-31",
                    "published_at": "2024-04-01",
                    "source_url": None,
                    "revenue": 120,
                },
            ],
        },
    )


def dataset_id(catalog, logical_root):
    return next(
        item["dataset_id"]
        for item in catalog.list_datasets(limit=100)["items"]
        if item["logical_root"] == logical_root
    )


def test_unbuilt_catalog_is_explicit_and_does_not_claim_missing_data(tmp_path):
    catalog = DatasetCatalog(tmp_path)
    for value in (catalog.list_datasets(), catalog.files(), catalog.documents()):
        assert value["index_status"] == "not_built"
        assert value["indexed_at"] is None
        assert value["indexed_at_iso"] is None
        assert "不代表" in value["index_notice"]


def test_incremental_index_paths_registers_only_requested_ad_hoc_files(tmp_path):
    pit(tmp_path, "data/point_in_time/assistant_ad_hoc")
    catalog = DatasetCatalog(tmp_path)
    paths = [
        "data/point_in_time/assistant_ad_hoc/market/000001.csv",
        "data/point_in_time/assistant_ad_hoc/fundamentals/000001.statements.json",
    ]

    result = catalog.index_paths(paths)

    assert result["indexed_files"] == 2
    assert result["enumeration_complete"] is False
    assert catalog.index_status()["index_status"] == "incremental"
    assert "最近一次为增量登记" in catalog.index_status()["index_notice"]
    files = catalog.files(code="000001", limit=10)
    assert files["total"] == 2
    assert {
        item["logical_path"] for item in files["items"]
    } == set(paths)
    assert all(
        catalog.resolve_dataset_root(item["dataset_id"])
        == tmp_path / "data/point_in_time/assistant_ad_hoc"
        for item in files["items"]
    )


def test_build_enumerates_all_scopes_and_preserves_sha_duplicates(tmp_path):
    pit(tmp_path)
    original = tmp_path / "data/point_in_time/market/000001.csv"
    write(
        tmp_path,
        "data/server_imports/snapshot/dataset/data/point_in_time/market/000001.csv",
        original.read_text(),
    )
    write(tmp_path, "data/000002_history.csv", "date,stock_code\n2024-01-01,000002\n")
    write(
        tmp_path,
        "data/reference_universe/companies.json",
        {"companies": [{"code": "000001.SZ"}]},
    )
    write(tmp_path, "data/point_in_time_other/market/000001.csv", original.read_text())
    write(
        tmp_path,
        "cache/announcement_content/source.json",
        {
            "stock_code": "000001",
            "url": "https://example.test/notice",
            "content": "原文",
        },
    )
    write(tmp_path, "cache/historical/000003.csv", "date,close\n2024-01-01,1\n")
    write(tmp_path, "cache/data/000004.csv", "date,close\n2024-01-01,2\n")
    write(tmp_path, "cache/pdf_files/report.pdf", "%PDF-1.4 fixture")
    write(tmp_path, "data/analysis/result/report.json", {"status": "downloaded"})
    catalog = DatasetCatalog(tmp_path)
    manifest = catalog.build()
    assert manifest["indexed_files"] == 11
    assert manifest["unique_security_count"] == 4
    assert manifest["unique_content_count"] < manifest["indexed_files"]
    assert manifest["analysis_ready"] is None
    key = dataset_id(catalog, "data/point_in_time")
    details = catalog.details(key)
    assert details["file_count"] == 2
    assert details["unique_security_count"] == 1
    assert catalog.resolve_dataset_root(key) == tmp_path / "data/point_in_time"
    files = catalog.files(key, code="000001")
    assert files["total"] == 2
    assert any(item["source_copy_count"] == 3 for item in files["items"])
    assert any(
        item["date_start"] == "2022-01-04" and item["date_end"] == "2024-01-02"
        for item in files["items"]
    )
    entries = [
        json.loads(line)
        for line in (tmp_path / manifest["manifest_path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len([entry for entry in entries if entry.get("status") == "indexed"]) == 11


def test_scan_has_no_file_cap_but_bot_queries_are_paginated(tmp_path):
    for index in range(121):
        write(tmp_path, f"data/analysis/run/file_{index}.json", {"n": index})
    catalog = DatasetCatalog(tmp_path)
    assert catalog.build()["indexed_files"] == 121
    page1 = catalog.files(limit=100)
    page2 = catalog.files(limit=100, offset=100)
    assert page1["total"] == page2["total"] == 121
    assert len(page1["items"]) == 100
    assert len(page2["items"]) == 21
    assert not (
        {item["file_id"] for item in page1["items"]}
        & {item["file_id"] for item in page2["items"]}
    )
    with pytest.raises(ValueError):
        catalog.files(limit=101)
    with pytest.raises(ValueError):
        catalog.files(offset=-1)


def test_bad_json_and_unsupported_binary_are_reported_not_silently_dropped(
    tmp_path, monkeypatch
):
    write(tmp_path, "data/analysis/run/bad.json", "{")
    write(tmp_path, "data/analysis/run/large.json", {"large": "x" * 100})
    write(tmp_path, "data/analysis/run/opaque.parquet", "fixture only")
    monkeypatch.setattr(module, "MAX_JSON_BYTES", 50)
    catalog = DatasetCatalog(tmp_path)
    result = catalog.build()
    assert result["indexed_files"] == 3
    assert result["parse_failures"] == 1
    assert result["unparsed_files"] == 2
    assert result["metadata_complete"] is False
    statuses = {item["parse_status"] for item in catalog.files()["items"]}
    assert statuses == {"parse_failed", "skipped_size_limit", "unsupported_format"}


def test_stable_ids_rebuild_tracks_edits_and_deleted_files(tmp_path):
    path = write(tmp_path, "cache/data/000001.csv", "date,close\n2024-01-01,1\n")
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    first = catalog.files()["items"][0]
    path.write_text("date,close\n2024-01-01,2\n", encoding="utf-8")
    catalog.build()
    second = catalog.files()["items"][0]
    assert second["file_id"] == first["file_id"]
    assert second["dataset_id"] == first["dataset_id"]
    assert second["sha256"] != first["sha256"]
    path.unlink()
    catalog.build()
    assert catalog.files()["total"] == 0


def test_private_operational_paths_and_hardlinks_are_excluded(tmp_path):
    for name in (
        "config/.env",
        "data/analysis/private_account/holdings.json",
        "data/analysis/run/credentials.json",
        "data/analysis/run/progress.json",
        "data/analysis/run/.transfer/chunk.bin",
    ):
        write(tmp_path, name, "secret")
    plain = write(tmp_path, "cache/data/normal.txt", "visible")
    alias = tmp_path / "cache/data/alias.txt"
    os.link(plain, alias)
    entries = list(iter_catalog_entries(tmp_path))
    assert all(not entry["eligible"] for entry in entries)
    assert sum(entry["reason"] == "hardlink_not_proven_local" for entry in entries) == 2
    assert list(iter_dataset_files(tmp_path)) == []


def test_symlink_and_linked_catalog_storage_are_rejected(tmp_path):
    outside = write(tmp_path, "outside/sensitive.txt", "private")
    link = tmp_path / "cache/data/link.txt"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not grant symlink creation")
    assert next(iter_catalog_entries(tmp_path))["reason"] == "symlink_or_reparse_point"
    catalog_dir = tmp_path / "data/dataset_catalog"
    catalog_dir.parent.mkdir(parents=True)
    catalog_dir.symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="linked"):
        DatasetCatalog(tmp_path)


def test_materialized_import_manifest_checks_all_declared_members(tmp_path):
    prefix = "data/dataset_imports/local_hash/dataset"
    pit(tmp_path, prefix + "/data/point_in_time")
    file = tmp_path / prefix / "data/point_in_time/market/000001.csv"
    write(
        tmp_path,
        "data/dataset_imports/local_hash/manifest.json",
        {
            "files": [
                {
                    "path": "data/point_in_time/market/000001.csv",
                    "bytes": file.stat().st_size,
                    "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                },
                {"path": "data/missing_history.csv", "bytes": 3, "sha256": "bad"},
                {"path": "../private.txt", "sha256": "bad"},
            ]
        },
    )
    catalog = DatasetCatalog(tmp_path)
    result = catalog.build()
    assert result["manifest_gaps"] == 2
    assert result["transfer_complete"] is False
    assert catalog.index_status()["index_status"] == "completed_with_gaps"
    key = dataset_id(catalog, "data/point_in_time")
    assert catalog.resolve_dataset_root(key) == tmp_path / prefix / "data/point_in_time"
    assert catalog.files(key)["items"][0]["logical_path"].startswith(
        "data/point_in_time/"
    )


def test_documents_report_missing_url_and_content_then_accumulate_artifacts(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    jobs = catalog.document_jobs()
    assert {item["status"] for item in jobs["items"]} == {
        "missing_url",
        "missing_document",
    }
    target = next(item for item in jobs["items"] if item["source_url"])
    assert catalog.claim_document_job(target["job_id"])["status"] == "running"
    assert catalog.claim_document_job(target["job_id"]) is None
    pdf = write(
        tmp_path, "data/dataset_documents/example/source.pdf", "%PDF-1.4 fixture"
    )
    text = write(
        tmp_path, "data/dataset_documents/example/source.txt", "原文第一行\n第二行\n"
    )
    catalog.record_document(target["job_id"], pdf.relative_to(tmp_path).as_posix())
    outcome = catalog.record_document(
        target["job_id"], text.relative_to(tmp_path).as_posix()
    )
    assert len(outcome["existing_paths"]) == 2
    assert outcome["source_pdf_missing"] is False
    assert outcome["source_url_missing"] is False
    assert outcome["original_path"].endswith("source.pdf")
    assert outcome["text_path"].endswith("source.txt")
    repeated = catalog.record_document(
        target["job_id"], text.relative_to(tmp_path).as_posix()
    )
    assert len(repeated["existing_paths"]) == 2
    file_id = catalog.files(query="source.txt")["items"][0]["file_id"]
    assert catalog.read_text(file_id, line_count=1)["text"] == "原文第一行\n"
    catalog.build()
    assert any(
        item["job_id"] == target["job_id"] and not item["source_pdf_missing"]
        for item in catalog.documents()["items"]
    )


def test_download_failure_state_is_visible_without_inventing_pdf(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    target = next(item for item in catalog.documents()["items"] if item["source_url"])
    with sqlite3.connect(catalog.db_path) as db:
        db.execute(
            "CREATE TABLE document_downloads(job_id TEXT PRIMARY KEY,status TEXT,attempts INTEGER,next_attempt_at REAL,updated REAL,error TEXT,result TEXT)"
        )
        db.execute(
            "INSERT INTO document_downloads VALUES(?,?,?,?,?,?,?)",
            (
                target["job_id"],
                "failed",
                2,
                999999,
                10,
                "source unavailable",
                '{"http_status":503}',
            ),
        )
    item = next(
        item
        for item in catalog.documents()["items"]
        if item["job_id"] == target["job_id"]
    )
    assert item["download_status"] == "failed"
    assert item["download_result"]["http_status"] == 503
    assert item["source_pdf_missing"] is True


def test_text_read_checks_identity_and_limits_content(tmp_path):
    path = write(tmp_path, "data/dataset_documents/local/source.txt", "x" * 20000)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    file = catalog.files()["items"][0]
    answer = catalog.read_text(file["file_id"])
    assert len(answer["text"]) == 16000
    assert answer["truncated"] is True
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        catalog.read_text(file["file_id"])
    with pytest.raises(ValueError):
        catalog.read_text("../../config/.env")


def test_sql_filters_are_literal_and_running_build_is_not_fresh(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    assert catalog.files(query="' OR 1=1 --")["total"] == 0
    assert catalog.documents(dataset_id="' OR 1=1 --")["total"] == 0
    with sqlite3.connect(catalog.db_path) as db:
        db.execute(
            "INSERT INTO builds VALUES('active',99999999999,NULL,'running','{}')"
        )
    result = catalog.files()
    assert result["index_status"] == "running"
    assert result["indexed_at"] is not None
    assert "旧快照" in result["index_notice"]


def test_index_failure_is_visible_in_completeness_manifest(tmp_path, monkeypatch):
    write(tmp_path, "cache/data/test.json", {})
    catalog = DatasetCatalog(tmp_path)
    monkeypatch.setattr(
        catalog, "_index", lambda *args: (_ for _ in ()).throw(OSError("unreadable"))
    )
    result = catalog.build()
    assert result["enumeration_complete"] is False
    assert result["index_failures"] == 1
    assert catalog.index_status()["index_status"] == "completed_with_gaps"


def test_transfer_policy_excludes_scripts_archives_and_embedded_credentials(tmp_path):
    for name, value in {
        "script.py": "print('not data')",
        "bundle.gz": "compressed bytes",
        "settings.yaml": "setting: 1",
        "config_snapshot.json": '{"stocks":[]}',
        "account_balance.json": '{"balance":100}',
        "ordinary_report.json": '{"nested":{"app_secret":"private-test-credential"}}',
        "report.txt": "API_KEY=private-test-credential",
        "safe.csv": "date,close\n2024-01-01,1\n",
    }.items():
        write(tmp_path, "data/analysis/run/" + name, value)
    entries = list(iter_catalog_entries(tmp_path))
    assert [entry["path"].name for entry in entries if entry["eligible"]] == [
        "safe.csv"
    ]
    assert (
        sum(entry["reason"] == "suspected_sensitive_content" for entry in entries) == 2
    )


def test_interrupted_build_rolls_back_every_file_to_previous_snapshot(
    tmp_path, monkeypatch
):
    path = write(tmp_path, "cache/data/000001.csv", "date,close\n2024-01-01,1\n")
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    old = catalog.files()["items"][0]
    path.write_text("date,close\n2024-01-01,9\n", encoding="utf-8")
    entry = next(iter_dataset_files(tmp_path))

    def broken_scan(_root):
        yield entry
        raise RuntimeError("scan interrupted")

    monkeypatch.setattr(module, "iter_catalog_entries", broken_scan)
    with pytest.raises(RuntimeError, match="interrupted"):
        catalog.build()
    assert catalog.files()["items"][0]["sha256"] == old["sha256"]
    assert catalog.index_status()["index_status"] == "failed"


def test_document_worker_does_not_invent_extra_missing_url_jobs(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    before = catalog.documents()
    job = next(item for item in before["items"] if item["source_url"])
    for name, value in (("source.pdf", "%PDF-1.4"), ("source.txt", "document text")):
        relative = "data/dataset_documents/job/" + name
        write(tmp_path, relative, value)
        catalog.record_document(job["job_id"], relative)
    assert catalog.documents()["total"] == before["total"]
    catalog.build()
    after = catalog.documents()
    assert after["total"] == before["total"]
    assert (
        after["status_counts"]["source_url_missing"]
        == before["status_counts"]["source_url_missing"]
    )


def test_public_eligibility_cannot_smuggle_files_through_import_roots():
    assert (
        module.eligibility_reason(
            "data/analysis/source.py", "data/point_in_time/market/a.csv"
        )
        == "logical_path_mismatch"
    )
    for path in (
        "src/foo.json",
        "config/config.json",
        "data/dataset_imports/a/dataset/src/foo.json",
        "data/dataset_imports/a/dataset/data/ref_portfolio.json",
    ):
        assert module.eligibility_reason(path) == "outside_business_data_roots"
    assert (
        module.eligibility_reason(
            "data/dataset_imports/a/dataset/data/point_in_time/market/000001.csv"
        )
        == ""
    )
    assert (
        module.eligibility_reason(
            "data/dataset_imports/a/dataset/data", is_directory=True
        )
        == ""
    )


def test_html_renamed_pdf_is_not_counted_as_available_original(tmp_path):
    write(tmp_path, "cache/pdf_files/report.pdf", "<html>access denied</html>")
    catalog = DatasetCatalog(tmp_path)
    assert catalog.build()["parse_failures"] == 1
    item = catalog.documents()["items"][0]
    assert item["source_pdf_missing"] is True
    assert item["status"] != "available"


def test_existing_pdf_metadata_restores_url_association_without_duplicate_gap(tmp_path):
    write(tmp_path, "cache/pdf_files/existing.pdf", "%PDF-1.4")
    write(
        tmp_path,
        "cache/announcement_content/notice.json",
        {
            "stock_code": "000001",
            "url": "https://example.test/existing.pdf",
            "content": "原文",
            "metadata": {
                "stock_code": "000001",
                "url": "https://example.test/existing.pdf",
                "pdf_file_path": "cache/pdf_files/existing.pdf",
            },
        },
    )
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    documents = catalog.documents()
    assert documents["total"] == 1
    assert documents["items"][0]["source_pdf_missing"] is False
    assert documents["items"][0]["source_url_missing"] is False


def test_analysis_recovery_and_instrument_audit_roots_are_fully_indexed(tmp_path):
    files = {
        "cache/analysis/option_collar_test/nav.csv": "date,nav\n2024-01-01,1\n",
        "cache/analysis/collar_robustness/report.json": '{"robustness":1}',
        "cache/analysis/collar_nav_history.csv": "date,nav\n2024-01-01,1\n",
        "data/optimizer_recovery/20260924/yahoo/responses/000001.json": '{"symbol":"000001"}',
        "data/optimizer_recovery/20260924/yahoo/market/000001.csv": "date,close\n2024-01-01,1\n",
        "data/instrument_audit/audit.json": '{"code":"000001"}',
        "data/instrument_audit/audit.html": "<html>instrument audit</html>",
    }
    for path, value in files.items():
        write(tmp_path, path, value)
    write(tmp_path, "data/optimizer/default.yaml", "model: configuration")
    write(tmp_path, "data/optimizer_recovery/20260924/yahoo/runtime.log", "private log")
    write(
        tmp_path,
        "cache/analysis/option_collar_test/config_snapshot.json",
        '{"api_key":"private-test-key"}',
    )
    catalog = DatasetCatalog(tmp_path)
    result = catalog.build()
    indexed = catalog.files(limit=100)["items"]
    assert {item["relative_path"] for item in indexed} == set(files)
    assert {item["kind"] for item in indexed} >= {
        "analysis",
        "price",
        "provider_response",
        "instrument_audit",
    }
    manifest = [
        json.loads(line)
        for line in (tmp_path / result["manifest_path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        item.get("relative_path") == "data/optimizer/default.yaml"
        and item["status"] == "skipped"
        for item in manifest
    )


def test_unknown_and_private_top_level_roots_are_audited_without_reading_contents(
    tmp_path, monkeypatch
):
    write(tmp_path, "data/future_dataset/secret.json", '{"private":true}')
    write(tmp_path, "data/private_account/holdings.json", '{"private":true}')
    write(tmp_path, "data/pf.json", '{"private":true}')
    write(tmp_path, "cache/unknown_future/source.json", '{"private":true}')
    monkeypatch.setattr(
        module,
        "_business_file_reason",
        lambda path: pytest.fail(f"Should not read excluded data: {path}"),
    )
    entries = list(iter_catalog_entries(tmp_path))
    assert {item["relative_path"] for item in entries} == {
        "data/future_dataset",
        "data/private_account",
        "data/pf.json",
        "cache/unknown_future",
    }
    assert all(not item["eligible"] for item in entries)
    assert all(item["reason"] for item in entries)


def test_downloaded_artifacts_belong_to_document_dataset_and_inherit_security(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    source_dataset = dataset_id(catalog, "data/point_in_time")
    job = next(
        item
        for item in catalog.documents(source_dataset)["items"]
        if item["source_url"]
    )
    for name, value in (("source.pdf", "%PDF-1.4"), ("source.txt", "original text")):
        relative = "data/dataset_documents/download/" + name
        write(tmp_path, relative, value)
        catalog.record_document(job["job_id"], relative)
    artifact_dataset = dataset_id(catalog, "data/dataset_documents")
    for _ in range(2):
        linked = catalog.documents(artifact_dataset, code="000001")
        assert linked["total"] == 1
        assert linked["items"][0]["document_id"] == job["job_id"]
        assert set(linked["items"][0]["dataset_ids"]) == {
            source_dataset,
            artifact_dataset,
        }
        artifacts = catalog.files(artifact_dataset, code="000001")
        assert artifacts["total"] == 2
        assert {item["kind"] for item in artifacts["items"]} == {"pdf", "text"}
        assert catalog.files(artifact_dataset, kind="text", code="000001")["total"] == 1
        assert catalog.details(artifact_dataset)["document_statuses"]["available"] == 1
        assert any(
            item["document_id"] == job["job_id"]
            for item in catalog.documents(source_dataset)["items"]
        )
        catalog.build()


@pytest.mark.parametrize("kinds", [("pdf",), ("text",), ("pdf", "text")])
def test_document_counts_distinguish_available_content_from_pdf_text_pairs(
    tmp_path, kinds
):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    source_dataset = dataset_id(catalog, "data/point_in_time")
    job = next(d for d in catalog.documents()["items"] if d["source_url"])
    for kind in kinds:
        suffix, content = ("pdf", "%PDF-1.4") if kind == "pdf" else ("txt", "text")
        for copy in range(2):
            relative = f"data/dataset_documents/test/{copy}.{suffix}"
            write(tmp_path, relative, content)
            catalog.record_document(job["job_id"], relative)
    for scope in (None, source_dataset):
        result = catalog.documents(dataset_id=scope, status="available", limit=1)
        counts = result["status_counts"]
        assert counts["available"] == 1
        assert counts["documents_with_pdf"] == int("pdf" in kinds)
        assert counts["documents_with_text"] == int("text" in kinds)
        assert counts["documents_with_pdf_and_text"] == int(len(kinds) == 2)
        assert counts["source_pdf_missing"] == 2 - int("pdf" in kinds)
        assert "绝不代表PDF与文字齐备" in result["document_counts_notice"]
    details = catalog.details(source_dataset)
    assert details["document_statuses"] == counts
    assert details["document_counts_notice"] == result["document_counts_notice"]
    with catalog._connect() as db:
        db.execute("UPDATE files SET present=0 WHERE kind='pdf'")
    counts = catalog.documents(source_dataset)["status_counts"]
    assert counts["documents_with_pdf"] == 0
    assert counts["documents_with_pdf_and_text"] == 0
    assert counts["source_pdf_missing"] == 2
    assert catalog.documents("nonexistent")["status_counts"]["documents_with_text"] == 0


def test_existing_buggy_document_links_can_be_migrated_without_redownload(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    source_dataset = dataset_id(catalog, "data/point_in_time")
    job = next(
        item
        for item in catalog.documents(source_dataset)["items"]
        if item["source_url"]
    )
    paths = []
    for name, value in (("source.pdf", "%PDF-1.4"), ("source.txt", "original text")):
        relative = "data/dataset_documents/download/" + name
        paths.append(write(tmp_path, relative, value))
        catalog.record_document(job["job_id"], relative)
    artifact_dataset = dataset_id(catalog, "data/dataset_documents")
    ids = [item["file_id"] for item in catalog.files(artifact_dataset)["items"]]
    with sqlite3.connect(catalog.db_path) as db:
        for file_id in ids:
            db.execute(
                "UPDATE document_sources SET dataset_id=? WHERE file_id=?",
                (source_dataset, file_id),
            )
            db.execute("DELETE FROM file_codes WHERE file_id=?", (file_id,))
    snapshots = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert catalog.documents(artifact_dataset)["total"] == 0
    assert catalog.files(artifact_dataset, code="000001")["total"] == 0
    result = catalog.repair_document_links()
    assert result == {"managed_files_checked": 2, "downloads_performed": 0}
    assert catalog.documents(artifact_dataset)["total"] == 1
    assert catalog.files(artifact_dataset, code="000001")["total"] == 2
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == snapshots
    assert catalog.repair_document_links() == result
    assert any(
        item["document_id"] == job["job_id"]
        for item in catalog.documents(source_dataset)["items"]
    )


def test_native_and_imported_snapshots_keep_distinct_physical_dataset_roots(tmp_path):
    logical_root = "data/point_in_time"
    roots = [
        logical_root,
        "data/dataset_imports/local_first/dataset/" + logical_root,
        "data/dataset_imports/local_second/dataset/" + logical_root,
        "data/dataset_imports/local_first/dataset/"
        "data/server_imports/older/dataset/" + logical_root,
    ]
    for root in roots:
        pit(tmp_path, root)
    catalog = DatasetCatalog(tmp_path)
    for _ in range(2):
        result = catalog.build()
        assert result["indexed_files"] == 8
        assert result["unique_content_count"] == 2
        assert result["unique_security_count"] == 1
        items = catalog.list_datasets(limit=100)["items"]
        assert len(items) == 4
        assert {item["physical_root"] for item in items} == set(roots)
        identifiers = {item["dataset_id"] for item in items}
        assert len(identifiers) == 4
        for item in items:
            root = item["physical_root"]
            key = item["dataset_id"]
            assert item["logical_root"] == logical_root
            assert item["title"] == root
            assert key == module._identifier("ds_", root)
            assert catalog.resolve_dataset_root(key) == tmp_path / root
            details = catalog.details(key)
            assert details["relative_root"] == root
            assert details["physical_root"] == root
            assert details["file_count"] == 2
            files = catalog.files(key, code="000001")["items"]
            assert len(files) == 2
            assert all(f["relative_path"].startswith(root + "/") for f in files)
            assert all(f["source_copy_count"] == 4 for f in files)
            job = next(
                doc
                for doc in catalog.documents(key, code="000001")["items"]
                if doc["source_url"]
            )
            assert set(job["dataset_ids"]) == identifiers
        with sqlite3.connect(catalog.db_path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM document_sources s JOIN files f "
                    "ON s.file_id=f.file_id WHERE s.dataset_id!=f.dataset_id"
                ).fetchone()[0]
                == 0
            )


def test_legacy_dataset_schema_migrates_once_under_concurrent_construction(tmp_path):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    native_id = dataset_id(catalog, "data/point_in_time")
    with sqlite3.connect(catalog.db_path) as db:
        references = db.execute(
            "SELECT dataset_id FROM document_sources ORDER BY file_id,code"
        ).fetchall()
        db.execute(
            "CREATE TABLE legacy_datasets(dataset_id TEXT PRIMARY KEY,"
            "logical_root TEXT UNIQUE,title TEXT)"
        )
        db.execute("INSERT INTO legacy_datasets SELECT * FROM datasets")
        db.execute("DROP TABLE datasets")
        db.execute("ALTER TABLE legacy_datasets RENAME TO datasets")
    with ThreadPoolExecutor(max_workers=4) as pool:
        catalogs = list(pool.map(lambda _: DatasetCatalog(tmp_path), range(8)))
    for migrated in catalogs:
        assert migrated.details(native_id)["file_count"] == 2
    with sqlite3.connect(catalog.db_path) as db:
        assert (
            db.execute(
                "SELECT dataset_id FROM document_sources ORDER BY file_id,code"
            ).fetchall()
            == references
        )
    imported_root = "data/dataset_imports/local_new/dataset/data/point_in_time"
    pit(tmp_path, imported_root)
    catalog.build()
    assert catalog.list_datasets()["total"] == 2
    assert catalog.details(native_id)["relative_root"] == "data/point_in_time"
    imported_id = module._identifier("ds_", imported_root)
    assert catalog.details(imported_id)["relative_root"] == imported_root


def test_current_schema_reader_does_not_wait_for_running_build(tmp_path, monkeypatch):
    pit(tmp_path)
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    native_id = dataset_id(catalog, "data/point_in_time")
    connect = sqlite3.connect

    def short_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.1
        return connect(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", short_timeout)
    with catalog._connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE files SET present=0")
        reader = DatasetCatalog(tmp_path)
        assert reader.list_datasets()["total"] == 1
        assert reader.details(native_id)["file_count"] == 2
        writer.rollback()


def test_manifest_gap_diagnostics_include_source_paths_and_bounded_examples(tmp_path):
    pit(tmp_path)
    prefix = "data/server_imports/legacy"
    write(
        tmp_path,
        prefix + "/manifest.json",
        {
            "files": [
                {"path": "data/analysis/old/progress.json"},
                {"path": "data/point_in_time/market/missing.csv"},
            ]
        },
    )
    write(tmp_path, "data/private_account/secret.json", {"private": True})
    catalog = DatasetCatalog(tmp_path)
    result = catalog.build()
    status = catalog.index_status()
    stamp = datetime.fromisoformat(status["indexed_at_iso"])
    assert stamp.timestamp() == pytest.approx(status["indexed_at"])
    assert stamp.utcoffset().total_seconds() == 8 * 60 * 60
    assert result["manifest_gaps"] == status["gap_total"] == 2
    assert len(status["gap_examples"]) == 2
    reasons = [entry["reason"] for entry in status["gap_examples"]]
    assert any(
        "excluded by business-data policy" in reason
        and "source_path=data/analysis/old/progress.json" in reason
        for reason in reasons
    )
    assert any(
        "missing or excluded" in reason
        and "source_path=data/point_in_time/market/missing.csv" in reason
        for reason in reasons
    )
    key = dataset_id(catalog, "data/point_in_time")
    assert catalog.details(key)["gap_examples"] == status["gap_examples"]
    with sqlite3.connect(catalog.db_path) as db:
        for index in range(12):
            error_status, reason = (
                ("index_failed", "ValueError: changed during indexing")
                if index % 2
                else ("skipped", "directory_error:PermissionError")
            )
            db.execute(
                "INSERT INTO scan_entries VALUES(?,?,?,?,?,?,?)",
                (
                    status["build_id"],
                    f"data/analysis/error_{index:02d}",
                    "",
                    0,
                    error_status,
                    reason,
                    "",
                ),
            )
    capped = catalog.index_status()
    assert capped["gap_total"] == 14
    assert len(capped["gap_examples"]) == 10
    assert all(
        "private_account" not in item["relative_path"]
        for item in capped["gap_examples"]
    )
    catalog.build()
    assert catalog.index_status()["gap_total"] == 2
