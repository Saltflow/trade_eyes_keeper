from __future__ import annotations

import json
import re
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.market_history import (
    CorporateAction,
    MarketHistoryProvider,
    PointInTimeMarketStore,
    PriceHistoryBundle,
    YahooMarketHistoryProvider,
)
from src.data.point_in_time_backfill import PointInTimeBackfillService
from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import (
    FUNDAMENTAL_FEATURE_NAMES,
    CninfoAnnualReportProvider,
    FundamentalFeaturePanelBuilder,
    HkexStatementProvider,
    PointInTimeFundamentalStore,
    StatementFetchResult,
    adjust_statement_shares,
    merge_statement_sources,
)


def _bundle(
    code: str = "601088",
    *,
    dates: tuple[str, ...] = ("2026-03-29", "2026-03-30"),
    raw: tuple[float, ...] = (10.0, 10.0),
    qfq: tuple[float, ...] = (5.0, 5.0),
    actions: list[CorporateAction] | None = None,
) -> PriceHistoryBundle:
    factor = np.asarray(qfq) / np.asarray(raw)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "raw_open": raw,
            "raw_high": raw,
            "raw_low": raw,
            "raw_close": raw,
            "qfq_open": qfq,
            "qfq_high": qfq,
            "qfq_low": qfq,
            "qfq_close": qfq,
            "qfq_factor": factor,
            "volume": [100.0] * len(raw),
            "tradable": [True] * len(raw),
        }
    )
    return PriceHistoryBundle(
        code=code,
        prices=frame,
        actions=actions or [],
        source="test",
    ).validate()


def _statements() -> list[FinancialStatementSnapshot]:
    periods = (
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    )
    publications = (
        date(2024, 4, 30),
        date(2024, 8, 30),
        date(2024, 10, 30),
        date(2025, 3, 30),
        date(2025, 4, 30),
        date(2025, 8, 30),
        date(2025, 10, 30),
        date(2026, 3, 30),
    )
    revenues = (70, 80, 90, 100, 110, 120, 130, 140)
    incomes = (4, 5, 6, 7, 8, 9, 10, 13)
    equities = (360, 370, 380, 400, 420, 440, 460, 500)
    return [
        FinancialStatementSnapshot(
            period_end=period,
            published_at=published,
            period_type="quarter",
            is_cumulative=False,
            currency="CNY",
            source="test_filing",
            common_shares_outstanding=100.0,
            parent_equity=float(equity),
            revenue=float(revenue),
            net_income_parent=float(income),
        )
        for period, published, revenue, income, equity in zip(
            periods, publications, revenues, incomes, equities
        )
    ]


def test_bundle_enforces_raw_times_factor_identity():
    bundle = _bundle()
    assert bundle.prices.iloc[-1]["raw_close"] == 10.0
    assert bundle.prices.iloc[-1]["qfq_close"] == 5.0
    assert bundle.prices.iloc[-1]["qfq_factor"] == 0.5

    invalid = bundle.prices.copy()
    invalid.loc[0, "qfq_factor"] = 0.6
    with pytest.raises(ValueError, match="qfq_close"):
        PriceHistoryBundle(code="x", prices=invalid).validate()


class _Response:
    url = "https://example.test/chart"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "chart": {
                "result": [
                    {
                        "timestamp": [1774828800],
                        "meta": {"currency": "USD"},
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100.0],
                                    "high": [102.0],
                                    "low": [98.0],
                                    "close": [100.0],
                                    "volume": [1000],
                                }
                            ],
                            "adjclose": [{"adjclose": [50.0]}],
                        },
                        "events": {
                            "dividends": {"d": {"date": 1774828800, "amount": 1.5}},
                            "splits": {
                                "s": {
                                    "date": 1774828800,
                                    "numerator": 2,
                                    "denominator": 1,
                                }
                            },
                        },
                    }
                ]
            }
        }


class _Http:
    def get(self, *_args, **_kwargs):
        return _Response()


def test_yahoo_keeps_raw_price_and_parses_actions():
    provider = YahooMarketHistoryProvider({}, http=_Http())
    bundle = provider.fetch("GOOG", date(2026, 3, 1), date(2026, 3, 31))
    row = bundle.prices.iloc[0]
    assert row["raw_close"] == 100.0
    assert row["qfq_close"] == 50.0
    assert row["qfq_factor"] == 0.5
    assert {item.action_type for item in bundle.actions} == {
        "cash_dividend",
        "split",
    }
    split = next(item for item in bundle.actions if item.action_type == "split")
    assert split.share_multiplier == 2.0


