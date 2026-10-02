"""Fetch, archive, and index official CNINFO A-share full reports."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import requests

from src.data.dataset_catalog import DatasetCatalog
from src.data.dataset_documents import (
    MAX_TEXT_BYTES,
    DatasetDocumentWorker,
    DocumentBlocked,
    render_pdf_page_images,
)
from src.instruments.point_in_time import CninfoAnnualReportProvider

MAX_REPORT_BYTES = 50 * 1024 * 1024
MAX_REPORT_PAGES = 300
MAX_REPORT_TEXT = MAX_TEXT_BYTES
PREVIEW_CHARS = 24000


class CninfoReportService:
    """Narrow A-share report flow backed by the existing catalog worker."""

    def __init__(self, project_root, vision_client, *, stop=None, provider=None):
        self.root = Path(project_root).resolve()
        self.catalog = DatasetCatalog(self.root)
        self.vision_client = vision_client
        self.stop = stop or threading.Event()
        self.provider = provider or CninfoAnnualReportProvider({})

    @staticmethod
    def _document_id(report: dict) -> str:
        key = [
            report["code"],
            report["report_type"],
            report["report_year"],
            report["announcement_id"],
        ]
        digest = hashlib.sha256(
            json.dumps(key, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        return "cninfo_" + digest

    def fetch(self, code, report_type, year=None) -> dict:
        if self.stop.is_set():
            raise RuntimeError("助手正在停止")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码必须是六位 A 股代码")
        if report_type not in {"annual", "half_year"}:
            raise ValueError("报告类型仅支持 annual 或 half_year")
        try:
            report = self.provider.discover_report(code, report_type, year)
        except LookupError as exc:
            return {
                "status": "not_found",
                "code": code,
                "report_type": report_type,
                "year": year,
                "reason": str(exc),
            }
        except requests.RequestException as exc:
            return {
                "status": "failed",
                "code": code,
                "report_type": report_type,
                "year": year,
                "reason": "cninfo_lookup_network_error",
                "error_type": type(exc).__name__,
            }
        document_id = self._document_id(report)
        self.catalog.register_document_source(
            document_id,
            report["source_url"],
            report["title"],
            report["publication_date"],
            report["code"],
        )
        saved = self.catalog.report_parse_state(document_id)
        if (
            saved
            and saved["status"] in {"succeeded", "partial"}
            and self._cached_result_intact(document_id, saved["result"])
        ):
            return self._public_result(
                report, document_id, saved["status"], saved["result"]
            )
        self.catalog.set_report_parse_state(document_id, "running", {})
        worker = DatasetDocumentWorker(
            self.catalog,
            stop=self.stop,
            max_document_bytes=MAX_REPORT_BYTES,
            max_pdf_pages=MAX_REPORT_PAGES,
        )
        worker_result = worker.run(
            max_documents=1,
            max_seconds=180,
            job_ids=[document_id],
        )
        job_page = self.catalog.document_jobs(job_ids=[document_id], limit=1)
        job = next(iter(job_page["items"]), None)
        if job is None:
            result = {"reason": "catalog_registration_failed"}
            self.catalog.set_report_parse_state(document_id, "failed", result)
            return self._public_result(report, document_id, "failed", result)
        artifacts = job.get("artifact_paths", [])
        pdf = next((item for item in artifacts if item.get("kind") == "pdf"), None)
        text = next((item for item in artifacts if item.get("kind") == "text"), None)
        download_result = job.get("download_result") or {}
        if not pdf:
            result = {
                "reason": job.get("download_error")
                or (worker_result.get("processed") or [{}])[0].get("status")
                or "download_failed",
                "worker": worker_result,
            }
            self.catalog.set_report_parse_state(document_id, "failed", result)
            return self._public_result(report, document_id, "failed", result)

        if text:
            text_path = self.root / text["relative_path"]
            content = text_path.read_text(encoding="utf-8", errors="replace")
            status = (
                "partial"
                if download_result.get("status") == "text_truncated"
                else "succeeded"
            )
            result = {
                "status": download_result.get("status", "text_extracted"),
                "pages": download_result.get("pages"),
                "parsed_pages": min(
                    download_result.get("pages") or MAX_REPORT_PAGES,
                    MAX_REPORT_PAGES,
                ),
                "text_file_id": text["file_id"],
                "text_path": text["relative_path"],
                "pdf_path": pdf["relative_path"],
                "text_sha256": text.get("sha256"),
                "text_preview": content[:PREVIEW_CHARS],
                "preview_truncated": len(content) > PREVIEW_CHARS,
            }
            self.catalog.set_report_parse_state(document_id, status, result)
            return self._public_result(report, document_id, status, result)

        pdf_path = self._safe_artifact_path(pdf["relative_path"])
        try:
            import pdfplumber

            with pdfplumber.open(pdf_path) as document:
                page_total = len(document.pages)
        except Exception as exc:  # noqa: BLE001 - preserve original and report parser gap
            result = {"reason": "pdf_open_failed", "error_type": type(exc).__name__}
            self.catalog.set_report_parse_state(document_id, "failed", result)
            return self._public_result(report, document_id, "failed", result)
        if page_total > MAX_REPORT_PAGES:
            result = {
                "reason": "pdf_page_limit_exceeded",
                "pages": page_total,
                "parsed_pages": 0,
                "pdf_path": pdf["relative_path"],
            }
            self.catalog.set_report_parse_state(document_id, "partial", result)
            return self._public_result(report, document_id, "partial", result)

        output, failed_pages = [], []
        deadline = time.monotonic() + 240
        for first_page in range(1, page_total + 1, 12):
            if self.stop.is_set() or time.monotonic() >= deadline:
                failed_pages.extend(range(first_page, page_total + 1))
                break
            try:
                pages = render_pdf_page_images(
                    pdf_path,
                    max_pages=MAX_REPORT_PAGES,
                    start_page=first_page,
                    page_count=12,
                )
                transcription = self.vision_client.read_report_images(
                    pages, deadline=deadline
                )
                output.append(transcription.strip())
                seen_pages = {
                    int(value)
                    for value in re.findall(
                        r"(?:\[|【)?\s*PAGE\s*(\d+)",
                        transcription,
                        re.IGNORECASE,
                    )
                }
                expected_pages = {item["page"] for item in pages}
                failed_pages.extend(sorted(expected_pages - seen_pages))
            except Exception as exc:  # noqa: BLE001 - keep parsing other page batches
                failed_pages.extend(
                    range(first_page, min(page_total, first_page + 11) + 1)
                )
                output.append(
                    f"[PAGE {first_page}-{min(page_total, first_page + 11)}: "
                    f"视觉识读失败 ({type(exc).__name__})]"
                )
        recognized = "\n\n".join(output).strip()
        if not recognized:
            result = {
                "reason": "vision_extraction_failed",
                "pages": page_total,
                "parsed_pages": 0,
                "pdf_path": pdf["relative_path"],
                "failed_pages": failed_pages,
            }
            self.catalog.set_report_parse_state(document_id, "failed", result)
            return self._public_result(report, document_id, "failed", result)
        if len(recognized.encode("utf-8")) > MAX_REPORT_TEXT:
            encoded = recognized.encode("utf-8")[:MAX_REPORT_TEXT]
            recognized = encoded.decode("utf-8", errors="ignore")
            failed_pages.extend(range(1, page_total + 1))
        text_path = pdf_path.with_name("document.txt")
        text_path.write_text(recognized, encoding="utf-8")
        relative_text = text_path.relative_to(self.root).as_posix()
        self.catalog.record_document(document_id, relative_text, status="available")
        text_record = next(
            (
                item
                for item in self.catalog.document_jobs(job_ids=[document_id], limit=1)[
                    "items"
                ][0]["artifact_paths"]
                if item.get("kind") == "text"
            ),
            {},
        )
        status = "partial" if failed_pages else "succeeded"
        result = {
            "status": "visual_text_extracted"
            if not failed_pages
            else "visual_text_partial",
            "pages": page_total,
            "parsed_pages": max(0, page_total - len(set(failed_pages))),
            "failed_pages": sorted(set(failed_pages)),
            "text_file_id": text_record.get("file_id"),
            "text_path": relative_text,
            "pdf_path": pdf["relative_path"],
            "text_preview": recognized[:PREVIEW_CHARS],
            "preview_truncated": len(recognized) > PREVIEW_CHARS,
        }
        self.catalog.set_report_parse_state(document_id, status, result)
        return self._public_result(report, document_id, status, result)

    def _safe_artifact_path(self, relative):
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise DocumentBlocked("document_path_outside_archive")
        path = self.root / relative_path
        expected_root = (self.root / "data" / "dataset_documents").resolve()
        if not path.is_relative_to(self.root / "data" / "dataset_documents"):
            raise DocumentBlocked("document_path_outside_archive")
        if any(
            part.is_symlink() for part in (path, *path.parents) if part != self.root
        ):
            raise DocumentBlocked("document_path_redirected")
        path = path.resolve()
        if not path.is_relative_to(expected_root) or not path.is_file():
            raise DocumentBlocked("document_path_outside_archive")
        return path

    def _cached_result_intact(self, document_id, result):
        page = self.catalog.document_jobs(job_ids=[document_id], limit=1)
        if not page["items"]:
            return False
        artifacts = page["items"][0].get("artifact_paths", [])
        pdf = next((item for item in artifacts if item.get("kind") == "pdf"), None)
        text = next((item for item in artifacts if item.get("kind") == "text"), None)
        if not pdf or not text or text.get("file_id") != result.get("text_file_id"):
            return False
        try:
            pdf_path = self._safe_artifact_path(pdf["relative_path"])
            if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != pdf.get("sha256"):
                return False
            self.catalog.read_text(text["file_id"], start_line=1, line_count=1)
        except (OSError, ValueError, KeyError):
            return False
        return True

    @staticmethod
    def _public_result(report, document_id, status, result):
        detail = dict(result)
        parse_status = detail.pop("status", None)
        return {
            "status": status,
            "parse_status": parse_status,
            "document_id": document_id,
            "code": report["code"],
            "report_type": report["report_type"],
            "report_year": report["report_year"],
            "title": report["title"],
            "publication_date": report["publication_date"],
            "source_url": report["source_url"],
            **detail,
        }
