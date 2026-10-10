"""Offline fixtures test transport contracts, not real research acceptance."""

import hashlib
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.fundamental_quant.pdf_sessions import (
    MIB,
    DeepSeekFiles,
    PageDocument,
    PDFBatchReader,
    PDFSessionError,
    validate_request,
)
from src.fundamental_quant.store import ResearchStore


@pytest.fixture
def reader(tmp_path):
    store = ResearchStore(tmp_path / "store")
    return PDFBatchReader(store, tmp_path / "images", [tmp_path], project_root=tmp_path)


def make_document(root, count=51):
    path = root / "original.pdf"
    pages = []
    for number in range(1, count + 1):
        image = Image.new("RGB", (144, 200), "white")
        draw = ImageDraw.Draw(image)
        draw.text((5, 185), f"Footnote {number}", fill="black")
        draw.text((5, 5), f"Page {number}", fill="black")
        pages.append(image)
    pages[0].save(
        path, format="PDF", save_all=True, append_images=pages[1:], resolution=72
    )
    return PageDocument(
        "fixture",
        "a_share:fixture",
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "2025-12-31",
        "2026-03-01",
        "annual",
        "Synthetic test only",
    )


def test_contiguous_full_pages_last_partial_and_restart(reader, tmp_path):
    document = make_document(tmp_path)
    batch = reader.render_batch(document)
    assert [p["page"] for p in batch["pages"]] == list(range(1, 51))
    assert batch["next_page"] == 51
    assert batch["estimated_image_tokens"] == 50 * 1024
    assert all(p["zoom_box"] is None for p in batch["pages"])
    assert all((p["width"], p["height"]) == (288, 400) for p in batch["pages"])
    restarted = PDFBatchReader(reader.store, reader.root, [tmp_path])
    tail = restarted.render_batch(document, batch["next_page"])
    assert [p["page"] for p in tail["pages"]] == [51]
    assert tail["next_page"] is None
    assert restarted.render_batch(document)["pages"] == batch["pages"]


def test_hash_and_allowed_root_fail_closed(reader, tmp_path):
    doc = make_document(tmp_path, 1)
    with pytest.raises(PDFSessionError, match="pdf_hash_mismatch"):
        reader.render_batch(replace(doc, sha256="0" * 64))
    forbidden = PDFBatchReader(reader.store, reader.root, [tmp_path / "other"])
    with pytest.raises(PDFSessionError, match="pdf_outside_allowed_roots"):
        forbidden.render_batch(doc)


def test_zoom_is_explicit_separate_image(reader, tmp_path):
    doc = make_document(tmp_path, 1)
    original = reader.render_batch(doc)["pages"][0]
    zoom = reader.render_zoom(doc, 1, [0, 0.5, 1, 1])
    assert zoom["source_ref"] == original["source_ref"]
    assert zoom["image_sha256"] != original["image_sha256"]
    assert zoom["zoom_box"] == [0, 0.5, 1, 1]
    assert reader.render_batch(doc)["pages"][0] == original
    with pytest.raises(PDFSessionError, match="invalid_zoom"):
        reader.render_zoom(doc, 1, [-1, 0, 1, 1])


def test_source_discovery_deduplicates_without_text(reader, tmp_path):
    doc = make_document(tmp_path, 1)
    source = {
        "document_path": "original.pdf",
        "document_sha256": doc.sha256,
        "issuer_id": doc.issuer_id,
        "period_end": doc.period_end,
        "published_at": doc.published_at,
        "kind": "annual",
        "text": "",
    }
    assert len(reader.discover({"sources": [source, dict(source, page=2)]})) == 1
    with pytest.raises(PDFSessionError, match="metadata_conflict"):
        reader.discover({"sources": [source, dict(source, period_end="2024-12-31")]})