class _StaticMarketProvider:
    def __init__(self, bundle=None, error=None):
        self.bundle = bundle
        self.error = error
        self.calls = []

    def fetch(self, code, start, end):
        self.calls.append((code, start, end))
        if self.error is not None:
            raise self.error
        return self.bundle


def test_a_share_partial_market_history_selects_earliest_free_source():
    start = date(2020, 1, 1)
    end = date(2026, 1, 1)
    baostock = _bundle(
        "510300",
        dates=("2025-06-01", "2025-06-02"),
    )
    baostock.source = "baostock"
    yahoo = _bundle(
        "510300",
        dates=("2020-01-02", "2020-01-03"),
    )
    yahoo.source = "yahoo_chart"
    primary = _StaticMarketProvider(baostock)
    fallback = _StaticMarketProvider(yahoo)
    provider = MarketHistoryProvider(
        {},
        baostock_provider=primary,
        yahoo_provider=fallback,
    )

    selected = provider.fetch("510300", start, end)

    assert selected.source == "yahoo_chart"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
    assert any(
        item.startswith("coverage_selected:yahoo_chart")
        for item in selected.diagnostics
    )


def test_a_share_complete_primary_history_skips_fallback():
    start = date(2020, 1, 1)
    primary = _StaticMarketProvider(
        _bundle(
            "601398",
            dates=("2020-01-02", "2020-01-03"),
        )
    )
    fallback = _StaticMarketProvider(error=AssertionError("must not fetch"))
    provider = MarketHistoryProvider(
        {},
        baostock_provider=primary,
        yahoo_provider=fallback,
    )

    selected = provider.fetch("601398", start, date(2026, 1, 1))

    assert selected is primary.bundle
    assert fallback.calls == []


def test_a_share_partial_history_survives_fallback_failure():
    start = date(2020, 1, 1)
    primary = _StaticMarketProvider(
        _bundle(
            "510300",
            dates=("2025-06-01", "2025-06-02"),
        )
    )
    fallback = _StaticMarketProvider(error=RuntimeError("rate limited"))
    provider = MarketHistoryProvider(
        {},
        baostock_provider=primary,
        yahoo_provider=fallback,
    )

    selected = provider.fetch("510300", start, date(2026, 1, 1))

    assert selected is primary.bundle
    assert any(
        item.startswith("partial_coverage_fallback_failed:")
        for item in selected.diagnostics
    )


class _AnnouncementResponse:
    def __init__(self, announcements=None):
        self.announcements = announcements or []

    def raise_for_status(self):
        return None

    def json(self):
        return {"announcements": self.announcements}


class _AnnouncementHttp:
    def __init__(self, announcements=None):
        self.announcements = announcements or []
        self.calls = []

    def post(self, *_args, **kwargs):
        self.calls.append(kwargs)
        return _AnnouncementResponse(self.announcements)


def test_cninfo_uses_exchange_specific_announcement_parameters():
    http = _AnnouncementHttp()
    provider = CninfoAnnualReportProvider({}, http=http)

    provider._annual_announcements(
        "601398", "sse-org", date(2024, 1, 1), date(2026, 1, 1)
    )
    provider._annual_announcements(
        "000333", "szse-org", date(2024, 1, 1), date(2026, 1, 1)
    )

    assert http.calls[0]["data"]["column"] == "sse"
    assert http.calls[0]["data"]["plate"] == "sh"
    assert http.calls[1]["data"]["column"] == "szse"
    assert http.calls[1]["data"]["plate"] == "sz"


