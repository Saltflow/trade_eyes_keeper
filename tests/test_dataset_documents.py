"""Document backfill validation with real SQLite and bounded fake HTTP peers."""

import hashlib
import importlib.util
import json
import socket
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.process_lock import exclusive_process_lock
from src.data import dataset_documents as module
from src.data.dataset_documents import (
    DatasetDocumentWorker,
    DocumentBlocked,
    DocumentFailure,
    download_document,
    extract_pdf_text,
    safe_public_url,
)


class Catalog:
    def __init__(self, root, jobs):
        self.project_root = root
        self.db_path = root / "data/dataset_catalog/catalog.sqlite3"
        self.db_path.parent.mkdir(parents=True)
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE fixture_catalog(job_id TEXT)")
        self.jobs = jobs
        self.registered = []

    def document_jobs(self, limit=20, offset=0):
        return {"items": self.jobs[offset : offset + limit], "total": len(self.jobs)}

    def record_document(self, job_id, relative_path=None, status="available", error=""):
        if relative_path is None:
            return {"status": status, "error": error}
        self.registered.append((job_id, relative_path))
        job = next(item for item in self.jobs if item["job_id"] == job_id)
        paths = job.setdefault("existing_paths", [])
        if relative_path not in paths:
            paths.append(relative_path)
        key = (
            "original_path"
            if relative_path.endswith(".pdf")
            else "text_path"
            if relative_path.endswith(".txt")
            else None
        )
        if key:
            job[key] = relative_path
        return {"status": status, "error": error}


@pytest.fixture
def catalog(tmp_path):
    return Catalog(
        tmp_path,
        [
            {
                "job_id": "doc-1",
                "source_url": "https://static.cninfo.com.cn/a.pdf",
                "code": "600036",
                "existing_paths": [],
            }
        ],
    )


def states(catalog):
    with sqlite3.connect(catalog.db_path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute("SELECT * FROM document_downloads ORDER BY job_id")
        ]


def fake_extract(_pdf, text, _deadline):
    payload = b"Actual mocked parser output from fixture PDF"
    text.write_bytes(payload)
    return {
        "status": "text_extracted",
        "text_available": True,
        "text_sha256": hashlib.sha256(payload).hexdigest(),
    }


@pytest.fixture
def successful_worker(catalog, monkeypatch):
    worker = DatasetDocumentWorker(catalog)
    download = Mock()

    def save(url, destination, *_args):
        assert url == catalog.jobs[0]["source_url"]
        Path(destination).write_bytes(b"%PDF-fixture-payload")
        return {"url": url, "bytes": 20, "content_type": "application/pdf"}

    download.side_effect = save
    monkeypatch.setattr(module, "download_document", download)
    monkeypatch.setattr(worker, "_extract", fake_extract)
    return worker, download


def test_plan_is_readonly_and_keeps_missing_sources_visible(catalog, monkeypatch):
    catalog.jobs.append({"job_id": "no-url", "source_url": None, "code": "000333"})
    before = catalog.db_path.read_bytes()
    network = Mock(side_effect=AssertionError("dry-run must not use network"))
    monkeypatch.setattr(module, "download_document", network)
    worker = DatasetDocumentWorker(catalog)
    result = worker.plan(max_documents=1)
    assert result["total_sources"] == 2
    assert result["counts"]["needs_source_lookup"] == 1
    assert result["truncated"] is True
    assert result["network_requests"] == 0
    assert catalog.db_path.read_bytes() == before
    assert not worker.output.exists()


def test_download_hashes_and_text_are_registered_resume_does_not_redownload(
    successful_worker, catalog
):
    worker, download = successful_worker
    result = worker.run(max_documents=1, max_seconds=10)
    assert result["counts"] == {"succeeded": 1}
    row = states(catalog)[0]
    assert row["attempts"] == 1
    metadata = json.loads(row["result"])
    assert metadata["original_pdf_available"] is True
    assert metadata["text_available"] is True
    for kind in ("pdf", "text"):
        path = catalog.project_root / metadata[f"{kind}_path"]
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest() == metadata[f"{kind}_sha256"]
        )
    assert len(catalog.registered) == 2
    assert worker.run(max_documents=1, max_seconds=10)["processed"] == []
    assert download.call_count == 1


