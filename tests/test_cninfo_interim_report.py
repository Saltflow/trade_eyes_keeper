from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta, timezone

from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import (
    CninfoAnnualReportProvider,
    CninfoInterimReportProvider,
    PointInTimeFundamentalStore,
)


class _Response:
    def __init__(self, payload=None, content: bytes = b"%PDF-test"):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    def __init__(self, provider: CninfoInterimReportProvider, announcements: list[dict]):
        self.provider = provider
        self.announcements = announcements
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[str] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        if url == self.provider.SEARCH_URL:
            return _Response([{"code": "000958", "orgId": "gssz0000958"}])
        return _Response({"announcements": self.announcements})

    def get(self, url: str, **kwargs):
        self.get_calls.append(url)
        return _Response()


def _announcement(title: str, suffix: str) -> dict:
    published = datetime(
        2026,
        8,
        28,
        19,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    return {
        "announcementTitle": title,
        "announcementTime": int(published.timestamp() * 1000),
        "adjunctUrl": f"finalpage/2026-08-28/{suffix}.PDF",
    }


def test_interim_provider_uses_full_report_and_keeps_only_parent_equity(
    monkeypatch,
    tmp_path,
):
    provider = CninfoInterimReportProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    )
    http = _Http(
        provider,
        [
            _announcement("2026年半年度报告摘要", "summary"),
            _announcement("2026年半年度报告", "full"),
            _announcement("2025年年度报告", "annual"),
        ],
    )
    provider.http = http
    monkeypatch.setattr(
        provider,
        "_parse_pdf",
        lambda content: (
            {
                "parent_equity": 500_000_000.0,
                "net_income_parent": 99_000_000.0,
                "revenue": 1_200_000_000.0,
            },
            "test-parser",
        ),
    )

    result = provider.fetch("000958", date(2026, 1, 1), date(2026, 9, 18))

    assert len(result.statements) == 1
    statement = result.statements[0]
    assert statement.period_end == date(2026, 6, 30)
    assert statement.published_at == date(2026, 8, 28)
    assert statement.source == "cninfo_interim_report"
    assert statement.parent_equity == 500_000_000.0
    assert statement.net_income_parent is None
    assert statement.revenue is None
    assert statement.source_url.endswith("/full.PDF")
    assert "interim_pdf_carries_parent_equity_only" in statement.diagnostics
    announcement_call = http.post_calls[-1][1]["data"]
    assert announcement_call["category"] == ""
    assert announcement_call["seDate"] == "2026-01-01~2026-09-18"

    cached = provider.fetch("000958", date(2026, 1, 1), date(2026, 9, 18))
    assert len(cached.statements) == 1
    assert len(http.get_calls) == 1
    assert any(item["status"] == "cached" for item in cached.attempts)


def test_interim_provider_rejects_missing_parent_equity(monkeypatch, tmp_path):
    provider = CninfoInterimReportProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    )
    provider.http = _Http(provider, [_announcement("2026年半年度报告", "full")])
    monkeypatch.setattr(
        provider,
        "_parse_pdf",
        lambda content: ({"net_income_parent": 99_000_000.0}, "test-parser"),
    )

    result = provider.fetch("000958", date(2026, 1, 1), date(2026, 9, 18))

    assert result.statements == []
    assert any(
        item["status"] == "failed" and "parent_equity" in item["reason"]
        for item in result.attempts
    )


def test_store_corrects_disclosure_date_for_interim_composite_source(tmp_path):
    store = PointInTimeFundamentalStore(tmp_path)
    store.upsert(
        "000958",
        [
            FinancialStatementSnapshot(
                period_end=date(2026, 6, 30),
                published_at=date(2026, 8, 29),
                period_type="quarter",
                is_cumulative=True,
                currency="CNY",
                source="baostock_profit+cninfo_interim_report",
                source_url=(
                    "https://static.cninfo.com.cn/finalpage/2026-08-28/"
                    "1225522363.PDF"
                ),
                total_shares=1.0,
                net_income_parent=1.0,
                parent_equity=2.0,
            )
        ],
    )

    restored = store.read_all("000958")

    assert restored[0].published_at == date(2026, 8, 28)


def test_cninfo_parser_reads_parent_equity_from_interim_summary_without_unit(
    monkeypatch,
):
    class _Page:
        def extract_text(self):
            return (
                "主要会计数据和财务指标 单位：元 "
                "归属于上市公司股东的净资产 "
                "119,423,494,333.79 118,717,605,893.86"
            )

    class _Pdf:
        def __init__(self):
            self.pages = [_Page()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=lambda _: _Pdf()))

    values, parser = CninfoAnnualReportProvider._parse_pdf(b"test")

    assert parser == "pdfplumber"
    assert values["parent_equity"] == 119_423_494_333.79