def test_cninfo_accepts_both_annual_report_title_conventions():
    http = _AnnouncementHttp(
        [
            {
                "announcementTitle": "2025\u5e74\u5ea6\u62a5\u544a",
                "announcementTime": 1774627200000,
                "adjunctUrl": "finalpage/2026/a.pdf",
            },
            {
                "announcementTitle": ("2025\u5e74\u5ea6\u62a5\u544a\u6458\u8981"),
                "announcementTime": 1774627200000,
                "adjunctUrl": "finalpage/2026/summary.pdf",
            },
            {
                "announcementTitle": "2024\u5e74\u5e74\u5ea6\u62a5\u544a",
                "announcementTime": 1743177600000,
                "adjunctUrl": "finalpage/2025/a.pdf",
            },
        ]
    )
    provider = CninfoAnnualReportProvider({}, http=http)

    selected = provider._annual_announcements(
        "601398", "sse-org", date(2024, 1, 1), date(2026, 8, 17)
    )

    assert set(selected) == {2024, 2025}
    assert selected[2025]["adjunctUrl"].endswith("/a.pdf")


def test_cninfo_pdf_parse_cache_is_url_bound_and_auditable(tmp_path):
    provider = CninfoAnnualReportProvider(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    )
    url = "https://static.cninfo.com.cn/finalpage/2026/report.pdf"

    output = provider._write_pdf_cache(
        url,
        b"real-pdf-bytes",
        {"revenue": 838_270_000_000.0},
        "pdfplumber",
    )
    cached = provider._read_pdf_cache(url)

    assert output.exists()
    assert cached is not None
    assert cached["contract"] == "cninfo-pdf-parse-5"
    assert cached["source_url"] == url
    assert cached["values"]["revenue"] == 838_270_000_000.0
    assert len(cached["content_sha256"]) == 64
    assert provider._read_pdf_cache(url + "?revision=2") is None


def test_cninfo_parses_bank_tables_reported_in_millions(monkeypatch):
    import sys
    from types import SimpleNamespace

    page_texts = [
        (
            "\u5168\u5e74\u7ecf\u8425\u6210\u679c"
            "\uff08\u4eba\u6c11\u5e01\u767e\u4e07\u5143\uff09"
            "\u8425\u4e1a\u6536\u5165838,270821,803"
            "\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c"
            "\u7684\u51c0\u5229\u6da6368,562365,863"
            "\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e"
            "\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c\u7684"
            "\u51c0\u5229\u6da6\uff082\uff09368,126364,277"
            "\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1"
            "\u6d41\u91cf\u51c0\u989d1,890,530579,194"
        ),
        (
            "\u4e8e\u62a5\u544a\u671f\u672b"
            "\uff08\u4eba\u6c11\u5e01\u767e\u4e07\u5143\uff09"
            "\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c"
            "\u7684\u6743\u76ca4,244,2593,969,841"
        ),
        (
            "\u5408\u5e76\u73b0\u91d1\u6d41\u91cf\u8868"
            "\uff08\u4eba\u6c11\u5e01\u767e\u4e07\u5143\uff09"
            "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62"
            "\u8d44\u4ea7\u548c\u5176\u4ed6\u957f\u671f\u8d44\u4ea7"
            "\u652f\u4ed8\u7684\u73b0\u91d1(38,239)(35,585)"
        ),
    ]

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Pdf:
        def __init__(self):
            self.pages = [Page(text) for text in page_texts]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )

    values, parser = CninfoAnnualReportProvider._parse_pdf(b"pdf")

    assert parser == "pdfplumber"
    assert values["revenue"] == 838_270_000_000.0
    assert values["net_income_parent"] == 368_562_000_000.0
    assert values["adjusted_net_income_parent"] == 368_126_000_000.0
    assert values["operating_cash_flow"] == 1_890_530_000_000.0
    assert values["parent_equity"] == 4_244_259_000_000.0
    assert values["capital_expenditures"] == 38_239_000_000.0


