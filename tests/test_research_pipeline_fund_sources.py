"""Fund originals enter the existing source registry and material authority."""

import hashlib
import socket
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from src.data.us_announcements import SecAnnouncementProvider
from src.research_pipeline.common import file_sha
from src.research_pipeline.models import Instrument, ResearchSpec
from src.research_pipeline.sources import SourceError, SourceService


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network is forbidden in fund report unit tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


class Response:
    status_code = 200
    headers: ClassVar[dict] = {}

    def __init__(self, value=None, content=b""):
        self.value, self.content = value, content

    def json(self):
        return self.value

    def raise_for_status(self):
        pass


def service(tmp_path, code, http=None, **kwargs):
    spec = ResearchSpec(
        (code,),
        tmp_path / "run",
        repo=tmp_path,
        store_root=tmp_path / "store",
        start_year=2024,
        end_year=2025,
    )
    return SourceService(
        spec,
        SimpleNamespace(root=spec.store_root),
        http=http,
        minimum_interval=0,
        **kwargs,
    )


def sse_row(title="基金2025年年度报告", code="508091", suffix="annual"):
    return {
        "SECURITY_CODE": code,
        "TITLE": title,
        "SSEDATE": "2026-03-27",
        "URL": f"/disclosure/fund/announcement/c/new/{code}_{suffix}.pdf",
    }


@pytest.mark.parametrize("code", ["508091", "510880", "520550", "560800"])
def test_sse_funds_use_shared_directory_and_immutable_acquisition(tmp_path, code):
    calls = []
    rows = [
        sse_row(title, code, str(i))
        for i, title in enumerate(
            [
                "基金2025年年度报告",
                "基金2025年年度报告更正公告",
                "基金2025年年度报告（修订版）",
                "基金2025年年度报告摘要",
                "基金2025年年度报告提示性公告",
                "基金2025年半年度报告",
                "基金2023年年度报告",
            ]
        )
    ]

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"pageHelp": {"pageNo": 1, "total": len(rows)}, "result": rows})

    downloads = []

    def download(document, path):
        downloads.append(document)
        return b"%PDF-1.7 official fund fixture"

    source = service(
        tmp_path, "a_share:" + code, SimpleNamespace(get=get), downloader=download
    )
    docs = source.discover(Instrument.parse("a_share:" + code), date(2026, 11, 3))
    assert [doc.kind for doc in docs] == ["annual", "amendment", "annual"]
    assert all(doc.period_start is None and doc.period_end is None for doc in docs)
    assert all(doc.provenance["source"] == "sse" for doc in docs)
    assert calls[0][1]["params"]["SECURITY_CODE"] == code
    first = source.acquire(docs[0])
    again = source.acquire(docs[0])
    assert len(downloads) == 1 and first.sha256 == again.sha256
    assert first.sha256 == file_sha(Path(first.local_path))
    assert (
        source.hub.lookup(first.url, version=first.document_id)["sha256"]
        == first.sha256
    )
    assert {item.sha256 for item in source.local_documents()} == {first.sha256}
    # Exact official IDs must not reuse a different revision just by URL.
    assert (
        source._reuse_candidate(replace(docs[0], document_id="sse:other"), [first])
        is None
    )


@pytest.mark.parametrize(
    "fault", ["wrong_fund", "empty_page", "total_changed", "repeat", "limit"]
)
def test_sse_fund_incomplete_directory_never_becomes_empty_success(tmp_path, fault):
    count = 0

    def get(url, **kwargs):
        nonlocal count
        count += 1
        row = sse_row(code="508002" if fault == "wrong_fund" else "508091")
        total = 1 if fault == "wrong_fund" else 101
        if count > 1 and fault == "total_changed":
            total = 100
        batch = [] if count > 1 and fault == "empty_page" else [row]
        if fault == "limit":
            batch = [sse_row(suffix=str(count))]
        return Response(
            {"pageHelp": {"pageNo": count, "total": total}, "result": batch}
        )

    source = service(tmp_path, "a_share:508091", SimpleNamespace(get=get))
    with pytest.raises(SourceError):
        source.discover(Instrument.parse("a_share:508091"), date(2026, 11, 3))


def cninfo_row(index=0, code="159915"):
    return {
        "secCode": code,
        "announcementTitle": "基金2025年年度报告",
        "announcementId": str(index),
        "announcementTime": 1774886400000,
        "adjunctUrl": f"finalpage/2026-03-31/{index}.PDF",
    }


