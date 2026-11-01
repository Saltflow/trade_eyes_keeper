"""Offline neutral parse cache, official adapters and legacy evidence imports."""

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from src.research_pipeline.common import (
    annual_versions,
    artifact_key,
    file_sha,
    fingerprint,
)
from src.research_pipeline.legacy import LegacyImporter
from src.research_pipeline.models import AnnualDocument, Instrument, ResearchSpec
from src.research_pipeline.parsing import ParsedCache, ParsedCacheError
from src.research_pipeline.sources import (
    SourceError,
    SourceService,
    document_from_record,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network is forbidden in pipeline source acceptance")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


class Store:
    def __init__(self, root):
        self.root = root
        self.artifacts = {}
        self.attempts = {}

    def get_artifact(self, key):
        return self.artifacts.get(key)

    def put_artifact(self, key, stage, path, status="complete", metadata=None):
        value = {
            "key": key,
            "stage": stage,
            "path": str(path),
            "status": status,
            "metadata": metadata or {},
        }
        self.artifacts[key] = value
        return value

    def get_attempt(self, key):
        return self.attempts.get(key)

    def import_attempt(self, key, record):
        self.attempts.setdefault(key, record)


def spec(tmp_path, companies=("a_share:600348",), **kwargs):
    return ResearchSpec(
        companies,
        tmp_path / "run",
        repo=tmp_path,
        store_root=tmp_path / "shared",
        start_year=2019,
        end_year=2025,
        **kwargs,
    )


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_pdf_content_cache_is_neutral_and_shared_by_content_not_path(tmp_path):
    first, second = tmp_path / "a.pdf", tmp_path / "other.pdf"
    first.write_bytes(b"%PDF-1.7 fake offline parser fixture")
    second.write_bytes(first.read_bytes())
    calls = []
    content = [
        {"type": "text", "text": "原数字10.0\r\n\n\n薄页"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
    ]

    def reader(path):
        calls.append(path)
        return content, 2

    cache = ParsedCache(tmp_path / "parsed", pdf_reader=reader)
    a, b = cache.read(first), cache.read(second)
    assert a["content"] == b["content"] == content
    assert a["pages"] == 2 and b["cache_hit"]
    assert len(calls) == 1


def test_parallel_consumers_parse_same_pdf_only_once(tmp_path):
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-1.7 fixture")
    calls = []

    def reader(path):
        calls.append(path)
        return [{"type": "text", "text": "财报"}], 1

    cache = ParsedCache(tmp_path / "cache", pdf_reader=reader)
    with ThreadPoolExecutor(10) as pool:
        values = list(pool.map(lambda _: cache.read(pdf), range(10)))
    assert len(calls) == 1
    assert sum(not v["cache_hit"] for v in values) == 1


def test_separate_processes_share_the_same_parse_claim(tmp_path):
    pdf = tmp_path / "shared.pdf"
    pdf.write_bytes(b"%PDF-offline fixture")
    counter = tmp_path / "calls.txt"
    program = """
import sys,time
from pathlib import Path
from src.research_pipeline.parsing import ParsedCache
def reader(path):
    with Path(sys.argv[3]).open('ab') as f: f.write(b'called\\n')
    time.sleep(.15)
    return [{'type':'text','text':'original'}],1
assert ParsedCache(Path(sys.argv[2]),pdf_reader=reader).read(Path(sys.argv[1]))['pages']==1
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    commands = [
        subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-c",
                program,
                str(pdf),
                str(tmp_path / "cache"),
                str(counter),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(3)
    ]
    for process in commands:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    assert counter.read_text() == "called\n"


def test_corrupt_parse_is_not_automatically_reparsed(tmp_path):
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-1.7 fixture")
    cache = ParsedCache(
        tmp_path / "cache", pdf_reader=lambda _: ([{"type": "text", "text": "1"}], 1)
    )
    value = cache.read(pdf)
    path = Path(value["cache_path"])
    raw = json.loads(path.read_text("utf-8"))
    raw["parsed"]["content"][0]["text"] = "changed"
    save(path, raw)
    cache.pdf_reader = lambda _: pytest.fail(
        "corrupt cache must not cause hidden reparse"
    )
    with pytest.raises(ParsedCacheError, match="corrupt_parsed_cache"):
        cache.read(pdf)


def test_legacy_frozen_content_import_removes_only_task_envelope(tmp_path):
    content = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "frozen_job_ownership": {"capital": {}},
                    "expected_company": "600348",
                    "expected_year": 2025,
                }
            ),
        },
        {"type": "text", "text": "原文\r\n资本null"},
        {"type": "text", "text": "[block:capital]\n任务"},
    ]
    cache = ParsedCache(
        tmp_path / "cache", pdf_reader=lambda _: pytest.fail("no PDF read")
    )
    value = cache.import_prepared("a" * 64, content, 2, "historical request")
    assert value["content"] == content[1:2]
    assert value["pages"] == 2 and value["complete"]


def test_html_preserves_tables_and_anchors_without_synthetic_pdf_pages(tmp_path):
    path = tmp_path / "10k.html"
    path.write_text(
        '<html><body><script>bad()</script><h1 id="item7">Annual 2025</h1>'
        '<table id="t1"><tr><td rowspan="2">Revenue</td><td>1,000</td></tr></table>'
        '<ix:hidden><ix:nonfraction name="us-gaap:Assets">200</ix:nonfraction></ix:hidden></body></html>',
        encoding="utf-8",
    )
    parsed = ParsedCache(tmp_path / "cache").read(path, "text/html")
    text = parsed["content"][0]["text"]
    assert "1,000" in text and 'rowspan="2"' in text and "200" in text
    assert "bad()" not in text and parsed["pages"] == 0
    assert {a["id"] for a in parsed["anchors"]} == {"item7", "t1"}


def test_local_manifest_read_never_constructs_network_or_parses(tmp_path):
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    manifest = tmp_path / "sources.json"
    save(
        manifest,
        {
            "records": [
                {
                    "code": "600348",
                    "year": 2025,
                    "pdf": str(pdf),
                    "profile": "extractives",
                }
            ]
        },
    )
    s = spec(tmp_path, source_manifest=manifest)
    service = SourceService(
        s, Store(s.store_root), http=SimpleNamespace(get=lambda *_: pytest.fail("HTTP"))
    )
    result = service.local_documents()
    assert len(result) == 1 and result[0].sha256 == file_sha(pdf)
    assert result[0].profiles == ["extractives"]


class Response:
    status_code = 200
    headers: ClassVar[dict] = {}

    def __init__(self, value=None, text=""):
        self.value, self.text = value, text

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


def test_cninfo_scans_late_old_year_amendment_and_distinguishes_full_report(
    tmp_path, monkeypatch
):
    from src.instruments.point_in_time import CninfoAnnualReportProvider

    monkeypatch.setattr(CninfoAnnualReportProvider, "_org_id", lambda *_: "org")
    rows = [
        {
            "announcementTitle": title,
            "adjunctUrl": f"p/{i}.pdf",
            "announcementId": str(i),
            "announcementTime": int(
                datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp() * 1000
            ),
        }
        for i, title in enumerate(
            [
                "2022年年度报告",
                "2022年年度报告更正公告",
                "2022年年度报告（修订版）",
                "2022年年度报告摘要",
            ]
        )
    ]
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["data"])
        return Response({"totalpages": 1, "announcements": rows})

    s = spec(tmp_path)
    service = SourceService(
        s, Store(s.store_root), http=SimpleNamespace(post=post), minimum_interval=0
    )
    docs = service.discover(Instrument.parse("a_share:600348"), date(2026, 11, 1))
    assert len(docs) == 3
    assert [d.kind for d in docs] == ["annual", "amendment", "annual"]
    assert calls[0]["seDate"].endswith("2026-11-01")


def test_hkex_does_not_use_results_as_annual_or_invent_fiscal_dates(
    tmp_path, monkeypatch
):
    from src.data.hk_announcements import HkexAnnouncementProvider

    monkeypatch.setattr(
        HkexAnnouncementProvider,
        "fetch",
        lambda *_: [
            {
                "title": title,
                "date": "2026-04-01",
                "url": f"https://www1.hkexnews.hk/{i}.pdf",
            }
            for i, title in enumerate(
                [
                    "2025 Annual Report",
                    "2025 Annual Results",
                    "2025 Annual Report Corrigendum",
                    "Annual Report",
                ]
            )
        ],
    )
    s = spec(tmp_path, ("hk:00883",))
    service = SourceService(s, Store(s.store_root))
    docs = service.discover(Instrument.parse("hk:00883"), date(2026, 11, 1))
    assert [d.kind for d in docs] == ["annual", "amendment"]
    assert all(d.period_end is None for d in docs)
    assert service.gaps[0]["error"] == "hk_annual_fiscal_year_not_in_title"


def test_sec_full_and_amendment_html_keep_explicit_fiscal_period(tmp_path, monkeypatch):
    from src.data.us_announcements import SecAnnouncementProvider

    monkeypatch.setattr(
        SecAnnouncementProvider,
        "fetch",
        lambda *_: [
            {
                "type": form,
                "date": "2026-01-15",
                "reportDate": "2025-09-27",
                "url": f"https://www.sec.gov/Archives/edgar/data/1/{i}/annual.htm",
            }
            for i, form in enumerate(["10-K", "10-K/A", "20-F", "40-F", "10-Q"])
        ],
    )
    s = spec(tmp_path, ("us:AAPL",))
    docs = SourceService(s, Store(s.store_root)).discover(
        Instrument.parse("us:AAPL"), date(2026, 11, 1)
    )
    assert len(docs) == 4 and docs[1].kind == "amendment"
    assert all(
        d.period_end == "2025-09-27" and d.content_type == "text/html" for d in docs
    )


@pytest.mark.parametrize("bad_annual", [False, True])
@pytest.mark.parametrize("other_form", ["8-K", "DEF 14A"])
def test_sec_annual_directory_isolates_other_forms_and_invalid_annual_rows(
    tmp_path, monkeypatch, bad_annual, other_form
):
    from src.data.us_announcements import SecAnnouncementProvider

    columns = {
        "accessionNumber": ["0000320193-25-000079", "0001308179-25-000008"],
        "filingDate": ["2025-10-31", "2025-01-10"],
        "form": ["10-K", other_form],
        "primaryDocument": ["aapl-20250927.htm", "current.htm"],
        # DEF 14A dates/accession are from the preserved real AAPL submissions
        # probe: a future shareholder-meeting period is valid for that form.
        "reportDate": ["2025-09-27", "2025-02-25"],
    }
    if bad_annual:
        for field, value in {
            "accessionNumber": "0000320193-24-000100",
            "filingDate": "2024-10-01",
            "form": "10-K/A",
            "primaryDocument": "amend.htm",
            "reportDate": "2024-10-02",
        }.items():
            columns[field].append(value)
    payload = {
        "cik": "320193",
        "tickers": ["AAPL"],
        "filings": {"recent": columns, "files": []},
    }
    monkeypatch.setattr(
        SecAnnouncementProvider, "_identity", lambda *_: ("0000320193", None)
    )
    monkeypatch.setattr(
        SecAnnouncementProvider,
        "_get",
        lambda *_: SimpleNamespace(json=lambda: payload),
    )
    s = spec(tmp_path, ("us:AAPL",))
    service = SourceService(s, Store(s.store_root))
    docs = service.discover(Instrument.parse("us:AAPL"), date(2026, 11, 1))
    assert len(docs) == 1
    assert docs[0].period_end == "2025-09-27"
    assert docs[0].provenance["report_series"] == "10-K:2025-09-27"
    assert len(service.gaps) == int(bad_annual)
    if bad_annual:
        assert service.gaps[0]["error"] == "sec_annual_row_invalid"
        assert "after filing" in service.gaps[0]["detail"]


def test_original_download_reuses_same_url_and_keeps_old_bytes(tmp_path):
    s = spec(tmp_path)
    calls = []

    def download(doc, path):
        calls.append(doc)
        return b"%PDF-1.7 original fixture"

    service = SourceService(s, Store(s.store_root), downloader=download)
    doc = AnnualDocument(
        "a_share:600348",
        "a_share:600348",
        2025,
        "a",
        url="https://static.cninfo.com.cn/one.pdf",
    )
    first = service.acquire(doc)
    second = service.acquire(replace(doc, document_id="b"))
    assert len(calls) == 1 and first.sha256 == second.sha256
    assert Path(first.local_path).read_bytes() == b"%PDF-1.7 original fixture"


def test_failed_original_download_attempt_is_durable_and_not_retried(tmp_path):
    s = spec(tmp_path)
    calls = []

    def download(doc, path):
        calls.append(doc)
        raise SourceError("source_http_404")

    service = SourceService(s, Store(s.store_root), downloader=download)
    doc = AnnualDocument(
        "a_share:600348",
        "a_share:600348",
        2025,
        "a",
        url="https://static.cninfo.com.cn/one.pdf",
    )
    with pytest.raises(SourceError, match="404"):
        service.acquire(doc)
    with pytest.raises(SourceError, match="not_retried"):
        SourceService(s, Store(s.store_root), downloader=download).acquire(doc)
    assert len(calls) == 1
    attempt = json.loads(
        next((s.store_root / "source_attempts").glob("*.json")).read_text("utf-8")
    )
    assert attempt["requests"] == 1 and attempt["status"] == "failed"


def test_official_id_can_reuse_verified_legacy_url_without_redownload(tmp_path):
    original = tmp_path / "legacy.pdf"
    original.write_bytes(b"%PDF-1.7 original")
    manifest = tmp_path / "sources.json"
    url = "https://static.cninfo.com.cn/one.pdf"
    save(
        manifest,
        {
            "records": [
                {
                    "code": "600348",
                    "year": 2025,
                    "pdf": str(original),
                    "sha256": file_sha(original),
                    "url": url,
                }
            ]
        },
    )
    s = spec(tmp_path, source_manifest=manifest)
    official = AnnualDocument(
        "a_share:600348", "a_share:600348", 2025, "cninfo:official", url=url
    )
    service = SourceService(
        s,
        Store(s.store_root),
        discovery=lambda *_: [official],
        downloader=lambda *_: pytest.fail("known URL must not download"),
    )
    docs = service.discover(Instrument.parse("a_share:600348"), date(2026, 11, 1))
    selected = next(d for d in docs if d.document_id == "cninfo:official")
    assert selected.sha256 == file_sha(original)
    assert service.acquire(selected).local_path == str(original.resolve())


def test_declared_official_revision_keeps_both_originals_even_with_same_url(tmp_path):
    s = spec(tmp_path)
    calls = []

    def download(doc, path):
        calls.append(doc.document_id)
        return b"%PDF-1.7 " + doc.document_id.encode()

    service = SourceService(s, Store(s.store_root), downloader=download)
    doc = AnnualDocument(
        "a_share:600348",
        "a_share:600348",
        2025,
        "cninfo:v1",
        url="https://static.cninfo.com.cn/one.pdf",
    )
    first = service.acquire(doc)
    second = service.acquire(replace(doc, document_id="cninfo:v2"))
    assert calls == ["cninfo:v1", "cninfo:v2"]
    assert first.sha256 != second.sha256 and first.local_path != second.local_path
    assert Path(first.local_path).read_bytes().endswith(b"cninfo:v1")


def test_failed_original_response_bytes_are_kept_as_failure_attachment(tmp_path):
    s = spec(tmp_path)
    raw = b"<html><title>gone</title>not the annual PDF</html>"
    service = SourceService(s, Store(s.store_root), downloader=lambda *_: raw)
    doc = AnnualDocument(
        "a_share:600348",
        "a_share:600348",
        2025,
        "cninfo:a",
        url="https://static.cninfo.com.cn/one.pdf",
    )
    with pytest.raises(SourceError, match="not_pdf"):
        service.acquire(doc)
    attempt = json.loads(
        next((s.store_root / "source_attempts").glob("*.json")).read_text("utf-8")
    )
    assert Path(attempt["raw_path"]).read_bytes() == raw
    assert not list((s.store_root / "originals").glob("*/original.pdf"))


def test_archive_url_receipt_is_optional_but_hash_bound_when_available(tmp_path):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"%PDF-1.7 fixture")
    s = spec(tmp_path)
    row = {"code": "600348", "year": 2025, "pdf": str(pdf), "sha256": file_sha(pdf)}
    save(
        tmp_path / "extraction.json",
        {
            "pdf_sha256": row["sha256"],
            "download": {"url": "https://static.cninfo.com.cn/known.pdf"},
        },
    )
    doc = document_from_record(row, tmp_path, s)
    assert doc.url == "https://static.cninfo.com.cn/known.pdf"
    save(tmp_path / "extraction.json", [])
    other = document_from_record(row, tmp_path, s)
    assert other.sha256 == row["sha256"] and other.provenance["url_evidence_gap"]


def test_quote_uses_source_clock_and_frozen_currency_not_fetch_time(tmp_path):
    s = spec(tmp_path)
    service = SourceService(
        s,
        Store(s.store_root),
        quoter=lambda *_: {"price": "8.33", "date": "2026-10-30", "currency": "CNY"},
    )
    quote = service.quote(Instrument.parse("a_share:600348"), date(2026, 11, 1))
    assert quote["date"] == "2026-10-30" and quote["price"] == "8.33"
    service.quoter = lambda *_: {"price": 8, "date": "2027-01-01", "currency": "CNY"}
    with pytest.raises(SourceError, match="future"):
        service.quote(Instrument.parse("a_share:600348"), date(2026, 11, 1))


def legacy_fixture(tmp_path):
    root = tmp_path / "old"
    pdf = root / "original.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.7 legacy")
    save(
        root / "sources.json",
        {
            "records": [
                {
                    "code": "600348",
                    "year": 2025,
                    "pdf": str(pdf),
                    "sha256": file_sha(pdf),
                }
            ]
        },
    )
    annual = root / "stage1/600348_2025"
    save(
        annual / "data.json",
        {"tables": [], "unstructured_blocks": {"failed": {"run_status": "failed"}}},
    )
    for name, status in [("statements", "complete"), ("capital", "failed")]:
        folder = annual / "blocks" / name
        save(folder / "attempt.json", {"requests": 1})
        save(folder / "receipt.json", {"status": status, "cost": {"usd": None}})
        if status == "complete":
            save(folder / "parsed.json", {"original": "10.00", "company": None})
    save(
        root / "contexts/600348/summary.json",
        {
            "company_code": "600348",
            "inputs": ["600348_2025"],
            "retention": {"passed": True, "errors": []},
        },
    )
    (root / "contexts/600348/模型正文.md").write_text(
        "原文10.00，身份未定", encoding="utf-8"
    )
    summary = root / "summaries/600348/完整摘要.md"
    summary.parent.mkdir(parents=True)
    summary.write_text("完整摘要原文", encoding="utf-8")
    save(
        root / "summary_results.json",
        {
            "companies": [
                {
                    "code": "600348",
                    "years": [2025],
                    "status": "complete",
                    "path": str(summary),
                }
            ]
        },
    )
    return root


def test_legacy_import_keeps_null_identity_success_and_failed_attempt_without_mutation(
    tmp_path,
):
    root = legacy_fixture(tmp_path)
    before = {str(p): file_sha(p) for p in root.rglob("*") if p.is_file()}
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [root])
    result = importer.run()
    docs = importer.documents()
    versions = annual_versions(docs)
    assert result["documents"] == 1 and result["attempts"] == 2
    assert store.get_artifact(artifact_key("summary", "a_share:600348", versions))
    assert store.get_artifact(artifact_key("context", "a_share:600348", versions))
    assert (
        store.get_attempt(artifact_key("extract:capital", "a_share:600348", versions))[
            "receipt"
        ]["status"]
        == "failed"
    )
    assert {str(p): file_sha(p) for p in root.rglob("*") if p.is_file()} == before


def test_parent_cache_root_discovers_descendant_batch_context_summary_and_failure(
    tmp_path,
):
    root = legacy_fixture(tmp_path / "archive")
    # A descendant plan is a formal source declaration even without sources.json.
    (root / "sources.json").rename(root / "plan.json")
    before = {str(p): file_sha(p) for p in root.rglob("*") if p.is_file()}
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [tmp_path / "archive"])
    report = importer.run()
    versions = annual_versions(importer.documents())
    assert report["documents"] == 1 and report["attempts"] == 2
    assert store.get_artifact(artifact_key("context", "a_share:600348", versions))
    assert store.get_artifact(artifact_key("summary", "a_share:600348", versions))
    assert (
        store.get_attempt(artifact_key("extract:capital", "a_share:600348", versions))[
            "receipt"
        ]["status"]
        == "failed"
    )
    assert {str(p): file_sha(p) for p in root.rglob("*") if p.is_file()} == before


def test_shared_registry_and_frozen_runtime_copies_are_not_legacy_archives(tmp_path):
    parent = tmp_path / "archives"
    valid = legacy_fixture(parent / "valid")
    frozen = legacy_fixture(parent / "frozen_copy")
    save(frozen.parent / "runtime_manifest.json", {"files": {}})
    unified = legacy_fixture(parent / "new_run")
    save(unified.parent / "spec.json", {"companies": ["a_share:600348"]})
    save(unified.parent / "run.json", {"id": "new", "store_root": "shared"})
    store = Store(parent / "shared")
    legacy_fixture(store.root)
    report = LegacyImporter(store, [parent]).run()
    assert report["documents"] == 1 and report["attempts"] == 2
    assert all(str(valid) in row["path"] for row in store.attempts.values())


def research_fixture(root, explicit_identity=False):
    summary = root / "summaries/600348/完整摘要.md"
    document = json.loads((root / "sources.json").read_text("utf-8"))["records"][0]
    versions = {"a_share:600348": {"2025": [document["sha256"]]}}
    plan = {
        "jobs": [{"id": phase} for phase in ("draft", "review", "write")],
        "source_hashes": {str(summary): file_sha(summary)},
    }
    identity = {
        "versions": versions,
        "instruments": ["a_share:600348"],
        "market": [
            {"instrument": "a_share:600348", "price": "8.33", "date": "2026-10-23"}
        ],
        "focus": "自由现金流",
        "assumptions": None,
    }
    if explicit_identity:
        plan["research_identity"] = identity
    save(root / "research/plan.json", plan)
    for phase in ("draft", "review", "write"):
        capture = root / "research/jobs" / phase
        save(
            capture / "request.json", {"messages": [{"role": "user", "content": phase}]}
        )
        save(
            capture / "attempt.json",
            {"request_sha256": file_sha(capture / "request.json")},
        )
        save(
            capture / "receipt.json",
            {
                "status": "failed" if phase == "review" else "complete",
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                "cost": {"usd": ".01"},
            },
        )
    (root / "research/最终报告.md").write_text("已保留研究原文", encoding="utf-8")
    return identity


@pytest.mark.parametrize("explicit_identity", [False, True])
def test_research_all_real_attempts_imported_and_scope_not_invented(
    tmp_path, explicit_identity
):
    root = legacy_fixture(tmp_path / "archive")
    identity = research_fixture(root, explicit_identity)
    before = {str(p): file_sha(p) for p in root.rglob("*") if p.is_file()}
    store = Store(tmp_path / "shared")
    report = LegacyImporter(store, [tmp_path / "archive"]).run()
    assert report["attempts"] == 5
    research = [
        row
        for row in store.attempts.values()
        if row.get("stage", "").startswith("research_")
    ]
    assert len(research) == 3
    assert (
        next(row for row in research if row["job"] == "review")["receipt"]["status"]
        == "failed"
    )
    final_key = artifact_key("research", ["a_share:600348"], identity)
    if explicit_identity:
        assert store.get_artifact(final_key)
        assert store.get_attempt(fingerprint({"research": final_key, "round": "write"}))
        assert not report["unassigned"]
    else:
        assert store.get_artifact(final_key) is None
        assert any(
            row["stage"] == "legacy_reference" and row["metadata"]["scope_unresolved"]
            for row in store.artifacts.values()
        )
        assert report["unassigned"][0]["attempts_imported"] == 3
    assert {str(p): file_sha(p) for p in root.rglob("*") if p.is_file()} == before


def test_multiple_real_producers_of_same_logical_block_keep_separate_receipts(tmp_path):
    first = legacy_fixture(tmp_path / "first")
    second = legacy_fixture(tmp_path / "second")
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [first, second])
    report = importer.run()
    assert report["documents"] == 1 and report["attempts"] == 4
    assert len(store.attempts) == 4
    assert sum(bool(row.get("logical_key")) for row in store.attempts.values()) == 2
    assert LegacyImporter(store, [first, second]).run()["attempts"] == 0


def test_legacy_ambiguous_source_is_reported_not_guessed_from_filename(tmp_path):
    root = tmp_path / "old"
    save(root / "sources.json", {"records": [{"pdf": "600348_2025.pdf", "year": 2025}]})
    result = LegacyImporter(Store(tmp_path / "shared"), [root]).run()
    assert result["documents"] == 0 and result["unassigned"]


def test_legacy_parsed_and_summary_caches_work_when_original_pdf_is_unavailable(
    tmp_path,
):
    root = legacy_fixture(tmp_path)
    manifest = root / "sources.json"
    record = json.loads(manifest.read_text("utf-8"))["records"][0]
    original_hash = record["sha256"]
    record.update(pages=1, url="https://static.cninfo.com.cn/original.pdf")
    save(manifest, {"records": [record]})
    save(
        root / "prepared/600348_2025/source_content.json",
        [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "frozen_job_ownership": {},
                        "expected_company": "600348",
                        "expected_year": 2025,
                    }
                ),
            },
            {"type": "text", "text": "保留原年报正文"},
        ],
    )
    (root / "original.pdf").unlink()
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [root])
    result = importer.run()
    assert len(result["missing_originals"]) == 1
    assert (
        ParsedCache(store.root / "parsed").get(original_hash)["content"][0]["text"]
        == "保留原年报正文"
    )
    s = spec(tmp_path)
    service = SourceService(
        s,
        store,
        discovery=lambda *_: [
            AnnualDocument(
                "a_share:600348",
                "a_share:600348",
                2025,
                "cninfo:official",
                url=record["url"],
            )
        ],
    )
    documents = service.discover(Instrument.parse("a_share:600348"), date(2026, 11, 1))
    assert (
        next(d for d in documents if d.document_id == "cninfo:official").sha256
        == original_hash
    )
    key = artifact_key(
        "summary", "a_share:600348", annual_versions(importer.documents())
    )
    assert store.get_artifact(key) is not None


def test_failed_summary_attempt_without_body_is_imported_without_reissuing(tmp_path):
    root = legacy_fixture(tmp_path)
    save(
        root / "plan.json",
        {
            "companies": [
                {
                    "code": "600348",
                    "years": [2025],
                    "direct": "600348_summary",
                    "parts": [],
                }
            ]
        },
    )
    capture = root / "execution/jobs/600348_summary"
    save(capture / "attempt.json", {"requests": 1})
    save(
        capture / "receipt.json",
        {"status": "failed", "usage": None, "cost": {"usd": None}},
    )
    (root / "summaries/600348/完整摘要.md").unlink()
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [root])
    result = importer.run()
    key = artifact_key(
        "summary", "a_share:600348", annual_versions(importer.documents())
    )
    assert store.get_artifact(key) is None
    assert store.get_attempt(key)["receipt"]["status"] == "failed"
    assert result["errors"]


def test_old_priceful_peer_is_reused_with_original_date_warning(tmp_path):
    root = legacy_fixture(tmp_path)
    data = root / "stage1/600348_2025/data.json"
    save(
        root / "peer_input/manifest.json",
        {
            "companies": [{"company": "600348"}],
            "source_hashes": {str(data): file_sha(data)},
        },
    )
    peer = root / "peer/完整分析.md"
    peer.parent.mkdir()
    peer.write_text("原横评，原报价8.33", encoding="utf-8")
    save(root / "execution/jobs/peer_analysis/request.json", {"content": "行情快照"})
    save(root / "market_snapshot.json", [{"code": "600348", "date": "2026-10-23"}])
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [root])
    importer.run()
    versions = {"a_share:600348": annual_versions(importer.documents())}
    artifact = store.get_artifact(artifact_key("peer", ["a_share:600348"], versions))
    assert artifact["metadata"]["legacy_price_dependency"]
    assert artifact["metadata"]["quote_dates"] == ["2026-10-23"]
    assert peer.read_text("utf-8") == "原横评，原报价8.33"


def test_continuation_peer_uses_frozen_explicit_metadata_not_folder_guess(tmp_path):
    root = legacy_fixture(tmp_path)
    data = root / "stage1/600348_2025/data.json"
    later = tmp_path / "later"
    peer = later / "peer/完整分析.md"
    peer.parent.mkdir(parents=True)
    peer.write_text("新批次横评原文", encoding="utf-8")
    content = (
        f"# 元数据\n- source: {data}\n\n# 行情快照\n|600348|2026-10-23|8.33|CNY|\n"
    )
    content += "\n# 本轮摘要真实执行状态与分段状态\n" + json.dumps(
        [{"code": "600348", "years": [2025]}]
    )
    save(
        later / "peer_request.json",
        {
            "messages": [
                {"role": "system", "content": "contract"},
                {"role": "user", "content": content},
            ]
        },
    )
    save(later / "freeze.json", {"files": {str(data.resolve()): file_sha(data)}})
    store = Store(tmp_path / "shared")
    importer = LegacyImporter(store, [root, later])
    result = importer.run()
    key = artifact_key(
        "peer",
        ["a_share:600348"],
        {"a_share:600348": annual_versions(importer.documents())},
    )
    assert store.get_artifact(key)["metadata"]["quote_dates"] == ["2026-10-23"]
    assert not result["errors"]