def test_cninfo_parses_real_cross_column_layout_variants(monkeypatch):
    import sys
    from types import SimpleNamespace

    current_text = [""]

    class Page:
        def extract_text(self):
            return current_text[0]

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )
    cases = [
        (
            "\u5355\u4f4d\uff1a\u5143"
            "\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u6240\u6709\u8005"
            "\u6743\u76ca118,717,605,893.86\uff08\u6216\u80a1\u4e1c"
            "\u6743\u76ca\uff09\u5408\u8ba1"
            "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62"
            "\u8d44\u4ea7\u548c\u5176\u4ed6\u957f\u671f\u8d44\u4ea7"
            "\u652f93,683,638,145.1190,708,324,096.213.28"
            "\u4ed8\u7684\u73b0\u91d1",
            118_717_605_893.86,
            93_683_638_145.11,
        ),
        (
            "\u4eba\u6c11\u5e01\u767e\u4e07\u5143"
            "\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c\u7684"
            "\u51c0\u5229\u6da6150,181148,391"
            "\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e"
            "\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c\u7684"
            "\u51c0\u5229\u6da6150,007148,011"
            "\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c"
            "\u6743\u76ca\u5408\u8ba11,272,8751,226,014"
            "\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u548c\u5176\u4ed6"
            "\u8d44\u4ea7\u6240\u652f\u4ed8\u7684\u73b0\u91d1"
            "(28,141)(34,930)",
            1_272_875_000_000.0,
            28_141_000_000.0,
        ),
    ]
    for text, equity, capex in cases:
        current_text[0] = text
        values, _ = CninfoAnnualReportProvider._parse_pdf(b"pdf")
        assert values["parent_equity"] == equity
        assert values["capital_expenditures"] == capex
    assert values["net_income_parent"] == 150_181_000_000.0
    assert values["adjusted_net_income_parent"] == 150_007_000_000.0


def test_cninfo_parses_split_adjusted_profit_label(monkeypatch):
    import sys
    from types import SimpleNamespace

    text = (
        "归属于上市公司股东的扣除非经常性损"
        "41,267,23335,741,41815.46%32,974,908"
        "益的净利润（千元）"
    )

    class Page:
        def extract_text(self):
            return text

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )
    values, _ = CninfoAnnualReportProvider._parse_pdf(b"pdf")

    assert values["adjusted_net_income_parent"] == 41_267_233_000.0


def test_cninfo_inherits_units_only_within_financial_statements(monkeypatch):
    import sys
    from types import SimpleNamespace

    pages = [
        "合并资产负债表单位：元",
        ("归属于母公司所有者权益（或142,468,648,190137,414,784,587股东权益）合计"),
        (
            "合并现金流量表单位：元"
            "购建固定资产、无形资产和其58,326,323,386"
            "63,653,239,054他长期资产支付的现金"
        ),
    ]

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Pdf:
        def __init__(self):
            self.pages = [Page(text) for text in pages]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )
    values, _ = CninfoAnnualReportProvider._parse_pdf(b"pdf")

    assert values["parent_equity"] == 142_468_648_190.0
    assert values["capital_expenditures"] == 58_326_323_386.0


def test_cninfo_inherits_bank_million_unit_on_continuation_page(monkeypatch):
    import sys
    from types import SimpleNamespace

    pages = [
        ("合并资产负债表（除特别注明外，货币单位均以人民币百万元列示）"),
        "归属于本行股东权益合计1,272,8751,226,014",
    ]

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Pdf:
        def __init__(self):
            self.pages = [Page(text) for text in pages]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )
    values, _ = CninfoAnnualReportProvider._parse_pdf(b"pdf")

    assert values["parent_equity"] == 1_272_875_000_000.0


def test_cninfo_parses_amount_unit_phrase_and_accounting_parentheses(monkeypatch):
    import sys
    from types import SimpleNamespace

    text = (
        "合并及公司现金流量表"
        "(除特别注明外，金额单位为人民币千元)"
        "购建固定资产、无形资产和其他长期资产支付的现金"
        "(11,141,889)(7,839,636)"
    )

    class Page:
        def extract_text(self):
            return text

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )
    values, _ = CninfoAnnualReportProvider._parse_pdf(b"pdf")

    assert values["capital_expenditures"] == 11_141_889_000.0


class _StatementProvider:
    def __init__(self, statements=None):
        self.statements = statements or []
        self.calls = []

    def fetch(self, code, start, end):
        self.calls.append((code, start, end))
        return StatementFetchResult(statements=self.statements, attempts=[])


