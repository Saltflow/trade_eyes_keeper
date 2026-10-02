"""A-share CNINFO discovery, catalog and report parsing acceptance tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.data import cninfo_reports
from src.data.cninfo_reports import CninfoReportService
from src.data.dataset_catalog import DatasetCatalog
from src.instruments.point_in_time import CninfoAnnualReportProvider


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def timestamp(year, month, day):
    value = datetime(year, month, day, tzinfo=timezone(timedelta(hours=8)))
    return int(value.timestamp() * 1000)


def announcement(title, published, adjunct, announcement_id):
    return {
        "announcementTitle": title,
        "announcementTime": timestamp(*published),
        "adjunctUrl": adjunct,
        "announcementId": announcement_id,
    }


class FakeProvider:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def discover_report(self, code, report_type, year):
        self.calls.append((code, report_type, year))
        return dict(self.report)


def test_discovery_selects_latest_complete_report_and_ignores_abstracts():
    http = Mock()
    http.post.side_effect = [
        Response([{"code": "600519", "orgId": "gssz0000519"}]),
        Response(
            {
                "totalpages": 1,
                "announcements": [
                    announcement(
                        "贵州茅台2025年年度报告摘要",
                        (2026, 4, 15),
                        "finalpage/2026-04-15/abstract.PDF",
                        "abstract",
                    ),
                    announcement(
                        "贵州茅台2024年年度报告",
                        (2025, 4, 2),
                        "finalpage/2025-04-02/old.PDF",
                        "old",
                    ),
                    announcement(
                        "贵州茅台2025年年度报告",
                        (2026, 4, 15),
                        "finalpage/2026-04-15/full.PDF",
                        "annual-2025",
                    ),
                ],
            }
        ),
    ]
    provider = CninfoAnnualReportProvider({}, http=http)
    report = provider.discover_report("600519", "annual")
    assert report["report_year"] == 2025
    assert report["publication_date"] == "2026-04-15"
    assert report["source_url"] == (
        "https://static.cninfo.com.cn/finalpage/2026-04-15/full.PDF"
    )
    assert report["announcement_id"] == "annual-2025"
    assert http.post.call_args_list[-1].kwargs["data"]["pageNum"] == 1


def test_discovery_uses_requested_year_for_half_year_report():
    http = Mock()
    http.post.side_effect = [
        Response([{"code": "002594", "orgId": "gssz002594"}]),
        Response(
            {
                "totalpages": 1,
                "announcements": [
                    announcement(
                        "比亚迪2023年半年度报告",
                        (2023, 8, 28),
                        "finalpage/2023-08-28/2023.PDF",
                        "h1-2023",
                    ),
                    announcement(
                        "比亚迪2024年半年度报告",
                        (2024, 8, 27),
                        "finalpage/2024-08-27/2024.PDF",
                        "h1-2024",
                    ),
                ],
            }
        ),
    ]
    report = CninfoAnnualReportProvider({}, http=http).discover_report(
        "002594", "half_year", 2023
    )
    assert report["report_year"] == 2023
    assert report["report_type"] == "half_year"
    assert http.post.call_args_list[-1].kwargs["data"]["seDate"].startswith("2023-")


def _sample_report(code="600519"):
    return {
        "code": code,
        "report_type": "annual",
        "report_year": 2025,
        "title": "贵州茅台2025年年度报告",
        "publication_date": "2026-04-15",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-15/full.PDF",
        "announcement_id": "annual-2025",
    }


def install_archive_worker(monkeypatch, root, *, text=None, pdf_bytes=None):
    def run(worker, *, job_ids, **_kwargs):
        assert len(job_ids) == 1
        document_id = job_ids[0]
        directory = Path(root) / "data/dataset_documents" / "sample-report"
        directory.mkdir(parents=True, exist_ok=True)
        pdf = directory / "original.pdf"
        pdf.write_bytes(pdf_bytes or b"%PDF-1.4\narchive fixture")
        worker.catalog.record_document(
            document_id, pdf.relative_to(root).as_posix(), status="available"
        )
        if text is not None:
            document = directory / "document.txt"
            document.write_text(text, encoding="utf-8")
            worker.catalog.record_document(
                document_id, document.relative_to(root).as_posix(), status="available"
            )
        return {"processed": [{"job_id": document_id, "status": "succeeded"}]}

    monkeypatch.setattr(cninfo_reports.DatasetDocumentWorker, "run", run)


def test_fetch_archives_text_report_and_repeated_request_is_idempotent(
    tmp_path, monkeypatch
):
    provider = FakeProvider(_sample_report())
    install_archive_worker(monkeypatch, tmp_path, text="--- PAGE 1 ---\n收入增长 15%")
    service = CninfoReportService(tmp_path, Mock(), provider=provider)
    first = service.fetch("600519", "annual")
    second = service.fetch("600519", "annual")
    assert first["status"] == second["status"] == "succeeded"
    assert first["text_file_id"]
    assert first["text_preview"].endswith("收入增长 15%")
    assert first["document_id"] == second["document_id"]
    assert provider.calls == [("600519", "annual", None), ("600519", "annual", None)]
    # The second call returns the persisted state without starting the worker.
    assert (
        len(
            DatasetCatalog(tmp_path).document_jobs(job_ids=[first["document_id"]])[
                "items"
            ][0]["artifact_paths"]
        )
        == 2
    )


def test_official_lookup_network_failure_is_not_reported_as_success(tmp_path):
    import requests

    class BrokenProvider:
        def discover_report(self, *_args):
            raise requests.Timeout("private response detail")

    result = CninfoReportService(tmp_path, Mock(), provider=BrokenProvider()).fetch(
        "600519", "annual"
    )
    assert result["status"] == "failed"
    assert result["reason"] == "cninfo_lookup_network_error"
    assert "private response detail" not in str(result)


def test_scanned_pdf_uses_page_vision_and_persists_page_citations(
    tmp_path, monkeypatch
):
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        (
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << >> /Contents 4 0 R >> endobj\n"
        ),
        b"4 0 obj << /Length 0 >> stream\n\nendstream endobj\n",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for item in objects:
        offsets.append(len(pdf))
        pdf.extend(item)
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    install_archive_worker(monkeypatch, tmp_path, pdf_bytes=bytes(pdf))
    vision = Mock()
    vision.read_report_images.return_value = "[PAGE 1]\n资产总计：100亿元"
    service = CninfoReportService(
        tmp_path, vision, provider=FakeProvider(_sample_report())
    )
    result = service.fetch("600519", "annual")
    assert result["status"] == "succeeded"
    assert result["parsed_pages"] == 1
    assert "[PAGE 1]" in result["text_preview"]
    vision.read_report_images.assert_called_once()
    assert vision.read_report_images.call_args.args[0][0]["page"] == 1
    matches = DatasetCatalog(tmp_path).search_document_text(
        result["document_id"], "资产总计"
    )
    assert matches["results"][0]["page"] == 1
    assert "100亿元" in matches["results"][0]["text"]
