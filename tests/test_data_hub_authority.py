"""Two projects use one real material authority; only external I/O is faked."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data.content_fetcher import ContentFetcher
from src.data.dataset_catalog import DatasetCatalog
from src.data.dataset_documents import DatasetDocumentWorker
from src.data_hub import DataHub, MaterialError, ParsedCache
from src.data_hub.storage import file_sha
from src.research_pipeline.models import AnnualDocument, ResearchSpec
from src.research_pipeline.sources import SourceService
from src.shared_contracts import AuthorityRef


def test_two_project_consumers_download_and_parse_only_once(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    downloads, parses = [], []

    def read(path):
        parses.append(str(path))
        return [{"type": "text", "text": "Full original 100.00"}], 1

    first, second = (
        DataHub(authority, pdf_reader=read),
        DataHub(authority, pdf_reader=read),
    )

    def download(path):
        downloads.append(str(path))
        time.sleep(0.04)
        path.write_bytes(b"%PDF-1.7 same approved fixture")

    def consume(hub):
        original = hub.ensure_original(
            "https://static.cninfo.com.cn/shared.pdf", download
        )
        return hub.ensure_parsed(original["path"], "application/pdf")

    with ThreadPoolExecutor(8) as pool:
        values = list(pool.map(consume, [first, second] * 4))
    assert len(downloads) == len(parses) == 1
    assert len({v["cache_path"] for v in values}) == 1
    preview = first.text_view(values[0], max_chars=4)
    assert preview["truncated"] and len(preview["text"]) == 4
    assert (
        first.parsed.get(values[0]["sha256"])["content"][0]["text"]
        == "Full original 100.00"
    )


def test_failed_source_is_durable_across_projects_but_other_source_continues(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    a, b = DataHub(authority), DataHub(authority)
    calls = []

    def broken(path):
        calls.append(path)
        path.write_bytes(b"404 raw evidence")
        raise ValueError("source_http_404")

    with pytest.raises(ValueError, match="404"):
        a.ensure_original("https://static.cninfo.com.cn/gap.pdf", broken)
    with pytest.raises(MaterialError, match="not_retried"):
        b.ensure_original("https://static.cninfo.com.cn/gap.pdf", broken)
    assert len(calls) == 1
    available = b.ensure_original(
        "https://static.cninfo.com.cn/other.pdf", lambda _: b"%PDF-other"
    )
    assert Path(available["path"]).read_bytes() == b"%PDF-other"
    attempt = next(
        p
        for p in (a.root / "attempts").glob("*.json")
        if json.loads(p.read_text())["status"] == "failed"
    )
    evidence = json.loads(attempt.read_text())
    assert Path(evidence["raw_path"]).read_bytes() == b"404 raw evidence"


def test_document_http_block_is_shared_with_another_url_and_project(tmp_path):
    from src.task_runtime.request_policy import RequestPolicy, RequestPolicyBlocked

    authority = AuthorityRef(tmp_path / "authority")
    hub = DataHub(authority)

    def denied(path):
        raise ValueError("source_http_403")

    with pytest.raises(ValueError, match="403"):
        hub.ensure_original("https://static.cninfo.com.cn/denied.pdf", denied)
    with pytest.raises(RequestPolicyBlocked):
        RequestPolicy(authority).wait(
            "https://www.cninfo.com.cn/different-document.pdf", min_interval=0
        )


def test_official_revision_does_not_overwrite_prior_original(tmp_path):
    hub = DataHub(AuthorityRef(tmp_path / "authority"))
    url = "https://static.cninfo.com.cn/annual.pdf"
    a = hub.ensure_original(url, lambda _: b"%PDF-v1", version="official:v1")
    b = hub.ensure_original(url, lambda _: b"%PDF-v2", version="official:v2")
    assert a["sha256"] != b["sha256"]
    assert hub.lookup(url, version="official:v1")["path"] == a["path"]
    assert hub.lookup(url)["path"] == b["path"]


def test_unversioned_consumer_cannot_retry_a_failed_official_request(tmp_path):
    first = DataHub(AuthorityRef(tmp_path / "authority"))
    second = DataHub(first.authority)
    calls = []

    def broken(path):
        calls.append(path)
        raise ValueError("HTTP 404")

    url = "https://static.cninfo.com.cn/failed-official.pdf"
    with pytest.raises(ValueError, match="404"):
        first.ensure_original(url, broken, version="cninfo:1")
    with pytest.raises(MaterialError, match="not_retried"):
        second.ensure_original(url, broken)
    assert len(calls) == 1


def test_first_parse_and_cache_replay_preserve_model_visible_key_order(tmp_path):
    original = tmp_path / "annual.pdf"
    original.write_bytes(b"%PDF-key-order")
    cache = ParsedCache(
        tmp_path / "parsed",
        pdf_reader=lambda _: ([{"type": "text", "text": "Original 1,000.00"}], 1),
    )
    first = cache.read(original)
    second = cache.read(original)
    assert json.dumps(first["content"]) == json.dumps(second["content"])


def test_parse_failure_cannot_be_replayed_by_another_project(tmp_path):
    authority = AuthorityRef(tmp_path / "authority")
    original = tmp_path / "invalid.pdf"
    original.write_bytes(b"%PDF-invalid-real-source")
    calls = []

    def reader(path):
        calls.append(path)
        raise ValueError("parser rejected source")

    with pytest.raises(ValueError, match="rejected"):
        DataHub(authority, pdf_reader=reader).ensure_parsed(original)
    with pytest.raises(ValueError, match="parse_attempt_not_retried"):
        DataHub(authority, pdf_reader=reader).ensure_parsed(original)
    assert len(calls) == 1
    with sqlite3.connect(authority.execution_db) as db:
        assert db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 1


def test_old_verified_pdf_is_imported_readonly_without_redownload_or_cleanup(tmp_path):
    url = "https://static.cninfo.com.cn/legacy-original.pdf"
    code = "600938"
    key = hashlib.md5(f"{code}_{url}".encode()).hexdigest()
    root = tmp_path / "cache/pdf_files"
    root.mkdir(parents=True)
    original = root / f"{key}.pdf"
    original.write_bytes(b"%PDF-legacy available original")
    metadata = root / f"{key}_metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "url": url,
                "stock_code": code,
                "file_size": original.stat().st_size,
                "metadata": {
                    "content_hash": hashlib.md5(original.read_bytes()).hexdigest()
                },
            }
        )
    )
    before = (original.read_bytes(), metadata.read_bytes())
    fetcher = ContentFetcher({"project_root": str(tmp_path)})
    fetcher._import_legacy_pdf(code, url)
    cached = fetcher.hub.ensure_original(
        url, lambda _: pytest.fail("legacy original redownloaded")
    )
    assert cached["path"] == str(original)
    assert (original.read_bytes(), metadata.read_bytes()) == before


def test_scope_references_cannot_read_another_namespace(tmp_path):
    public = DataHub(AuthorityRef(tmp_path / "authority"))
    private = DataHub(AuthorityRef(tmp_path / "authority", scope="private-a"))
    record = public.import_bytes(b"%PDF-public")
    marker = tmp_path / "original.material-ref.json"
    public.write_reference(marker, record)
    with pytest.raises(MaterialError, match="scope_or_authority"):
        private.resolve_reference(marker)
    fake = json.loads(marker.read_text())
    fake["path"] = str(tmp_path / "secret.txt")
    (tmp_path / "secret.txt").write_text("private secret")
    marker.write_text(json.dumps(fake))
    assert public.resolve_reference(marker)["path"] == record["path"]


def test_old_complete_parse_is_referenced_not_copied_or_reparsed(tmp_path):
    old = tmp_path / "old/parsed"
    original = tmp_path / "annual.pdf"
    original.write_bytes(b"%PDF-frozen")
    parsed = ParsedCache(
        old, pdf_reader=lambda _: ([{"type": "text", "text": "immutable body"}], 1)
    ).read(original)
    before = file_sha(Path(parsed["cache_path"]))
    hub = DataHub(
        AuthorityRef(tmp_path / "authority"),
        pdf_reader=lambda _: pytest.fail("must not reparse"),
    )
    central = ParsedCache.bind_root(old, hub.parsed.root)
    assert hub.ensure_parsed(original)["content"][0]["text"] == "immutable body"
    assert not (central / parsed["sha256"] / "parsed.json").exists()
    assert file_sha(Path(parsed["cache_path"])) == before
    assert (
        ParsedCache(old).get(parsed["sha256"])["content"][0]["text"] == "immutable body"
    )


def test_source_archive_and_announcement_share_real_catalog_material(tmp_path):
    project_a, project_b = tmp_path / "project-a", tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    authority = AuthorityRef(tmp_path / "authority")
    source_spec = ResearchSpec(
        ("a_share:600938",),
        project_a / "run",
        repo=project_a,
        store_root=project_a / "research",
        authority_root=authority.root,
    )
    calls = []

    def download(document, target):
        calls.append(document.url)
        return b"%PDF-1.7 provider fixture"

    source = SourceService(
        source_spec, SimpleNamespace(root=source_spec.store_root), downloader=download
    )
    url = "https://static.cninfo.com.cn/financial-chain.pdf"
    document = AnnualDocument(
        "a_share:600938", "a_share:600938", 2025, "cninfo:1", url=url
    )
    original = source.acquire(document)
    parsed = DataHub(
        authority,
        pdf_reader=lambda _: ([{"type": "text", "text": "Real source body"}], 1),
    ).ensure_parsed(original.local_path)
    catalog = DatasetCatalog(project_b, material_authority=authority)
    catalog.register_document_source(
        "cninfo_" + "a" * 64, url, "Annual 2025", "2026-03-01", "600938"
    )
    worker = DatasetDocumentWorker(catalog)
    # This is the real archive and SQLite/reference path; its extractor reads
    # the verified complete cache, so no external parser/network is needed.
    worker._initialize()
    result = worker._archive(catalog.document_jobs()["items"][0], time.monotonic() + 30)
    assert result["original_pdf_available"]
    assert result["pdf_path"].endswith(".material-ref.json")
    assert catalog.resolve_artifact(result["pdf_path"]) == Path(original.local_path)
    assert not list((project_b / "data/dataset_documents").rglob("*.pdf"))
    fetcher = ContentFetcher(
        {
            "project_root": str(project_b),
            "storage": {"authority_root": str(authority.root)},
        }
    )
    fetcher.extract_max_length = 5
    announcement = fetcher.fetch_content(url, "600938", "2026-03-01")
    assert announcement["success"] and announcement["metadata"]["preview_truncated"]
    assert announcement["metadata"]["parse_ref"] == parsed["cache_path"]
    assert len(calls) == 1
    assert (
        DataHub(authority).parsed.get(original.sha256)["content"][0]["text"]
        == "Real source body"
    )


def test_distinct_project_producers_share_download_capacity_and_hits_submit_nothing(
    tmp_path,
):
    authority = AuthorityRef(tmp_path / "authority")
    hubs = [DataHub(authority), DataHub(authority)]
    lock = threading.Lock()
    at_least_two = threading.Event()
    active, peak = 0, 0

    def download(path):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                at_least_two.set()
        try:
            assert at_least_two.wait(5), "independent downloads were serialized"
            time.sleep(0.05)
            path.write_bytes(b"%PDF-" + path.stem.encode())
        finally:
            with lock:
                active -= 1

    urls = [f"https://static.cninfo.com.cn/parallel-{i}.pdf" for i in range(6)]
    with ThreadPoolExecutor(6) as pool:
        references = list(
            pool.map(lambda i: hubs[i % 2].ensure_original(urls[i], download), range(6))
        )
    assert 2 <= peak <= 3
    with sqlite3.connect(authority.execution_db) as db:
        before = db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0]
        lanes = {
            row[0] for row in db.execute("SELECT DISTINCT lane FROM execution_jobs")
        }
    assert "material_download" in lanes
    assert before == 6
    for i in range(6):
        assert (
            hubs[1].ensure_original(
                urls[i], lambda _: pytest.fail("cache hit downloaded")
            )["sha256"]
            == references[i]["sha256"]
        )
    with sqlite3.connect(authority.execution_db) as db:
        assert db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == before


def test_two_processes_share_the_native_parse_lane(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-native A")
    b.write_bytes(b"%PDF-native B")
    program = r"""