def test_hkex_provider_uses_title_search_release_date_and_parses_report_values(
    monkeypatch,
):
    class Response:
        def __init__(self, *, payload=None, text="", content=b"", url="https://test"):
            self._payload = payload
            self.text = text
            self.content = content
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Http:
        def __init__(self):
            self.calls = []

        def get(self, url, **_kwargs):
            self.calls.append(url)
            if "activestock" in url:
                return Response(payload=[{"c": "00700", "i": 7609}])
            return Response(content=b"official-pdf", url=url)

        def post(self, url, **_kwargs):
            self.calls.append(url)
            if "titlesearch" in url:
                return Response(
                    text=(
                        '<tr><td class="release-time">15/04/2025</td>'
                        '<td><div class="headline">Annual Results</div>'
                        '<div class="doc-link"><a href="/report.pdf">PDF</a></div>'
                        "</td></tr>"
                    ),
                    url="https://www1.hkexnews.hk/search/titlesearch.xhtml",
                )
            raise AssertionError(f"unexpected POST: {url}")

    report_text = """
        Consolidated statement of profit or loss
        RMB'000
        Year ended 31 December 2024
        Revenue 373,710,000
        Profit attributable to owners of the Company 19,272,000
        Consolidated statement of financial position
        Equity attributable to owners 120,000,000
        Consolidated statement of cash flows
        Net cash generated from operating activities 50,000,000
        Purchase of property, plant and equipment (8,000,000)
        Basic earnings per share 1.20
        Diluted earnings per share 1.18
    """
    monkeypatch.setattr(
        HkexStatementProvider,
        "_extract_pdf_text",
        classmethod(lambda _cls, _content: (report_text, "test_pdf")),
    )
    provider = HkexStatementProvider(http=Http())
    result = provider.fetch("700", date(2024, 1, 1), date(2025, 12, 31))

    assert len(result.statements) == 1
    statement = result.statements[0]
    assert statement.period_end == date(2024, 12, 31)
    assert statement.published_at == date(2025, 4, 15)
    assert statement.currency == "CNY"
    assert statement.revenue == 373_710_000_000.0
    assert statement.free_cash_flow == 42_000_000_000.0
    assert statement.source == "hkex_results_pdf"
    assert statement.source_url == "https://www1.hkexnews.hk/report.pdf"


def test_hkex_pdf_extraction_releases_page_caches(monkeypatch):
    import sys
    from types import SimpleNamespace

    class Page:
        def __init__(self, text):
            self.text = text
            self.flushes = 0

        def extract_text(self):
            return self.text

        def flush_cache(self):
            self.flushes += 1

    pages = [Page("first"), Page("second")]

    class Pdf:
        def __init__(self):
            self.pages = pages
            self.flushes = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def flush_cache(self):
            self.flushes += 1

    pdf = Pdf()
    monkeypatch.setitem(
        sys.modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: pdf),
    )

    text, parser = HkexStatementProvider._extract_pdf_text(b"pdf")

    assert parser == "pdfplumber"
    assert text == "first\nsecond"
    assert [page.flushes for page in pages] == [1, 1]
    assert pdf.flushes == 1


def test_hkex_provider_accepts_only_known_report_kind_configuration():
    provider = HkexStatementProvider(
        {"point_in_time_data": {"hkex_report_kinds": ["year"]}}
    )

    assert provider.report_kinds == frozenset({"year"})
    with pytest.raises(ValueError, match="hkex_report_kinds"):
        HkexStatementProvider(
            {"point_in_time_data": {"hkex_report_kinds": ["quarter"]}}
        )


def test_hkex_parser_uses_audited_statement_pages_not_financial_summary(monkeypatch):
    # The first page mirrors the common HKEX five-year financial-summary
    # layout: its values are deliberately plausible-looking but wrong for the
    # 2025 audited period.  Page boundaries must keep it out of the parser.
    report_text = "\f".join(
        [
            """
            Financial Summary HK$'000
            Year ended 31 December 2021 2022 2023 2024 2025
            Revenues 560,118 554,552 609,015 660,257 751,766
            """,
            """
            Consolidated Statement of Comprehensive Income
            RMB'Million
            Year ended 31 December 2025
            Revenue 751,766 660,257
            Profit attributable to owners of the Company 224,842 194,073
            Basic earnings per share 24.749 21.343
            """,
            """
            Consolidated Statement of Financial Position
            RMB'Million
            As at 31 December 2025
            Equity attributable to equity holders of the Company 1,154,152 1,015,386
            """,
            """
            Consolidated Statement of Cash Flows
            RMB'Million
            Year ended 31 December 2025
            Net cash flows generated from operating activities 303,052 258,521
            Purchase of/prepayments for property, plant and equipment (87,482) (62,927)
            """,
            """
            13 Earnings per Share
            Weighted average number of ordinary shares for the calculation of
            diluted EPS (million shares) 9,244 9,408
            """,
        ]
    )
    monkeypatch.setattr(
        HkexStatementProvider,
        "_extract_pdf_text",
        classmethod(lambda _cls, _content: (report_text, "test_pdf")),
    )

    values, _parser, period_end, currency = HkexStatementProvider._parse_pdf(b"pdf")

    assert period_end == date(2025, 12, 31)
    assert currency == "CNY"
    assert values["revenue"] == 751_766_000_000.0
    assert values["net_income_parent"] == 224_842_000_000.0
    assert values["parent_equity"] == 1_154_152_000_000.0
    assert values["basic_eps"] == 24.749
    assert values["diluted_average_shares"] == 9_244_000_000.0
    assert values["operating_cash_flow"] == 303_052_000_000.0
    assert values["capital_expenditures"] == 87_482_000_000.0
    assert values["free_cash_flow"] == 215_570_000_000.0