def test_changed_saved_text_is_repaired_without_redownloading_pdf(
    successful_worker, catalog
):
    worker, download = successful_worker
    worker.run(max_seconds=10)
    saved = json.loads(states(catalog)[0]["result"])
    (catalog.project_root / saved["text_path"]).write_text("tampered", encoding="utf-8")
    worker.run(max_seconds=10)
    assert download.call_count == 1
    assert states(catalog)[0]["attempts"] == 2
    assert states(catalog)[0]["status"] == "succeeded"


def test_missing_source_is_blocked_not_faked_and_source_update_can_resume(
    successful_worker, catalog
):
    worker, download = successful_worker
    catalog.jobs[0]["source_url"] = None
    worker.run(max_seconds=10)
    assert states(catalog)[0]["status"] == "blocked"
    assert states(catalog)[0]["error"] == "needs_source_lookup"
    assert not list(worker.output.rglob("*.pdf"))
    download.assert_not_called()
    catalog.jobs[0]["source_url"] = "https://static.cninfo.com.cn/a.pdf"
    worker.run(max_seconds=10)
    assert states(catalog)[0]["status"] == "succeeded"


def test_existing_cached_pdf_is_reused_with_no_network(successful_worker, catalog):
    worker, download = successful_worker
    path = catalog.project_root / "cache/pdf_files/existing.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-fixture")
    catalog.jobs[0]["existing_paths"] = [
        path.relative_to(catalog.project_root).as_posix()
    ]
    worker.run(max_seconds=10)
    assert states(catalog)[0]["status"] == "succeeded"
    download.assert_not_called()


def test_existing_pdf_without_source_url_can_extract_text_preserving_provenance_gap(
    successful_worker, catalog
):
    worker, download = successful_worker
    source = catalog.project_root / "cache/pdf_files/source_unknown.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-fixture")
    catalog.jobs[0].update(
        source_url=None,
        existing_paths=[source.relative_to(catalog.project_root).as_posix()],
    )
    plan = worker.plan()
    assert plan["counts"] == {"eligible_for_text_extraction": 1}
    assert plan["missing_source_urls"] == 1
    assert plan["items"][0]["source_url_missing"] is True
    result = worker.run(max_documents=1, max_seconds=10)
    assert result["counts"] == {"succeeded": 1}
    metadata = json.loads(states(catalog)[0]["result"])
    assert metadata["source_url_missing"] is True
    assert metadata["text_available"] is True
    download.assert_not_called()


def test_missing_url_gap_resumes_when_real_pdf_cache_arrives(
    successful_worker, catalog
):
    worker, download = successful_worker
    catalog.jobs[0]["source_url"] = None
    worker.run(max_documents=1, max_seconds=10)
    assert states(catalog)[0]["error"] == "needs_source_lookup"
    source = catalog.project_root / "cache/pdf_files/arrived.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-cached")
    catalog.jobs[0]["existing_paths"] = [
        source.relative_to(catalog.project_root).as_posix()
    ]
    assert worker.run(max_documents=1, max_seconds=10)["counts"] == {"succeeded": 1}
    assert json.loads(states(catalog)[0]["result"])["source_url_missing"] is True
    download.assert_not_called()


def test_scanned_pdf_remains_indexed_but_needs_ocr(
    successful_worker, catalog, monkeypatch
):
    worker, _ = successful_worker
    monkeypatch.setattr(
        worker, "_extract", lambda *_: {"status": "needs_ocr", "text_available": False}
    )
    worker.run(max_seconds=10)
    row = states(catalog)[0]
    assert row["status"] == "blocked"
    assert row["error"] == "needs_ocr"
    assert json.loads(row["result"])["original_pdf_available"] is True
    assert len(catalog.registered) == 1
    assert not list(worker.output.rglob("*.txt"))


def test_html_response_is_not_renamed_into_fake_pdf(
    successful_worker, catalog, monkeypatch
):
    worker, _ = successful_worker

    def html_response(_url, target, *_args):
        target.write_bytes(b"<html>source page, not PDF</html>")
        return {"content_type": "text/html"}

    monkeypatch.setattr(module, "download_document", html_response)
    worker.run(max_seconds=10)
    assert states(catalog)[0]["status"] == "blocked"
    assert states(catalog)[0]["error"] == "source_not_pdf_needs_source_lookup"
    assert not list(worker.output.rglob("*.pdf"))
    assert not catalog.registered
    assert list(worker.output.rglob("source_response.html"))