import json,sys,time
from pathlib import Path
from src.data_hub import DataHub
from src.shared_contracts import AuthorityRef
root,original,events=map(Path,sys.argv[1:])
def reader(path):
    with events.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(['start',time.time()])+'\n')
    time.sleep(.25)
    with events.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(['end',time.time()])+'\n')
    return [{'type':'text','text':str(path)}],1
hub=DataHub(AuthorityRef(root),pdf_reader=reader)
ready=events.with_name(original.stem+'.ready');ready.write_text('ready')
deadline=time.monotonic()+10
while len(list(events.parent.glob('*.ready')))<2:
    if time.monotonic()>deadline:raise TimeoutError('peer did not start')
    time.sleep(.01)
hub.ensure_parsed(original)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                program,
                str(tmp_path / "authority"),
                str(pdf),
                str(tmp_path / "events.jsonl"),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for pdf in (a, b)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert [event[0] for event in sorted(events, key=lambda item: item[1])] == [
        "start",
        "end",
        "start",
        "end",
    ]
    with sqlite3.connect(tmp_path / "authority/execution.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM execution_runs WHERE status='succeeded'"
            ).fetchone()[0]
            == 2
        )


def test_portable_material_packages_import_under_an_unrelated_namespace(tmp_path):
    root = Path(__file__).resolve().parents[1] / "src"
    foreign = tmp_path / "foreign_project"
    foreign.mkdir()
    (foreign / "__init__.py").write_text("")
    for package in ("shared_contracts", "task_runtime", "data_hub"):
        shutil.copytree(
            root / package,
            foreign / package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    program = r"""
import sys
from pathlib import Path
from foreign_project.data_hub import DataHub
from foreign_project.shared_contracts import AuthorityRef
hub=DataHub(AuthorityRef(Path(sys.argv[1])))
record=hub.ensure_original('https://static.cninfo.com.cn/foreign.pdf',lambda _:b'%PDF-independent')
assert Path(record['path']).read_bytes()==b'%PDF-independent'
assert not any(name.startswith(('src.','scripts.')) for name in sys.modules)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            program,
            str(tmp_path / "authority"),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