def test_hkex_parser_rejects_impossible_summary_value_pairing(monkeypatch):
    report_text = """
        Consolidated statement of profit or loss
        RMB'Million
        Year ended 31 December 2025
        Revenue 3
        Profit attributable to owners of the Company 224,842
    """
    monkeypatch.setattr(
        HkexStatementProvider,
        "_extract_pdf_text",
        classmethod(lambda _cls, _content: (report_text, "test_pdf")),
    )

    with pytest.raises(ValueError, match="internally inconsistent"):
        HkexStatementProvider._parse_pdf(b"pdf")


def test_hk_backfill_stores_only_hkex_dated_statements(tmp_path):
    hk_statement = FinancialStatementSnapshot(
        period_end=date(2025, 12, 31),
        published_at=date(2026, 3, 20),
        period_type="year",
        is_cumulative=True,
        currency="HKD",
        accounting_standard="HKFRS",
        source="hkex_results_pdf",
        revenue=1_000.0,
        net_income_parent=100.0,
    )
    hkex = _StatementProvider([hk_statement])
    service = PointInTimeBackfillService(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        market_provider=_StaticMarketProvider(_bundle("00700")),
        hkex_statements=hkex,
        market_store=PointInTimeMarketStore(tmp_path),
        fundamental_store=PointInTimeFundamentalStore(tmp_path),
    )

    report = service.run(["00700"], evaluation_date=date(2026, 8, 17))

    assert report["statement_success"] == 1
    assert hkex.calls and hkex.calls[0][0] == "00700"
    stored = PointInTimeFundamentalStore(tmp_path).as_of("00700", date(2026, 8, 17))
    assert len(stored) == 1
    assert stored[0].source == "hkex_results_pdf"


def test_market_only_fx_series_is_stored_without_company_statement_fetch(tmp_path):
    market = _StaticMarketProvider(_bundle("CNYHKD=X"))
    service = PointInTimeBackfillService(
        {
            "point_in_time_data": {
                "output_dir": str(tmp_path),
                "fx_symbols": ["CNYHKD=X"],
            }
        },
        market_provider=market,
        market_store=PointInTimeMarketStore(tmp_path),
        fundamental_store=PointInTimeFundamentalStore(tmp_path),
    )

    report = service.run([], evaluation_date=date(2026, 8, 17))

    assert report["instrument_count"] == 1
    assert report["instruments"][0]["instrument_type"] == "fx"
    assert report["instruments"][0]["statements"]["status"] == "not_applicable"
    assert PointInTimeMarketStore(tmp_path).read("CNYHKD=X") is not None


def test_market_store_preserves_yahoo_quote_currency(tmp_path):
    bundle = _bundle("00700")
    bundle.currency = "HKD"
    store = PointInTimeMarketStore(tmp_path)
    store.write(bundle)

    restored = store.read("00700")

    assert restored is not None
    assert restored.currency == "HKD"