def test_failure_persists_backoff_and_http_retry_after_for_all_jobs(
    catalog, monkeypatch
):
    monkeypatch.setattr(
        module,
        "download_document",
        Mock(side_effect=DocumentFailure("source_http_429", retry_after=300)),
    )
    worker = DatasetDocumentWorker(catalog)
    before = time.time()
    worker.run(max_seconds=10)
    row = states(catalog)[0]
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] >= before + 300
    with sqlite3.connect(catalog.db_path) as db:
        assert (
            db.execute("SELECT next_request_at FROM document_download_rate").fetchone()[
                0
            ]
            >= before + 300
        )
    assert worker.run(max_seconds=10)["processed"] == []


def test_restart_running_is_failed_not_assumed_success(catalog, monkeypatch):
    worker = DatasetDocumentWorker(catalog)
    worker._initialize()
    with sqlite3.connect(catalog.db_path) as db:
        db.execute(
            "INSERT INTO document_downloads(job_id,status,updated) VALUES ('doc-1','running',?)",
            (time.time(),),
        )
    network = Mock(
        side_effect=AssertionError("interrupted job must not immediately replay")
    )
    monkeypatch.setattr(module, "download_document", network)
    assert worker.run(max_seconds=10)["processed"] == []
    assert states(catalog)[0]["status"] == "failed"
    assert states(catalog)[0]["error"] == "interrupted_previous_worker"
    network.assert_not_called()


def test_worker_lock_prevents_concurrent_acquisition(catalog):
    worker = DatasetDocumentWorker(catalog)
    worker._initialize()
    with exclusive_process_lock(worker.lock_path) as acquired:
        assert acquired
        with pytest.raises(RuntimeError, match="already holds"):
            worker.run(max_seconds=1)


def test_preexisting_stop_does_not_download(successful_worker):
    worker, download = successful_worker
    worker.stop.set()
    assert worker.run(max_seconds=10)["stopped"] is True
    download.assert_not_called()


def test_existing_cached_path_outside_root_is_rejected(catalog):
    catalog.jobs[0]["existing_paths"] = ["../private.pdf"]
    worker = DatasetDocumentWorker(catalog)
    worker.run(max_seconds=10)
    assert states(catalog)[0]["status"] == "blocked"
    assert "path_outside" in states(catalog)[0]["error"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/a.pdf",
        "https://127.0.0.1/a.pdf",
        "https://static.cninfo.com.cn.evil.invalid/a.pdf",
        "https://user:password@static.cninfo.com.cn/a.pdf",
        "https://static.cninfo.com.cn:8443/a.pdf",
        "https://static.cninfo.com.cn/\nheader",
        "",
    ],
)
def test_source_url_authority_and_scheme_are_strict(url):
    with pytest.raises(DocumentBlocked):
        safe_public_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1", "0.0.0.0"]
)
def test_public_domain_with_private_dns_is_rejected(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        ],
    )
    with pytest.raises(DocumentBlocked, match="non_public"):
        safe_public_url("https://static.cninfo.com.cn/a.pdf")


class Response:
    def __init__(self, data=b"%PDF-data", status=200, headers=None):
        self.data, self.offset, self.status = data, 0, status
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, limit):
        result = self.data[self.offset : self.offset + limit]
        self.offset += len(result)
        return result


def mock_peer(monkeypatch, response):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    connection = Mock(sock=None)
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(module, "_PinnedHTTPSConnection", factory)
    return connection, factory


def test_download_pins_validated_ip_and_keeps_hostname_for_tls(monkeypatch, tmp_path):
    connection, factory = mock_peer(monkeypatch, Response())
    output = tmp_path / "download.part"
    result = download_document(
        "https://static.cninfo.com.cn/a.pdf",
        output,
        time.monotonic() + 10,
        threading.Event(),
        lambda _: None,
    )
    assert factory.call_args.args[:3] == ("static.cninfo.com.cn", 443, "8.8.8.8")
    assert (
        connection.request.call_args.kwargs["headers"]["Host"] == "static.cninfo.com.cn"
    )
    assert output.read_bytes() == b"%PDF-data"
    assert result["bytes"] == 9