def test_szse_fund_uses_fund_identity_category_and_has_more_pagination(tmp_path):
    calls = []

    def get(url, **kwargs):
        assert url.endswith("fund_stock.json")
        return Response({"stockList": [{"code": "159915", "orgId": "fund-manager"}]})

    def post(url, **kwargs):
        data = kwargs["data"]
        calls.append(data)
        first = data["pageNum"] == 1
        return Response(
            {
                "announcements": [cninfo_row(i) for i in range(50)]
                if first
                else [cninfo_row(50)],
                "totalAnnouncement": 51,
                "totalpages": 0,  # The real fund API reports zero even when rows exist.
                "hasMore": first,
            }
        )

    source = service(tmp_path, "a_share:159915", SimpleNamespace(get=get, post=post))
    docs = source.discover(Instrument.parse("a_share:159915"), date(2026, 11, 3))
    assert len(docs) == 51 and len(calls) == 2
    assert calls[0]["column"] == "fund"
    assert calls[0]["category"] == "category_ndbg_jjgg;"
    assert calls[0]["stock"] == "159915,fund-manager"
    assert all(doc.period_start is None for doc in docs)


@pytest.mark.parametrize(
    "fault",
    ["missing_identity", "ambiguous_identity", "truncated", "repeated", "wrong_fund"],
)
def test_szse_fund_rejects_bad_directory_evidence(tmp_path, fault):
    identities = [{"code": "159915", "orgId": "one"}]
    if fault == "missing_identity":
        identities = []
    if fault == "ambiguous_identity":
        identities.append({"code": "159915", "orgId": "two"})

    def post(url, **kwargs):
        row = cninfo_row(code="159919" if fault == "wrong_fund" else "159915")
        return Response(
            {
                "announcements": [row, row] if fault == "repeated" else [row],
                "totalAnnouncement": 2 if fault in {"truncated", "repeated"} else 1,
                "hasMore": False,
            }
        )

    source = service(
        tmp_path,
        "a_share:159915",
        SimpleNamespace(
            get=lambda *a, **k: Response({"stockList": identities}), post=post
        ),
    )
    with pytest.raises(SourceError):
        source.discover(Instrument.parse("a_share:159915"), date(2026, 11, 3))


def sec_index(
    *,
    fund_class="C000092055",
    symbol="VOO",
    period="2025-12-31",
    form="N-CSR",
    href=None,
):
    href = href or "/ix?doc=/Archives/edgar/data/36405/000110465926021519/annual.htm"
    return f"""<html><div class="infoHead">Period of Report</div>
    <div class="info">{period}</div><table><tr>
    <td><a>{fund_class}</a></td><td>ETF Shares</td><td>{symbol}</td></tr></table>
    <table summary="Document Format Files"><tr><td>1</td><td>Annual</td>
    <td><a href="{href}">annual.htm</a></td><td>{form}</td></tr></table></html>""".encode()


INDEX = "https://www.sec.gov/Archives/edgar/data/36405/000110465926021519/0001104659-26-021519-index.htm"


@pytest.mark.parametrize("form", ["N-CSR", "N-CSR/A"])
def test_sec_fund_resolves_original_and_explicit_period_in_existing_pipeline(
    tmp_path, monkeypatch, form
):
    monkeypatch.setenv("SEC_USER_AGENT", "fund-report-tests test@example.com")
    monkeypatch.setattr(
        SecAnnouncementProvider, "_identity", lambda *a: ("0000036405", "C000092055")
    )
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        if "browse-edgar" in url:
            return Response(
                content=f"""<feed><company-info><cik>36405</cik></company-info>
            <entry><filing-date>2026-02-27</filing-date><filing-type>{form}</filing-type>
            <filing-href>{INDEX}</filing-href></entry>
            <entry><filing-date>2026-08-27</filing-date><filing-type>N-CSRS</filing-type>
            <filing-href>{INDEX.replace("021519", "102240")}</filing-href></entry></feed>""".encode()
            )
        assert url == INDEX
        return Response(content=sec_index(form=form))

    downloads = []

    def download(document, path):
        downloads.append(document)
        return b"<html><body>Fund financial statements</body></html>"

    source = service(tmp_path, "us:VOO", SimpleNamespace(get=get), downloader=download)
    docs = source.discover(Instrument.parse("us:VOO"), date(2026, 11, 3))
    assert len(docs) == 1 and len(requested) == 2
    doc = docs[0]
    assert doc.fiscal_year == 2025 and doc.period_end == "2025-12-31"
    assert doc.kind == ("amendment" if form.endswith("/A") else "annual")
    assert doc.content_type == "text/html" and doc.url.endswith("/annual.htm")
    evidence = doc.provenance["official_directory_row"]
    assert evidence["fund_class"] == "C000092055"
    assert evidence["filing_index_url"] == INDEX
    assert (
        evidence["filing_index_sha256"]
        == hashlib.sha256(sec_index(form=form)).hexdigest()
    )
    first, again = source.acquire(doc), source.acquire(doc)
    assert len(downloads) == 1 and first.sha256 == again.sha256
    assert source.hub.lookup(doc.url, version=doc.document_id)["sha256"] == first.sha256