def test_catalog_discovers_original_without_extracted_pages(reader, tmp_path):
    original = make_document(tmp_path, 1)
    catalog = tmp_path / "data/dataset_catalog/catalog.sqlite3"
    catalog.parent.mkdir(parents=True)
    with sqlite3.connect(catalog) as db:
        db.executescript("""
            CREATE TABLE documents(document_id TEXT,title TEXT,publication_date TEXT,source_url TEXT);
            CREATE TABLE document_sources(document_id TEXT,code TEXT);
            CREATE TABLE document_artifacts(document_id TEXT,file_id TEXT,kind TEXT);
            CREATE TABLE files(file_id TEXT,relative_path TEXT,sha256 TEXT,present INT);
        """)
        db.execute(
            "INSERT INTO documents VALUES(?,?,?,?)",
            ("doc", "测试公司2025年第三季度报告", "2025-10-30", "https://static.cninfo.com.cn/fixture.pdf"),
        )
        db.execute("INSERT INTO document_sources VALUES('doc','fixture')")
        db.execute("INSERT INTO document_artifacts VALUES('doc','file','pdf')")
        db.execute(
            "INSERT INTO files VALUES(?,?,?,1)",
            ("file", "original.pdf", original.sha256),
        )
    docs = reader.discover_catalog(
        {"id": "a_share:fixture", "code": "fixture"}, date(2026, 10, 6)
    )
    assert len(docs) == 1
    assert docs[0].period_end == "2025-09-30"
    assert docs[0].kind == "quarterly"
    assert docs[0].url == "https://static.cninfo.com.cn/fixture.pdf"


def test_request_limits():
    page = {"bytes": 1024, "width": 4000, "height": 4000}
    assert validate_request([], [page] * 600)["estimated_image_tokens"] == 614400
    with pytest.raises(PDFSessionError, match="count_limit"):
        validate_request([], [page] * 601)
    with pytest.raises(PDFSessionError, match="total_image_size"):
        validate_request([], [dict(page, bytes=64 * MIB)] * 4)
    with pytest.raises(PDFSessionError, match="dimension"):
        validate_request([], [dict(page, width=4097)] * 15)


class Response:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return self.body

    def close(self):
        pass


class FakeHTTP:
    def __init__(self):
        self.uploads = []
        self.gets = []
        self.retrieve_status = 200
        self.clock = 1000

    def post(self, url, **kwargs):
        self.uploads.append((url, kwargs))
        return Response(
            200,
            {
                "id": f"file-api-test-{len(self.uploads)}",
                "object": "file",
                "bytes": len(kwargs["files"]["file"][1]),
                "expires_at": self.clock + 10000,
            },
        )

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return Response(
            self.retrieve_status,
            {
                "id": url.rsplit("/", 1)[-1],
                "object": "file",
                "expires_at": 11000,
                "bytes": len(self.uploads[-1][1]["files"]["file"][1]),
            },
        )


def test_files_reuse_scope_expiry_and_external_deletion(reader, tmp_path):
    page = reader.render_batch(make_document(tmp_path, 1))["pages"][0]
    http = FakeHTTP()
    config = {"llm": {"api_key": "fixture-key"}}
    files = DeepSeekFiles(reader.store, config, http=http, clock=lambda: http.clock)
    first = files.ensure_uploaded(page)
    assert first == {"type": "file", "file_id": "file-api-test-1"}
    assert files.ensure_uploaded(page) == first
    assert len(http.uploads) == 1 and not http.gets
    http.clock += 400
    assert files.ensure_uploaded(page) == first
    assert len(http.gets) == 1
    http.retrieve_status = 404
    http.clock += 400
    assert files.ensure_uploaded(page)["file_id"] == "file-api-test-2"
    another_account = DeepSeekFiles(
        reader.store,
        {"llm": {"api_key": "other-fixture-key"}},
        http=http,
        clock=lambda: http.clock,
    )
    assert another_account.ensure_uploaded(page)["file_id"] == "file-api-test-3"
    http.clock += 12000
    assert files.ensure_uploaded(page)["file_id"] == "file-api-test-4"
    assert http.uploads[0][1]["data"]["purpose"] == "user_data"
    assert http.uploads[0][1]["allow_redirects"] is False


def test_files_rate_limit_does_not_silently_upload_again(reader, tmp_path):
    page = reader.render_batch(make_document(tmp_path, 1))["pages"][0]
    http = FakeHTTP()
    files = DeepSeekFiles(
        reader.store,
        {"llm": {"api_key": "fixture"}},
        http=http,
        clock=lambda: http.clock,
    )
    files.ensure_uploaded(page)
    http.clock += 400
    http.retrieve_status = 429
    with pytest.raises(PDFSessionError, match="files_http_429") as error:
        files.ensure_uploaded(page)
    assert error.value.retryable
    assert len(http.uploads) == 1


def test_corrupt_render_cache_rerenders(reader, tmp_path):
    doc = make_document(tmp_path, 1)
    page = reader.render_batch(doc)["pages"][0]
    Path(page["image_path"]).write_bytes(b"corrupt")
    recovered = reader.render_batch(doc)["pages"][0]
    assert (
        hashlib.sha256(Path(recovered["image_path"]).read_bytes()).hexdigest()
        == page["image_sha256"]
    )