def test_redirect_cannot_reach_local_network(monkeypatch, tmp_path):
    _, factory = mock_peer(
        monkeypatch,
        Response(
            status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"}
        ),
    )
    with pytest.raises(DocumentBlocked):
        download_document(
            "https://static.cninfo.com.cn/a.pdf",
            tmp_path / "part",
            time.monotonic() + 10,
            threading.Event(),
            lambda _: None,
        )
    assert factory.call_count == 1


def test_download_stops_at_size_cap_and_rejects_partial_body(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "MAX_DOCUMENT_BYTES", 5)
    mock_peer(monkeypatch, Response(data=b"1234567"))
    with pytest.raises(DocumentBlocked, match="size_limit"):
        download_document(
            "https://static.cninfo.com.cn/a.pdf",
            tmp_path / "part",
            time.monotonic() + 10,
            threading.Event(),
            lambda _: None,
        )
    mock_peer(monkeypatch, Response(data=b"123", headers={"Content-Length": "4"}))
    with pytest.raises(DocumentFailure, match="incomplete"):
        download_document(
            "https://static.cninfo.com.cn/a.pdf",
            tmp_path / "part",
            time.monotonic() + 10,
            threading.Event(),
            lambda _: None,
        )


def test_real_pdf_text_extraction_and_scanned_page_are_distinct(tmp_path):
    if not importlib.util.find_spec("fitz"):
        pytest.skip(
            "PyMuPDF is not installed; no new PDF dependencies are installed by tests"
        )
    import fitz

    pdf, text = tmp_path / "real.pdf", tmp_path / "real.txt"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Actual PDF text extraction fixture")
    document.save(pdf)
    document.close()
    result = extract_pdf_text(pdf, text)
    assert result["status"] == "text_extracted"
    assert "Actual PDF text extraction fixture" in text.read_text(encoding="utf-8")
    blank = tmp_path / "scanned.pdf"
    document = fitz.open()
    document.new_page()
    document.save(blank)
    document.close()
    result = extract_pdf_text(blank, text)
    assert result["status"] == "needs_ocr"
    assert not text.exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_documents": 0},
        {"max_documents": True},
        {"max_seconds": 0},
        {"max_seconds": float("nan")},
    ],
)
def test_execution_budgets_are_validated(catalog, kwargs):
    with pytest.raises(ValueError):
        DatasetDocumentWorker(catalog).run(**kwargs)


