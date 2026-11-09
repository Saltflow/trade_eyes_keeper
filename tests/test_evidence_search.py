"""Bounded indexed evidence search; no network, OCR, or model calls."""

import hashlib
import json
import os

import pytest

from src.data import dataset_catalog as module
from src.data.dataset_catalog import DatasetCatalog
from src.data_hub import DataHub


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def catalog_with(root, files):
    for name, text in files.items():
        write(root, name, text)
    catalog = DatasetCatalog(root)
    catalog.index_paths(list(files))
    return catalog


@pytest.fixture(autouse=True)
def never_acquire(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("search must never acquire or parse materials")

    monkeypatch.setattr(DataHub, "ensure_parsed", forbidden)
    monkeypatch.setattr(DataHub, "ensure_original", forbidden)
    monkeypatch.setattr(DataHub, "parse_bytes", forbidden)


def test_cross_file_body_search_not_filename_and_numbered_citations(
    tmp_path, monkeypatch
):
    catalog = catalog_with(
        tmp_path,
        {
            "data/analysis/000001_notes.md": "# Evidence\n--- PAGE 17 ---\n会计政策变更附注\n",
            "data/analysis/000002_notes.txt": "会计政策变更：重述比较期\n",
            "data/email_archive/20261109_report.html": "<html><head><style>秘密词</style></head><body><script>秘密词</script>"
            "<!--秘密词--><table><tr><td>会计政策变更</td><td>邮件正文</td></tr></table></body></html>",
        },
    )
    monkeypatch.setattr(
        module, "iter_catalog_entries", lambda *_: pytest.fail("no crawl")
    )
    result = catalog.search_text("会计政策变更")
    assert len(result["results"]) == 3
    by_kind = {r["line_basis"]: r for r in result["results"]}
    html = by_kind["rendered_html_text"]
    assert "邮件正文" in html["text"] and html["page"] is None
    assert html["start_line"] == 1
    report = next(r for r in result["results"] if r["path"].endswith(".md"))
    assert report["page"] == 17 and report["start_line"] == 3
    assert report["data_id"] == report["file_id"]
    assert len(report["sha256"]) == len(report["text_sha256"]) == 64
    assert catalog.search_text("秘密词")["results"] == []


@pytest.mark.parametrize("marker", ["[PAGE 7]", "--- PAGE 7 ---", "# 页 7"])
def test_existing_text_page_marker_conventions(tmp_path, marker):
    catalog = catalog_with(tmp_path, {"data/analysis/notes.md": marker + "\nneedle"})
    assert catalog.search_text("needle")["results"][0]["page"] == 7


def test_snippet_keeps_match_after_unicode_casefold_expansion(tmp_path):
    catalog = catalog_with(tmp_path, {"data/analysis/notes.txt": "ß" * 1000 + "needle"})
    match = catalog.search_text("NEEDLE")["results"][0]
    assert "needle" in match["text"] and match["snippet_truncated"]


def test_company_kind_and_date_filters_run_on_index_before_open(tmp_path, monkeypatch):
    catalog = catalog_with(
        tmp_path,
        {
            "data/analysis/000001_notes.txt": "变更附注",
            "data/analysis/000002_notes.txt": "变更附注",
            "data/email_archive/20261109_report.html": "<p>变更附注</p>",
            "data/email_archive/20261108_report.html": "<p>变更附注</p>",
        },
    )
    original = catalog._search_text_source
    opened = []

    def observe(row):
        opened.append(row["relative_path"])
        return original(row)

    monkeypatch.setattr(catalog, "_search_text_source", observe)
    result = catalog.search_text("变更附注", code="000001", kind="text")
    assert len(result["results"]) == 1
    assert opened == ["data/analysis/000001_notes.txt"]
    opened.clear()
    result = catalog.search_text(
        "变更附注", kind="report", date_from="2026-11-09", date_to="2026-11-09"
    )
    assert len(result["results"]) == 1
    assert opened == ["data/email_archive/20261109_report.html"]


def test_cursor_pages_within_and_across_files_without_losing_hits(tmp_path):
    catalog = catalog_with(
        tmp_path,
        {
            "data/analysis/000001_notes.txt": "needle one\nneedle two\nneedle three",
            "data/analysis/000002_notes.txt": "needle four\nneedle five",
        },
    )
    cursor, hits = None, []
    for _ in range(8):
        result = catalog.search_text("needle", limit=1, cursor=cursor, max_files=1)
        hits.extend((r["file_id"], r["start_line"]) for r in result["results"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and len(hits) == len(set(hits)) == 5


def test_no_match_page_still_provides_candidate_cursor(tmp_path):
    catalog = catalog_with(
        tmp_path,
        {f"data/analysis/00000{i}_notes.txt": "unrelated" for i in range(1, 4)},
    )
    first = catalog.search_text("absent", max_files=1)
    assert first["results"] == [] and first["next_cursor"]
    assert first["scanned_files"] == 1
    assert not first["search_complete"]


def test_cursor_is_bound_to_query_filters_and_index_version(tmp_path):
    catalog = catalog_with(
        tmp_path, {"data/analysis/000001_notes.txt": "needle\nneedle"}
    )
    first = catalog.search_text("needle", limit=1)
    with pytest.raises(ValueError, match="cursor"):
        catalog.search_text("other", cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="cursor"):
        catalog.search_text("needle", code="000001", cursor=first["next_cursor"])
    catalog.index_paths(["data/analysis/000001_notes.txt"])
    with pytest.raises(ValueError, match="cursor"):
        catalog.search_text("needle", cursor=first["next_cursor"])


def test_business_tools_first_page_refresh_and_new_session_cursor_are_consistent(
    tmp_path, monkeypatch
):
    from src.interactive.assistant.data_tools import BusinessDataTools

    write(
        tmp_path,
        "data/email_archive/20261109_report.html",
        "<p>needle first</p><p>needle second</p>",
    )
    first_tools = BusinessDataTools(tmp_path)
    first = first_tools.search(query="needle", limit=1)
    assert first["build_id"] and first["next_cursor"]
    write(tmp_path, "data/email_archive/20261110_report.html", "<p>needle unseen</p>")
    second_tools = BusinessDataTools(tmp_path)
    monkeypatch.setattr(
        second_tools, "_refresh_public", lambda: pytest.fail("cursor must not refresh")
    )
    second = second_tools.search(query="needle", limit=1, cursor=first["next_cursor"])
    assert second["build_id"] == first["build_id"]
    assert second["results"][0]["text"] == "needle second"
    assert second["next_cursor"] is None
    assert first["results"][0]["file_id"] == second["results"][0]["file_id"]


def test_oversize_changed_unsupported_and_unparsed_are_gaps_not_absence(
    tmp_path, monkeypatch
):
    catalog = catalog_with(
        tmp_path,
        {
            "data/analysis/000001_large.txt": "x" * 300,
            "data/analysis/000002_changed.txt": "needle",
            "data/analysis/000003_unparsed.pdf": "%PDF-existing-only",
            "data/analysis/000004_sheet.xlsx": "fixture-binary",
        },
    )
    write(tmp_path, "data/analysis/000002_changed.txt", "changed")
    monkeypatch.setattr(module, "SEARCH_FILE_BYTES", 256)
    result = catalog.search_text("needle")
    reasons = {g["reason"] for g in result["coverage_gaps"]}
    assert {
        "file_size_limit",
        "content_changed_refresh_catalog",
        "existing_text_parse_unavailable",
        "unsupported_format_no_existing_text",
    } <= reasons
    assert not result["results"] and not result["search_complete"]


def test_missing_company_or_date_metadata_excluded_but_explicit(tmp_path):
    catalog = catalog_with(tmp_path, {"data/analysis/notes.txt": "needle"})
    result = catalog.search_text("needle", code="000001", date_from="2026-01-01")
    assert result["candidate_files"] == result["scanned_files"] == 0
    reasons = {g["reason"] for g in result["coverage_gaps"]}
    assert {"company_metadata_unavailable", "date_metadata_unavailable"} <= reasons


def test_never_reads_unindexed_or_private_files(tmp_path):
    catalog = catalog_with(tmp_path, {"data/analysis/public.txt": "ordinary"})
    write(tmp_path, "data/analysis/unindexed.txt", "needle")
    write(tmp_path, "data/private/secret.txt", "needle")
    assert catalog.search_text("needle")["results"] == []
    assert (
        catalog.search_text("needle", file_id="../../private/secret.txt")["results"]
        == []
    )


def test_indexed_content_changed_to_credentials_is_never_returned(tmp_path):
    catalog = catalog_with(tmp_path, {"data/analysis/public.txt": "ordinary"})
    write(tmp_path, "data/analysis/public.txt", 'api_key="needle-secret"')
    result = catalog.search_text("needle")
    assert result["results"] == []
    assert any(
        g["reason"] == "content_changed_refresh_catalog"
        for g in result["coverage_gaps"]
    )


def test_same_size_and_timestamp_change_still_fails_hash_verification(tmp_path):
    catalog = catalog_with(tmp_path, {"data/analysis/public.txt": "needle original"})
    path = tmp_path / "data/analysis/public.txt"
    before = path.stat()
    path.write_text("needle changed!", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = catalog.search_text("needle")
    assert result["results"] == []
    assert any(
        g["reason"] == "content_changed_refresh_catalog"
        for g in result["coverage_gaps"]
    )


def test_byte_budget_stops_before_next_read_and_can_resume(tmp_path, monkeypatch):
    catalog = catalog_with(
        tmp_path, {f"data/analysis/00000{i}.txt": "needle1234" for i in range(1, 4)}
    )
    monkeypatch.setattr(module, "SEARCH_TOTAL_BYTES", 15)
    first = catalog.search_text("needle")
    assert first["scanned_files"] == 1 and first["next_cursor"]
    second = catalog.search_text("needle", cursor=first["next_cursor"])
    assert second["scanned_files"] == 1
    assert first["results"][0]["file_id"] != second["results"][0]["file_id"]


def cached_pdf(catalog, path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    hub = DataHub(catalog.material_authority)
    parsed = {
        "sha256": digest,
        "complete": True,
        "content_type": "application/pdf",
        "content": [{"type": "text", "text": "needle audited note"}],
        "page_texts": {"12": "needle audited note", "13": "needle comparison"},
    }
    hub.parsed._save(parsed)
    return hub, digest


def test_existing_pdf_cache_search_preserves_page_and_never_parses(tmp_path):
    catalog = catalog_with(
        tmp_path, {"data/analysis/000001_report.pdf": "%PDF-fixture"}
    )
    path = tmp_path / "data/analysis/000001_report.pdf"
    _, digest = cached_pdf(catalog, path)
    result = catalog.search_text("needle", code="000001")
    assert [r["page"] for r in result["results"]] == [12, 13]
    assert all(r["sha256"] == digest for r in result["results"])
    assert all(r["line_basis"] == "existing_parsed_text" for r in result["results"])


def test_indexed_public_material_reference_returns_stable_material_id(tmp_path):
    catalog = DatasetCatalog(tmp_path)
    original = write(tmp_path, "original.pdf", "%PDF-fixture")
    hub, digest = cached_pdf(catalog, original)
    record = hub.register_original(original)
    marker = tmp_path / "data/dataset_documents/report.material-ref.json"
    hub.write_reference(marker, record)
    catalog.index_paths([marker.relative_to(tmp_path).as_posix()])
    result = catalog.search_text("needle")
    assert len(result["results"]) == 2
    assert all(
        r["data_id"] == r["material_id"] == f"material:{digest}"
        for r in result["results"]
    )
    assert all(r["source_file_id"].startswith("file_") for r in result["results"])


def test_register_public_web_material_is_incremental_and_searchable_in_new_session(
    tmp_path, monkeypatch
):
    catalog = DatasetCatalog(tmp_path)
    original = write(
        tmp_path, "public.blob", "<html><body><p>会计政策重述原文</p></body></html>"
    )
    hub = DataHub(catalog.material_authority)
    record = hub.register_original(original, url="https://example.test/notice")
    callback_record = {
        "sha256": record["sha256"],
        "content_type": "text/html",
        "title": "公开附注",
        "final_url": "https://example.test/notice",
    }
    monkeypatch.setattr(catalog, "build", lambda: pytest.fail("must not rebuild"))
    result = catalog.register_material(callback_record)
    marker = tmp_path / result["reference_path"]
    assert marker.stat().st_size < 1024
    assert original.exists()
    assert catalog.register_material(callback_record)["file_id"] == result["file_id"]
    reopened = DatasetCatalog(tmp_path)
    search = reopened.search_text("政策重述")
    assert search["results"][0]["material_id"] == result["material_id"]
    assert search["results"][0]["page"] is None
    assert search["results"][0]["line_basis"] == "rendered_html_text"
    with reopened._connect() as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_registration_failure_keeps_original_and_reference_for_retry(
    tmp_path, monkeypatch
):
    catalog = DatasetCatalog(tmp_path)
    path = write(tmp_path, "public.txt", "needle archived")
    hub = DataHub(catalog.material_authority)
    record = hub.register_original(path)
    real_index = catalog.index_paths
    monkeypatch.setattr(
        catalog, "index_paths", lambda _: {"indexed_files": 0, "index_failures": 1}
    )
    with pytest.raises(ValueError, match="registration failed"):
        catalog.register_material(record)
    marker = (
        tmp_path
        / "data/dataset_documents/web"
        / f"{record['sha256']}.material-ref.json"
    )
    assert marker.exists() and path.exists()
    monkeypatch.setattr(catalog, "index_paths", real_index)
    catalog.register_material(record)
    assert (
        catalog.search_text("needle")["results"][0]["data_id"]
        == f"material:{record['sha256']}"
    )


def test_registration_rejects_unknown_or_conflicting_material_identity(tmp_path):
    catalog = DatasetCatalog(tmp_path)
    with pytest.raises(ValueError):
        catalog.register_material({"sha256": "a" * 64})
    with pytest.raises(ValueError):
        catalog.register_material(
            {"sha256": "a" * 64, "material_id": "material:" + "b" * 64}
        )


def test_mislabeled_pdf_is_not_searched_as_plain_web_text(tmp_path):
    catalog = DatasetCatalog(tmp_path)
    path = write(tmp_path, "public.blob", "%PDF-needle is not a text parse")
    record = DataHub(catalog.material_authority).register_original(path)
    catalog.register_material({**record, "content_type": "text/html"})
    result = catalog.search_text("needle")
    assert result["results"] == []
    assert any(
        g["reason"] == "existing_text_parse_unavailable"
        for g in result["coverage_gaps"]
    )


def test_parse_changes_between_pages_are_reported_without_stale_citations(tmp_path):
    catalog = catalog_with(
        tmp_path, {"data/analysis/000001_report.pdf": "%PDF-fixture"}
    )
    hub, digest = cached_pdf(catalog, tmp_path / "data/analysis/000001_report.pdf")
    first = catalog.search_text("needle", limit=1)
    cache = hub.parsed._path(digest)
    from src.data_hub.storage import fingerprint

    envelope = json.loads(cache.read_text("utf-8"))
    envelope["parsed"]["page_texts"]["13"] = "needle revised"
    envelope["payload_sha256"] = fingerprint(envelope["parsed"])
    cache.write_text(json.dumps(envelope), encoding="utf-8")
    second = catalog.search_text("needle", cursor=first["next_cursor"])
    assert second["results"] == []
    assert any(
        g["reason"] == "parsed_text_changed_restart_search"
        for g in second["coverage_gaps"]
    )


@pytest.mark.parametrize(
    "options",
    [
        {"query": "x"},
        {"limit": True},
        {"max_files": 0},
        {"cursor": "not-base64"},
        {"date_from": "2026-99-99"},
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
    ],
)
def test_invalid_parameters_rejected(tmp_path, options):
    with pytest.raises(ValueError):
        DatasetCatalog(tmp_path).search_text(**{"query": "needle", **options})


def test_cninfo_search_keeps_dedicated_identity_constraint(tmp_path):
    with pytest.raises(ValueError, match="document_id"):
        DatasetCatalog(tmp_path).search_document_text("doc_arbitrary", "needle")


def test_search_citation_can_be_read_by_pdf_file_id_or_unindexed_material_id(tmp_path):
    from src.interactive.assistant.data_tools import BusinessDataTools

    catalog = catalog_with(tmp_path, {"data/analysis/report.pdf": "%PDF-fixture"})
    path = tmp_path / "data/analysis/report.pdf"
    hub, digest = cached_pdf(catalog, path)
    hub.register_original(path)
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    hit = catalog.search_text("comparison")["results"][0]
    for data_id in (hit["data_id"], f"material:{digest}"):
        read = tools.read(data_id, offset=hit["start_line"] - 1, limit=1)
        assert read["items"] == [
            {"line": hit["start_line"], "page": 13, "text": hit["text"]}
        ]
        assert read["text_sha256"] == hit["text_sha256"]
        assert read["line_basis"] == hit["line_basis"]
    registered = catalog.register_material(
        {"sha256": digest, "content_type": "application/pdf"}
    )
    (tmp_path / registered["reference_path"]).unlink()
    # The directory is a discovery index, not the authority of a cached material.
    read = tools.read(f"material:{digest}", offset=hit["start_line"] - 1, limit=1)
    assert read["items"][0]["text"] == hit["text"]


@pytest.mark.parametrize(
    "media,body",
    [
        ("text/plain", "needle first\nneedle second"),
        ("text/html", "<p>needle first</p><p>needle second</p>"),
    ],
)
def test_registered_unparsed_blob_reads_same_lines_as_search(tmp_path, media, body):
    from src.interactive.assistant.data_tools import BusinessDataTools

    catalog = DatasetCatalog(tmp_path)
    original = write(tmp_path, "original.blob", body)
    record = DataHub(catalog.material_authority).register_original(original)
    catalog.register_material({**record, "content_type": media})
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    hit = catalog.search_text("second")["results"][0]
    read = tools.read(hit["data_id"], offset=hit["start_line"] - 1, limit=1)
    assert read["items"][0] == {"line": 2, "page": None, "text": "needle second"}
    assert read["text_sha256"] == hit["text_sha256"]


@pytest.mark.parametrize("body", ["", "needle partial text"])
def test_pdf_visual_pages_are_coverage_gaps_in_search_and_both_read_id_forms(
    tmp_path, body
):
    from src.interactive.assistant.data_tools import BusinessDataTools

    catalog = catalog_with(tmp_path, {"data/analysis/report.pdf": "%PDF-visual"})
    path = tmp_path / "data/analysis/report.pdf"
    hub = DataHub(catalog.material_authority)
    record = hub.register_original(path)
    hub.parsed._save(
        {
            "sha256": record["sha256"],
            "complete": True,
            "content_type": "application/pdf",
            "page_texts": {"1": body, "2": ""},
            "content": [
                {"type": "text", "text": body},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,eA=="},
                },
            ],
            "images": [{"page": 2, "base64": "eA=="}],
        }
    )
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    search = catalog.search_text("absent")
    assert not search["search_complete"]
    assert any(
        "visual_text_unavailable" in gap["reason"] for gap in search["coverage_gaps"]
    )
    file_id = catalog.files()["items"][0]["file_id"]
    for data_id in (file_id, f"material:{record['sha256']}"):
        read = tools.read(data_id)
        assert not read["scan_complete"] and read["truncated"]
        assert any(
            "visual_text_unavailable" in gap and "pages=2" in gap
            for gap in read["coverage_gaps"]
        )
        assert read["next_offset"] is None


def test_large_company_date_candidate_query_does_not_rescan_sources_per_file(
    tmp_path, monkeypatch
):
    """Production-sized metadata, bounded VM work, without creating blob files."""
    catalog = DatasetCatalog(tmp_path)
    file_count, document_count = 34558, 9275

    def identity(n):
        return f"file_{n:024x}"

    with catalog._connect() as db:
        db.executemany(
            "INSERT INTO files(file_id,relative_path,bytes,kind,date_start,date_end,present) "
            "VALUES(?,?,?,'text','','',1)",
            (
                (identity(n), f"data/analysis/{n}.txt", module.SEARCH_FILE_BYTES + 1)
                for n in range(file_count)
            ),
        )
        db.executemany(
            "INSERT INTO file_codes VALUES(?,?)",
            (
                (identity(n), "600519" if n < 10 else "000001")
                for n in range(file_count)
            ),
        )
        db.executemany(
            "INSERT INTO documents(document_id,publication_date) VALUES(?,'2026-01-01')",
            ((f"doc-{n}",) for n in range(document_count)),
        )
        db.executemany(
            "INSERT INTO document_artifacts VALUES(?,?,?)",
            ((f"doc-{n}", identity(n), "text") for n in range(document_count)),
        )
        db.executemany(
            "INSERT INTO document_sources VALUES(?,?,?,?)",
            (
                (
                    f"doc-{n}",
                    identity(n + source),
                    "ds",
                    "600519" if n % 100 == 0 else "000001",
                )
                for n in range(document_count)
                for source in range(10)
            ),
        )
    connect, statements, steps = catalog._connect, [], [0]

    def bounded_connect():
        db = connect()

        def progress():
            steps[0] += 1
            return steps[0] > 30000

        db.set_progress_handler(progress, 1000)
        db.set_trace_callback(
            lambda statement: (
                statements.append(statement)
                if statement.lstrip().upper().startswith(("SELECT", "WITH"))
                else None
            )
        )
        return db

    monkeypatch.setattr(catalog, "_connect", bounded_connect)
    result = catalog.search_text(
        "needle",
        code="600519",
        date_from="2026-01-01",
        date_to="2026-01-01",
        max_files=1,
    )
    assert result["candidate_files"] == len(
        set(range(10)) | set(range(0, document_count, 100))
    )
    assert result["scanned_files"] == 1 and result["next_cursor"]
    steps[0] = 0
    counts = catalog._document_status_counts("ds")
    assert counts["documents_with_text"] == document_count
    assert counts["documents_with_pdf"] == 0
    assert counts["documents_with_pdf_and_text"] == 0
    assert counts["source_pdf_missing"] == document_count
    # Inspect actual emitted SQL as well as bounding execution, so small test
    # hosts cannot accidentally hide a restored correlated source scan.
    with connect() as db:
        plans = [
            tuple(row)
            for statement in statements
            for row in db.execute("EXPLAIN QUERY PLAN " + statement)
        ]
    assert not any("CORRELATED" in str(row) for row in plans)
    assert not any("SCAN a LEFT-JOIN" in str(row) for row in plans)


@pytest.mark.parametrize("newline", ["\r", "\n", "\r\n"])
@pytest.mark.parametrize("control", ["\f", "\v", "\u2028", "\u2029"])
@pytest.mark.parametrize("suffix", ["txt", "md"])
def test_source_citations_keep_file_line_semantics(tmp_path, newline, control, suffix):
    from src.interactive.assistant.data_tools import BusinessDataTools

    path = tmp_path / f"data/dataset_documents/report/document.{suffix}"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        newline.join(
            ["--- PAGE 7 ---", "alpha" + control + "needle", "last", ""]
        ).encode()
    )
    catalog = DatasetCatalog(tmp_path)
    document_id = "cninfo_" + "a" * 64
    catalog.register_document_source(
        document_id,
        "https://static.cninfo.com.cn/report.pdf",
        "report",
        "2026-01-01",
        "600519",
    )
    catalog.record_document(document_id, path.relative_to(tmp_path).as_posix())
    legacy = catalog.search_document_text(document_id, "needle")["results"][0]
    generic = catalog.search_text("needle")["results"][0]
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    for hit in (legacy, generic):
        assert hit["start_line"] == 2 and hit["page"] == 7
        assert hit["text"] == "alpha" + control + "needle"
        old_read = catalog.read_text(hit["file_id"], start_line=2, line_count=1)
        read = tools.read(hit["data_id"], offset=1, limit=1)
        assert old_read["text"].removesuffix("\n") == hit["text"]
        assert read["items"][0]["text"] == hit["text"]


def test_cached_pdf_controls_do_not_add_lines_or_duplicate_page_markers(tmp_path):
    from src.interactive.assistant.data_tools import BusinessDataTools

    catalog = catalog_with(
        tmp_path, {"data/analysis/report.pdf": "%PDF-control-fixture"}
    )
    path = tmp_path / "data/analysis/report.pdf"
    hub = DataHub(catalog.material_authority)
    record = hub.register_original(path)
    hub.parsed._save(
        {
            "sha256": record["sha256"],
            "complete": True,
            "content_type": "application/pdf",
            "content": [{"type": "text", "text": "unused alternative projection"}],
            "page_texts": {
                "2": "prefix\fneedle\r\nsame",
                "3": "beta\vneedle\u2028\u2029rest",
            },
        }
    )
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    hits = catalog.search_text("needle")["results"]
    assert [(hit["start_line"], hit["page"]) for hit in hits] == [(2, 2), (6, 3)]
    for hit in hits:
        for data_id in (hit["data_id"], "material:" + record["sha256"]):
            read = tools.read(data_id, offset=hit["start_line"] - 1, limit=1)
            assert read["items"] == [
                {"line": hit["start_line"], "page": hit["page"], "text": hit["text"]}
            ]


def test_cached_html_search_read_coordinates_remain_rendered_rows(tmp_path):
    from src.interactive.assistant.data_tools import BusinessDataTools

    catalog = DatasetCatalog(tmp_path)
    body = "<html><body><p>alpha\fneedle</p><p>beta\vneedle\u2028end</p></body></html>"
    path = write(tmp_path, "original.html", body)
    hub = DataHub(catalog.material_authority)
    record = hub.register_original(path)
    hub.parsed._save(
        {
            "sha256": record["sha256"],
            "complete": True,
            "content_type": "text/html",
            "content": [{"type": "text", "text": body}],
        }
    )
    catalog.register_material({**record, "content_type": "text/html"})
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    hits = catalog.search_text("needle")["results"]
    assert [(hit["start_line"], hit["page"]) for hit in hits] == [(2, None), (4, None)]
    for hit in hits:
        read = tools.read(hit["data_id"], offset=hit["start_line"] - 1, limit=1)
        assert read["items"][0]["text"] == hit["text"]


def test_search_schema_maxima_are_executable_and_reject_the_same_overflow(
    tmp_path, monkeypatch
):
    from src.interactive.assistant.data_tools import TOOLS, BusinessDataTools

    schema = next(
        tool["function"]["parameters"]
        for tool in TOOLS
        if tool["function"]["name"] == "data_search"
    )
    assert schema["properties"]["query"]["maxLength"] == 120
    assert schema["properties"]["limit"]["maximum"] == 50
    query = "n" * schema["properties"]["query"]["maxLength"]
    catalog = catalog_with(
        tmp_path, {"data/analysis/notes.txt": "\n".join([query] * 50)}
    )
    tools = BusinessDataTools(tmp_path, catalog=catalog)
    monkeypatch.setattr(tools, "_refresh_public", list)
    result = tools.search(query=query, limit=schema["properties"]["limit"]["maximum"])
    assert len(result["results"]) == 50
    with pytest.raises(ValueError, match="query"):
        tools.search(query=query + "n", limit=1)
    with pytest.raises(ValueError, match="limit"):
        tools.search(query=query, limit=51)


@pytest.mark.parametrize(
    "marker",
    [
        "PAGE 7",
        "[PAGE 7]",
        "【PAGE 7】",
        "--- PAGE 7 ---",
        "# PAGE 7",
        "页：7",
        "[页 7]",
        "【页 7】",
        " page: 7 ",
    ],
)
def test_explicit_legacy_page_markers_keep_page_without_guessing_html(marker):
    from src.data.evidence_text import numbered_lines

    assert list(numbered_lines([marker, "needle"]))[1] == {
        "line": 2,
        "page": 7,
        "text": "needle",
    }
    assert (
        list(numbered_lines([marker, "needle"], line_basis="rendered_html_text"))[1][
            "page"
        ]
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        "See PAGE 7 for details",
        "PAGE 7 annual report",
        "【PAGE 7】附注",
        "第7页",
        "网页7的正文",
        "revenue per page 7: 100",
    ],
)
def test_ordinary_body_text_does_not_become_a_page_marker(body):
    from src.data.evidence_text import numbered_lines

    assert [row["page"] for row in numbered_lines([body, "needle"])] == [None, None]