def test_sse_backfill_adds_recent_cninfo_complete_reports(tmp_path):
    base = _StatementProvider([_statements()[0]])
    sse = _StatementProvider()
    cninfo = _StatementProvider()
    market = _StaticMarketProvider(_bundle("601398"))
    service = PointInTimeBackfillService(
        {
            "point_in_time_data": {
                "output_dir": str(tmp_path),
                "official_pdf_recent_years": 3,
            }
        },
        market_provider=market,
        a_share_statements=base,
        sse_statements=sse,
        cninfo_statements=cninfo,
        market_store=PointInTimeMarketStore(tmp_path),
        fundamental_store=PointInTimeFundamentalStore(tmp_path),
    )

    service.run(["601398"], evaluation_date=date(2026, 8, 17))

    assert len(sse.calls) == 1
    assert len(cninfo.calls) == 1
    assert cninfo.calls[0][1] == date(2024, 1, 1)


def test_store_rejects_undated_rows_and_selects_latest_known_revision(tmp_path):
    store = PointInTimeFundamentalStore(tmp_path)
    original = _statements()[0]
    revision = original.copy(deep=True)
    revision.published_at = date(2024, 5, 15)
    revision.net_income_parent = 9.0
    undated = original.copy(deep=True)
    undated.published_at = None
    store.upsert("601088", [original, revision, undated])

    before = store.as_of("601088", date(2024, 5, 1))
    after = store.as_of("601088", date(2024, 5, 16))
    assert len(store.read_all("601088")) == 2
    assert before[0].net_income_parent == 4.0
    assert after[0].net_income_parent == 9.0
    payload = json.loads(store._path("601088").read_text(encoding="utf-8"))
    assert payload["contract"] == "point-in-time-statements-1"


def test_source_merge_unions_complementary_fields_and_prefers_official(tmp_path):
    disclosed = date(2025, 4, 26)
    baostock = FinancialStatementSnapshot(
        period_end=date(2024, 12, 31),
        published_at=disclosed,
        period_type="year",
        is_cumulative=True,
        currency="CNY",
        source="baostock_profit",
        total_shares=5_383_418_520.0,
        revenue=5_744_509_404.79,
        net_income_parent=1_100_000_000.0,
    )
    cninfo = FinancialStatementSnapshot(
        period_end=date(2024, 12, 31),
        published_at=disclosed,
        period_type="year",
        is_cumulative=True,
        currency="CNY",
        source="cninfo_annual_report",
        source_url=("https://static.cninfo.com.cn/finalpage/2025-04-26/1223307084.PDF"),
        revenue=5_744_509_404.79,
        net_income_parent=1_043_960_186.44,
        adjusted_net_income_parent=832_156_137.45,
        operating_cash_flow=2_341_366_193.44,
        capital_expenditures=523_302_292.80,
        free_cash_flow=1_818_063_900.64,
    )

    merged = merge_statement_sources([baostock], [cninfo])
    composite = next(
        item for item in merged if item.source == "baostock_profit+cninfo_annual_report"
    )
    assert composite.total_shares == 5_383_418_520.0
    assert composite.net_income_parent == 1_043_960_186.44
    assert composite.adjusted_net_income_parent == 832_156_137.45
    assert composite.free_cash_flow == 1_818_063_900.64
    assert any(
        item.startswith("official_value_conflict:net_income_parent:")
        for item in composite.diagnostics
    )

    store = PointInTimeFundamentalStore(tmp_path)
    store.upsert("000958", merged)
    selected = store.as_of("000958", disclosed)
    assert len(selected) == 1
    assert selected[0].total_shares == 5_383_418_520.0
    assert selected[0].adjusted_net_income_parent == 832_156_137.45


def test_feature_panel_uses_raw_close_and_never_reads_future_filing(tmp_path):
    store = PointInTimeFundamentalStore(tmp_path)
    store.upsert("601088", _statements())
    panel = FundamentalFeaturePanelBuilder(store).build(
        {"601088": _bundle()},
        dates=[date(2026, 3, 29), date(2026, 3, 30)],
    )
    pe_index = FUNDAMENTAL_FEATURE_NAMES.index("pe_ttm")
    assert panel.availability_mask[0, 0, pe_index]
    # Before the 2025 annual report is filed, the last four known quarters
    # earn 34.  The Q4 result becomes visible exactly on its filing date.
    assert panel.values[0, 0, pe_index] == pytest.approx(10.0 * 100.0 / 34.0)
    assert panel.availability_mask[1, 0, pe_index]
    # TTM income is 40; raw price 10 and shares 100 => PE 25.  Using the
    # qfq price of 5 would incorrectly produce 12.5.
    assert panel.values[1, 0, pe_index] == pytest.approx(25.0)