def test_no_extractor_never_claims_fake_text(tmp_path, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    result = extract_pdf_text(tmp_path / "unread.pdf", tmp_path / "text.txt")
    assert result == {"status": "needs_extractor", "text_available": False}
    assert not (tmp_path / "text.txt").exists()


def test_pdftotext_fallback_has_fixed_arguments_and_real_text_hash(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/pdftotext")
    pdf, output = tmp_path / "source.pdf", tmp_path / "source.txt"

    def extract(command, **kwargs):
        assert command == [
            "/usr/bin/pdftotext",
            "-enc",
            "UTF-8",
            "-f",
            "1",
            "-l",
            "1000",
            str(pdf),
            str(output),
        ]
        assert kwargs["timeout"] == 45
        assert "shell" not in kwargs
        output.write_text("first page\fsecond page\f", encoding="utf-8")
        return Mock(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", extract)
    result = extract_pdf_text(pdf, output)
    assert result["status"] == "text_extracted"
    assert result["parser"] == "pdftotext"
    assert result["pages"] == 2
    assert result["text_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_missing_urls_do_not_starve_eligible_jobs(successful_worker, catalog):
    worker, download = successful_worker
    eligible = catalog.jobs[0]
    catalog.jobs[:] = [
        {"job_id": f"missing-{index}", "source_url": None} for index in range(120)
    ] + [eligible]
    # The fixture transport verifies catalog.jobs[0], so use a simple PDF writer.
    download.side_effect = lambda _url, destination, *_: (
        destination.write_bytes(b"%PDF-fixture"),
        {"bytes": 12},
    )[1]
    result = worker.run(max_documents=1, max_seconds=15)
    assert result["total_sources"] == 121
    assert result["counts"] == {"blocked": 120, "succeeded": 1}
    assert result["processed"] == [{"job_id": "doc-1", "status": "succeeded"}]
    assert all(
        row["attempts"] == 0
        for row in states(catalog)
        if row["job_id"].startswith("missing-")
    )


@pytest.mark.parametrize("boundary", ["cancel", "deadline", "max_documents"])
def test_execution_boundary_stops_before_scanning_missing_url_tail(
    catalog, monkeypatch, boundary
):
    catalog.jobs.extend(
        {"job_id": f"missing-{index}", "source_url": None} for index in range(250)
    )
    worker = DatasetDocumentWorker(catalog)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    pages = Mock(wraps=catalog.document_jobs)
    monkeypatch.setattr(catalog, "document_jobs", pages)
    cached_pdf = Mock(wraps=worker._has_cached_pdf)
    monkeypatch.setattr(worker, "_has_cached_pdf", cached_pdf)

    def finish_first(_job, _deadline):
        if boundary == "cancel":
            worker.stop.set()
        elif boundary == "deadline":
            clock[0] = 111.0
        return {"status": "text_extracted"}

    monkeypatch.setattr(worker, "_archive", finish_first)
    result = worker.run(
        max_documents=1 if boundary == "max_documents" else 20, max_seconds=10
    )
    # All sources remain indexed, but the execution scan stops at its boundary
    # before inspecting the missing-URL tail or fetching more catalog pages.
    assert result["source_sync_incomplete"] is False
    assert result["total_sources"] == 251
    assert result["counts"] == {"blocked": 250, "succeeded": 1}
    assert result["processed"] == [{"job_id": "doc-1", "status": "succeeded"}]
    assert [call.kwargs["offset"] for call in pages.call_args_list] == [0, 100, 200, 0]
    assert cached_pdf.call_count == 250
    assert result["stopped"] is (boundary == "cancel")
    assert result["time_budget_exhausted"] is (boundary == "deadline")


def test_same_url_across_datasets_downloads_once_but_registers_both(
    successful_worker, catalog, monkeypatch
):
    worker, download = successful_worker
    catalog.jobs.append(
        {
            "job_id": "other-dataset-doc",
            "source_url": catalog.jobs[0]["source_url"],
            "existing_paths": [],
        }
    )
    extract = Mock(side_effect=fake_extract)
    monkeypatch.setattr(worker, "_extract", extract)
    result = worker.run(max_documents=2, max_seconds=15)
    assert result["counts"] == {"succeeded": 2}
    assert download.call_count == 1
    assert extract.call_count == 1
    assert len(catalog.registered) == 4
    assert catalog.jobs[0]["original_path"] == catalog.jobs[1]["original_path"]
    assert catalog.jobs[0]["text_path"] == catalog.jobs[1]["text_path"]


def test_real_catalog_indexes_download_artifacts_without_new_sourceless_jobs(
    tmp_path, monkeypatch
):
    from src.data.dataset_catalog import DatasetCatalog

    source = tmp_path / "data/point_in_time/fundamentals/600036.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "code": "600036",
                "source_url": "https://static.cninfo.com.cn/a.pdf",
                "period_end": "2025-12-31",
                "title": "Annual report",
            }
        ),
        encoding="utf-8",
    )
    catalog = DatasetCatalog(tmp_path)
    catalog.build()
    before = catalog.document_jobs()
    assert before["total"] == 1
    worker = DatasetDocumentWorker(catalog)
    monkeypatch.setattr(
        module,
        "download_document",
        lambda _url, destination, *_: (
            destination.write_bytes(b"%PDF-fixture"),
            {"bytes": 12},
        )[1],
    )
    monkeypatch.setattr(worker, "_extract", fake_extract)
    assert worker.run(max_documents=1, max_seconds=20)["counts"] == {"succeeded": 1}
    documents = catalog.document_jobs()
    assert documents["total"] == 1
    saved = documents["items"][0]
    assert saved["download_status"] == "succeeded"
    assert saved["source_pdf_missing"] is False
    assert len(saved["existing_paths"]) == 2