@pytest.mark.parametrize(
    "change",
    [
        {"fund_class": "C000007778"},
        {"symbol": "VTV"},
        {"period": "2027-12-31"},
        {"period": ""},
        {"form": "N-CSRS"},
        {"href": "https://evil.test/annual.htm"},
        {"href": "/Archives/edgar/data/36405/000110465926102240/annual.htm"},
        {"href": "/ix?doc=https://evil.test/annual.htm"},
    ],
)
def test_sec_fund_wrong_identity_period_and_original_are_rejected(monkeypatch, change):
    monkeypatch.setenv("SEC_USER_AGENT", "fund-report-tests test@example.com")
    provider = SecAnnouncementProvider(
        http=SimpleNamespace(get=lambda *a, **k: Response(content=sec_index(**change)))
    )
    row = {"stock_code": "VOO", "url": INDEX, "date": "2026-02-27", "type": "N-CSR"}
    with pytest.raises(ValueError):
        provider.resolve_fund_report(row, "0000036405", "C000092055")


def test_cninfo_fund_empty_is_explicit_and_successful(tmp_path):
    http = SimpleNamespace(
        get=lambda *a, **k: Response(
            {"stockList": [{"code": "159915", "orgId": "one"}]}
        ),
        post=lambda *a, **k: Response(
            {
                "announcements": None,
                "totalAnnouncement": 0,
                "totalRecordNum": 0,
                "hasMore": False,
            }
        ),
    )
    source = service(tmp_path, "a_share:159915", http)
    assert source.discover(Instrument.parse("a_share:159915"), date(2026, 11, 3)) == []


def test_sse_source_does_not_accept_external_originals(tmp_path):
    row = {**sse_row(), "URL": "https://evil.test/annual.pdf"}
    http = SimpleNamespace(
        get=lambda *a, **k: Response(
            {"pageHelp": {"pageNo": 1, "total": 1}, "result": [row]}
        )
    )
    source = service(tmp_path, "a_share:508091", http)
    with pytest.raises(SourceError, match="identity invalid"):
        source.discover(Instrument.parse("a_share:508091"), date(2026, 11, 3))


def test_bad_sec_fund_annual_keeps_gap_and_other_valid_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "fund-report-tests test@example.com")
    monkeypatch.setattr(
        SecAnnouncementProvider, "_identity", lambda *a: ("0000036405", "C000092055")
    )
    rows = [
        {"stock_code": "VOO", "type": form, "date": "2026-02-27", "url": str(i)}
        for i, form in enumerate(["N-CSR", "N-CSR", "NPORT-P"])
    ]
    monkeypatch.setattr(SecAnnouncementProvider, "_fund_filings", lambda *a: rows)

    def resolve(self, row, cik, fund_class):
        if row["url"] == "0":
            raise ValueError("wrong class")
        assert row["type"] == "N-CSR"
        return {
            **row,
            "url": INDEX.replace("-index.htm", ".htm"),
            "reportDate": "2025-12-31",
        }

    monkeypatch.setattr(SecAnnouncementProvider, "resolve_fund_report", resolve)
    source = service(tmp_path, "us:VOO")
    docs = source.discover(Instrument.parse("us:VOO"), date(2026, 11, 3))
    assert len(docs) == 1 and docs[0].fiscal_year == 2025
    assert source.gaps[0]["error"] == "sec_fund_annual_row_invalid"
    assert source.gaps[0]["detail"] == "wrong class"


def test_sse_source_requires_pdf_original(tmp_path):
    row = {**sse_row(), "URL": "https://www.sse.com.cn/annual.html"}
    http = SimpleNamespace(
        get=lambda *a, **k: Response(
            {"pageHelp": {"pageNo": 1, "total": 1}, "result": [row]}
        )
    )
    source = service(tmp_path, "a_share:508091", http)
    with pytest.raises(SourceError, match="identity invalid"):
        source.discover(Instrument.parse("a_share:508091"), date(2026, 11, 3))
