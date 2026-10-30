"""Targeted original-report acquisition for PDF research; never invokes an LLM."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .contracts import canonical, fingerprint
from .pdf_sessions import PDFBatchReader

VERSION = "pdf-report-source/1"
# Public-document retries do not authorize model calls.
SOURCE_RETRY_LIMIT = 3
SOURCE_RETRY_DELAY = 300
SOURCE_REQUEST_LEASE = 900
TRANSIENT_ERRORS = {"document_source_network_error", "document_request_interrupted"}
TYPES = {
    "annual": (r"年度报告", "12-31", "annual", "category_ndbg_szsh;"),
    "half_year": (r"半年度报告|中期报告", "06-30", "interim", "category_bndbg_szsh;"),
    "first_quarter": (
        r"(?:第一|一|第1|1)季度报告",
        "03-31",
        "quarterly",
        "category_yjdbg_szsh;",
    ),
    "third_quarter": (
        r"(?:第三|三|第3|3)季度报告",
        "09-30",
        "quarterly",
        "category_sjdbg_szsh;",
    ),
}


class SourceFailure(RuntimeError):
    def __init__(self, code, retry_at=None):
        self.code, self.retry_at = code, retry_at
        super().__init__(code)


class _RateHTTP:
    """Share the archive worker lock/rate ledger for announcement requests too."""

    def __init__(self, worker, guard, http=None):
        self.worker, self.guard = worker, guard
        self.http = http or requests.Session()

    def post(self, url, **kwargs):
        from ..data.dataset_documents import document_worker_lock

        self.guard()
        self.worker._initialize()
        with document_worker_lock(self.worker.lock_path) as acquired:
            if not acquired:
                raise SourceFailure("document_worker_busy", time.time() + 60)
            self.worker._wait_request(time.monotonic() + 60)
            self.guard()
            kwargs["allow_redirects"] = False
            response = self.http.post(url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                from ..data.dataset_documents import _retry_after

                delay = max(60, _retry_after(response.headers.get("Retry-After")))
                with self.worker._db() as db:
                    db.execute(
                        "INSERT INTO document_download_rate VALUES(1,?) ON CONFLICT(id) "
                        "DO UPDATE SET next_request_at=MAX(next_request_at,excluded.next_request_at)",
                        (time.time() + delay,),
                    )
                response.close()
                raise SourceFailure("cninfo_http_throttled", time.time() + delay)
            if response.status_code != 200:
                response.close()
                raise SourceFailure("cninfo_http_" + str(response.status_code))
            return response


class _Discovery:
    def __init__(self, config, worker, guard):
        from ..instruments.point_in_time import CninfoAnnualReportProvider

        self.provider = CninfoAnnualReportProvider(
            config, http=_RateHTTP(worker, guard)
        )

    def discover_report(self, code, report_type, year, as_of):
        provider = self.provider
        org = provider._org_id(code)
        pattern, _, _, category = TYPES[report_type]
        start, end = date(year, 1, 1), min(as_of, date(year + 1, 12, 31))
        candidates = []
        for page in range(1, 11):
            response = provider.http.post(
                provider.ANNOUNCEMENT_URL,
                data={
                    "pageNum": page,
                    "pageSize": 50,
                    "column": "sse" if code.startswith(("5", "6", "9")) else "szse",
                    "tabName": "fulltext",
                    "stock": f"{code},{org}",
                    "searchkey": "",
                    "category": category,
                    "seDate": f"{start.isoformat()}~{end.isoformat()}",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                },
                headers=provider._headers,
                timeout=max(provider.timeout, 45),
            )
            try:
                body = response.json()
            finally:
                response.close()
            if not isinstance(body, dict) or not isinstance(
                body.get("announcements"), list
            ):
                raise SourceFailure("cninfo_discovery_invalid_response")
            for row in body["announcements"]:
                if str(row.get("secCode", code)) != code:
                    continue
                title = re.sub(
                    r"<[^>]+>|\s+", "", str(row.get("announcementTitle", ""))
                )
                if (
                    not re.search(rf"(?<!\d){year}年?(?:{pattern})", title)
                    or "摘要" in title
                    or "英文" in title
                ):
                    continue
                try:
                    timestamp = float(row["announcementTime"]) / 1000
                    published = datetime.fromtimestamp(
                        timestamp, timezone(timedelta(hours=8))
                    ).date()
                except (KeyError, ValueError, TypeError, OverflowError, OSError):
                    continue
                if not start <= published <= end:
                    continue
                url = urljoin(provider.STATIC_ROOT, str(row.get("adjunctUrl", "")))
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "static.cninfo.com.cn"
                    or parsed.username
                    or parsed.password
                    or not parsed.path.lower().endswith(".pdf")
                ):
                    continue
                candidates.append(
                    {
                        "code": code,
                        "report_type": report_type,
                        "report_year": year,
                        "announcement_id": str(row.get("announcementId", "")),
                        "publication_date": published.isoformat(),
                        "source_url": url,
                        "title": title,
                        "timestamp": timestamp,
                    }
                )
            total = int(body.get("totalpages", page))
            if page >= total or not body["announcements"]:
                break
            if page == 10:
                raise SourceFailure("cninfo_discovery_pagination_incomplete")
        if not candidates:
            raise SourceFailure("report_not_found")
        return max(candidates, key=lambda r: r["timestamp"])


class SessionReportSource:
    def __init__(self, service, *, discovery=None, archive=None, clock=time.time):
        self.service, self.store, self.clock = service, service.store, clock
        self.project_root = Path(
            getattr(service.sources, "project_root", Path.cwd())
        ).resolve()
        self.reader = PDFBatchReader(
            self.store,
            service.root / "pdf_pages",
            project_root=self.project_root,
            allowed_roots=[self.project_root],
        )
        self.discovery, self.archive = discovery, archive
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pdf_report_source_attempts (
                request_key TEXT PRIMARY KEY, evidence_key TEXT NOT NULL,
                result TEXT NOT NULL, retry_at REAL, updated_at REAL NOT NULL)""")

    def _guard(self):
        if self.service.acceptance.enabled:
            state = self.service.acceptance.state()
            if state.get("status") != "active":
                raise SourceFailure("active_research_budget_required")
            for kind, limit in state.get("limits", {}).items():
                if state.get("usage", {}).get(kind, 0) >= limit:
                    raise SourceFailure("acceptance_budget_exhausted")

    def _cached(self, company, report_type, year):
        _, end, kind, _ = TYPES[report_type]
        found = []
        for doc in self.reader.discover_catalog(company, self.service.today):
            if doc.period_end == f"{year}-{end}" and doc.kind == kind:
                # Verify immutable archive bytes without requiring text extraction.
                self.reader._bytes(doc)
                found.append(asdict(doc))
        return found

    def _envelope(self, status, documents=None, error=None, retry_at=None):
        documents = documents or []
        return {
            "status": status,
            "data": {"documents": documents},
            "documents": documents,
            "source_refs": [d["document_id"] for d in documents],
            "as_of": self.service.today.isoformat(),
            "coverage": {"documents": len(documents)},
            "errors": []
            if not error
            else [{"code": error, "stage": "document_acquisition"}],
            "retryable": retry_at is not None,
            "retry_at": retry_at,
            "next_action": "read_original_pdf"
            if documents
            else "wait_until_retry_at"
            if retry_at
            else "new_evidence_or_source_revision_required",
        }

    def collect(self, company, report_type, year):
        if report_type not in TYPES or type(year) is not int:
            return self._envelope("unavailable", error="invalid_report_request")
        if not self.service.today.year - 7 <= year <= self.service.today.year:
            return self._envelope("unavailable", error="report_year_outside_scope")
        if not str(company.get("id", "")).startswith("a_share:") or not re.fullmatch(
            r"\d{6}", str(company.get("code", ""))
        ):
            return self._envelope(
                "unavailable", error="targeted_market_source_unavailable"
            )
        request_key = fingerprint([company["id"], report_type, year])
        evidence_key = fingerprint(
            [VERSION, company, self.service.settings.get("source_revision", "1")]
        )
        try:
            cached = self._cached(company, report_type, year)
            if cached:
                return self._envelope("success", cached)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            return self._envelope(
                "conflict", error=getattr(exc, "code", type(exc).__name__)
            )
        try:
            self._guard()
        except SourceFailure as exc:
            return self._envelope("unavailable", error=exc.code)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM pdf_report_source_attempts WHERE request_key=?",
                (request_key,),
            ).fetchone()
            previous = (
                json.loads(row["result"])
                if row and row["evidence_key"] == evidence_key
                else {}
            )
            attempts = int(previous.get("attempt_count", 1)) if previous else 0
            # Migrate only legacy transient failures, never missing reports or
            # parser conflicts. Delay first so a restart does not cause a burst.
            old_transient = any(
                isinstance(error, dict) and error.get("code") in TRANSIENT_ERRORS
                for error in previous.get("errors", [])
            )
            if (
                row
                and previous
                and row["retry_at"] is None
                and old_transient
                and not previous.get("retry_policy_version")
            ):
                previous.update(
                    retryable=True,
                    retry_at=self.clock() + SOURCE_RETRY_DELAY,
                    next_action="wait_until_retry_at",
                    attempt_count=attempts,
                    retry_policy_version=1,
                )
                db.execute(
                    "UPDATE pdf_report_source_attempts SET result=?,retry_at=?,"
                    "updated_at=? WHERE request_key=?",
                    (
                        canonical(previous),
                        previous["retry_at"],
                        self.clock(),
                        request_key,
                    ),
                )
                return previous
            if (
                row
                and row["evidence_key"] == evidence_key
                and (row["retry_at"] is None or row["retry_at"] > self.clock())
            ):
                return json.loads(row["result"])
            # Persist count and lease before I/O; restart cannot reset attempts.
            if attempts >= SOURCE_RETRY_LIMIT:
                previous.update(
                    retryable=False,
                    retry_at=None,
                    next_action="new_evidence_or_source_revision_required",
                )
                return previous
            attempt_count = attempts + 1
            pending = self._envelope(
                "unavailable",
                error="document_request_interrupted",
                retry_at=(self.clock() + SOURCE_REQUEST_LEASE)
                if attempt_count < SOURCE_RETRY_LIMIT
                else None,
            )
            pending.update(attempt_count=attempt_count, retry_policy_version=1)
            db.execute(
                "INSERT OR REPLACE INTO pdf_report_source_attempts VALUES(?,?,?,?,?)",
                (
                    request_key,
                    evidence_key,
                    canonical(pending),
                    pending["retry_at"],
                    self.clock(),
                ),
            )
        try:
            self._guard()
            if self.discovery is None:
                from ..data.dataset_catalog import DatasetCatalog
                from ..data.dataset_documents import DatasetDocumentWorker

                catalog = DatasetCatalog(self.project_root)
                worker = DatasetDocumentWorker(
                    catalog, max_document_bytes=50 * 1024 * 1024
                )
                self.discovery = _Discovery(self.service.config, worker, self._guard)
                self.archive = lambda report: self._archive(catalog, worker, report)
            report = self.discovery.discover_report(
                company["code"], report_type, year, self.service.today
            )
            self._guard()
            self.archive(report)
            documents = self._cached(company, report_type, year)
            if not documents:
                raise SourceFailure("original_pdf_missing_after_archive")
            result = self._envelope("success", documents)
        except SourceFailure as exc:
            result = self._envelope(
                "missing" if exc.code == "report_not_found" else "unavailable",
                error=exc.code,
                retry_at=exc.retry_at,
            )
        except requests.RequestException:
            result = self._envelope(
                "unavailable",
                error="document_source_network_error",
                retry_at=self.clock() + SOURCE_RETRY_DELAY * 2 ** (attempt_count - 1),
            )
        except Exception as exc:  # noqa: BLE001 -- retain visible failure, never empty success
            result = self._envelope(
                "unavailable", error=getattr(exc, "code", type(exc).__name__)
            )
        result.update(attempt_count=attempt_count, retry_policy_version=1)
        if result.get("retryable") and attempt_count >= SOURCE_RETRY_LIMIT:
            result.update(
                retryable=False,
                retry_at=None,
                retry_exhausted=True,
                next_action="new_evidence_or_source_revision_required",
            )
        result["request"] = {
            "company_id": company["id"],
            "report_type": report_type,
            "year": year,
        }
        with self.store.transaction() as db:
            db.execute(
                "UPDATE pdf_report_source_attempts SET result=?,retry_at=?,updated_at=? WHERE request_key=?",
                (canonical(result), result.get("retry_at"), self.clock(), request_key),
            )
        self.store.event(company["id"], "pdf_report_source_result", result)
        return result

    def _archive(self, catalog, worker, report):
        from ..data.cninfo_reports import CninfoReportService

        document_id = CninfoReportService._document_id(report)
        catalog.register_document_source(
            document_id,
            report["source_url"],
            report["title"],
            report["publication_date"],
            report["code"],
        )
        self._guard()
        worker.run(max_documents=1, max_seconds=180, job_ids=[document_id])
        jobs = catalog.document_jobs(job_ids=[document_id], limit=1)["items"]
        if not jobs:
            raise SourceFailure("archive_job_missing")
        if not any(a.get("kind") == "pdf" for a in jobs[0].get("artifact_paths", [])):
            with sqlite3.connect(catalog.db_path) as db:
                row = db.execute(
                    "SELECT error,next_attempt_at FROM document_downloads WHERE job_id=?",
                    (document_id,),
                ).fetchone()
            raise SourceFailure(
                "original_pdf_archive_failed",
                row[1] if row and row[1] > self.clock() else None,
            )