def test_share_changing_action_updates_denominator_without_rewriting_profit():
    statement = _statements()[-1]
    action = CorporateAction(
        code="601088",
        action_type="split",
        ex_date=date(2026, 2, 1),
        share_multiplier=2.0,
        source="test_action",
    )
    adjusted = adjust_statement_shares([statement], [action], date(2026, 3, 1))[0]
    assert adjusted.common_shares_outstanding == 200.0
    assert adjusted.net_income_parent == statement.net_income_parent
    assert "shares_adjusted_for_disclosed_actions:2" in adjusted.diagnostics


def test_unknown_adjustment_factor_does_not_guess_share_change():
    statement = _statements()[-1]
    action = CorporateAction(
        code="601088",
        action_type="adjustment_factor_change",
        ex_date=date(2026, 2, 1),
        raw_adjustment_factor=0.8,
        source="test_factor",
    )
    adjusted = adjust_statement_shares([statement], [action], date(2026, 3, 1))[0]
    assert adjusted.common_shares_outstanding == 100.0


def test_backfill_summary_reports_field_coverage_across_all_equities():
    rows = [
        {
            "market_history": {
                "status": "success",
                "requested_window_coverage": "full",
            },
            "statements": {
                "status": "success",
                "field_availability": {
                    "revenue": {"available": True},
                    "free_cash_flow": {"available": True},
                },
            },
        },
        {
            "market_history": {
                "status": "success",
                "requested_window_coverage": "partial",
            },
            "statements": {"status": "missing"},
        },
        {
            "market_history": {
                "status": "success",
                "requested_window_coverage": "full",
            },
            "statements": {"status": "not_applicable"},
        },
    ]
    report = PointInTimeBackfillService._summary(
        date(2020, 1, 1),
        date(2026, 1, 1),
        rows,
    )

    assert report["statement_applicable"] == 2
    assert report["market_history_full_window"] == 2
    assert report["market_history_partial_window"] == 1
    assert report["statement_field_coverage"]["revenue"] == {
        "filled": 1,
        "total": 2,
        "fill_rate": 0.5,
    }
    assert report["statement_field_coverage"]["free_cash_flow"] == {
        "filled": 1,
        "total": 2,
        "fill_rate": 0.5,
    }


def test_cninfo_report_unit_requires_explicit_financial_statement_declaration():
    assert (
        CninfoAnnualReportProvider._declared_statement_unit(
            "财务附注中报表的单位为：千元"
        )
        == "千元"
    )
    assert (
        CninfoAnnualReportProvider._declared_statement_unit(
            "财务报表的单位为人民币万元"
        )
        == "万元"
    )
    assert CninfoAnnualReportProvider._declared_statement_unit("本页单位：万元") is None
    grouped_integer = re.search(
        CninfoAnnualReportProvider.MONEY,
        "97,359,768122,093,509",
    )
    grouped_decimal = re.search(
        CninfoAnnualReportProvider.MONEY,
        "5,744,509,404.796,076,000,000.00",
    )
    assert grouped_integer is not None
    assert grouped_integer.group() == "97,359,768"
    assert grouped_decimal is not None
    assert grouped_decimal.group() == "5,744,509,404.79"


def test_store_can_replace_legacy_rows_from_one_source(tmp_path):
    store = PointInTimeFundamentalStore(tmp_path)
    legacy = FinancialStatementSnapshot(
        period_end=date(2025, 3, 31),
        published_at=date(2026, 4, 30),
        period_type="quarter",
        is_cumulative=False,
        source="sec_companyfacts",
        revenue=100.0,
    )
    corrected = FinancialStatementSnapshot(
        period_end=date(2025, 3, 31),
        published_at=date(2025, 4, 25),
        period_type="quarter",
        is_cumulative=True,
        source="sec_companyfacts",
        revenue=100.0,
    )

    store.upsert("GOOG", [legacy])
    store.upsert(
        "GOOG",
        [corrected],
        replace_sources={"sec_companyfacts"},
    )

    rows = store.read_all("GOOG")
    assert len(rows) == 1
    assert rows[0].published_at == date(2025, 4, 25)
    assert rows[0].is_cumulative is True
