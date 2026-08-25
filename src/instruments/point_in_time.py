"""Point-in-time financial history and fundamental feature construction.

Only observations with a real disclosure date can enter this store.  Daily
valuation features are calculated with contemporaneous *unadjusted* prices;
forward-adjusted prices are intentionally absent from this API.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.data.market_history import (
    CorporateAction,
    PriceHistoryBundle,
    _result_rows,
    baostock_session,
    baostock_timeout_seconds,
)

from .calculations import derive_company_fundamentals
from .models import FinancialStatementSnapshot, MetricStatus, MetricValue

logger = logging.getLogger(__name__)


def _float(value: Any) -> float | None:
    if value in (None, "", "None", "null"):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _baostock_code(code: str) -> str:
    normalized = str(code).strip()
    prefix = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}.{normalized}"


@dataclass
class StatementFetchResult:
    statements: list[FinancialStatementSnapshot]
    attempts: list[dict[str, Any]]


class BaostockStatementProvider:
    """Fetch dated A-share profit snapshots from Baostock.

    Baostock exposes ``pubDate`` and ``statDate`` for every quarterly row.  Its
    profit feed supplies cumulative revenue/profit, ROE and total shares but
    not a parent-equity balance.  Average equity is therefore derived only
    when both profit and ROE are meaningful, and carries an explicit
    diagnostic instead of masquerading as a reported balance-sheet value.
    """

    def __init__(
        self,
        module: Any | None = None,
        config: dict | None = None,
    ):
        self._module = module
        self.socket_timeout_seconds = baostock_timeout_seconds(config)

    def _load(self):
        if self._module is None:
            try:
                import baostock as bs
            except ImportError as exc:
                raise RuntimeError(
                    "baostock is required for A-share statements"
                ) from exc
            self._module = bs
        return self._module

    def fetch(self, code: str, start: date, end: date) -> StatementFetchResult:
        bs = self._load()
        attempts: list[dict[str, Any]] = []
        statements: list[FinancialStatementSnapshot] = []
        with baostock_session(bs, self.socket_timeout_seconds):
            symbol = _baostock_code(code)
            for year in range(start.year - 1, end.year + 1):
                for quarter in range(1, 5):
                    result = bs.query_profit_data(symbol, year, quarter)
                    rows = _result_rows(result)
                    attempts.append(
                        {
                            "source": "baostock_profit",
                            "year": year,
                            "quarter": quarter,
                            "status": "success" if rows else "empty",
                        }
                    )
                    for row in rows:
                        snapshot = self._snapshot(row, quarter)
                        if snapshot is None:
                            continue
                        if snapshot.period_end > end:
                            continue
                        statements.append(snapshot)
        selected: dict[tuple[date, date, str], FinancialStatementSnapshot] = {}
        for item in statements:
            if item.published_at is None:
                continue
            selected[(item.period_end, item.published_at, item.source)] = item
        return StatementFetchResult(
            statements=sorted(
                selected.values(),
                key=lambda item: (item.period_end, item.published_at or date.max),
            ),
            attempts=attempts,
        )

    @staticmethod
    def _snapshot(
        row: dict[str, Any], quarter: int
    ) -> FinancialStatementSnapshot | None:
        period_end = _date(row.get("statDate"))
        published_at = _date(row.get("pubDate"))
        if period_end is None or published_at is None:
            return None
        net_income = _float(row.get("netProfit"))
        revenue = _float(row.get("MBRevenue"))
        total_shares = _float(row.get("totalShare"))
        roe = _float(row.get("roeAvg"))
        eps_ttm = _float(row.get("epsTTM"))
        roe_pct = None
        average_equity = None
        if roe is not None:
            # Baostock normally returns a decimal ratio.  Be tolerant of feeds
            # that serialize it as a percentage number.
            roe_ratio = roe / 100.0 if abs(roe) > 1.0 else roe
            roe_pct = roe_ratio * 100.0
            if net_income is not None and abs(roe_ratio) > 1e-9:
                average_equity = net_income / roe_ratio
        diagnostics = [
            "baostock_profit_values_are_cumulative_ytd",
            "parent_equity_not_available_in_baostock_profit_feed",
        ]
        if average_equity is not None:
            diagnostics.append(
                "average_equity_derived_from_net_profit_and_reported_roe"
            )
        if eps_ttm is not None:
            diagnostics.append("diluted_eps_contains_baostock_ttm_eps")
        return FinancialStatementSnapshot(
            period_end=period_end,
            published_at=published_at,
            period_type="year" if quarter == 4 else "quarter",
            is_cumulative=True,
            currency="CNY",
            accounting_standard="PRC-GAAP",
            source="baostock_profit",
            total_shares=total_shares,
            common_shares_outstanding=total_shares,
            average_parent_equity=average_equity,
            revenue=revenue,
            net_income_parent=net_income,
            diluted_eps=eps_ttm,
            reported_roe=roe_pct,
            diagnostics=diagnostics,
        )


class SseXbrlStatementProvider:
    """Fetch disclosure-dated statements from the free official SSE XBRL site."""

    QUERY_URL = "https://query.sse.com.cn/commonSoaQuery.do"
    LEGACY_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
    REFERER = "https://www.sse.com.cn/disclosure/listedinfo/listedcompanies/"
    SOURCE_URL = "https://www.sse.com.cn/disclosure/listedinfo/listedcompanies/"

    def __init__(
        self,
        config: dict | None = None,
        http: requests.Session | None = None,
    ):
        audit = ((config or {}).get("instrument_audit", {}) or {})
        self.timeout = int(audit.get("timeout_seconds", 20))
        self.user_agent = audit.get(
            "user_agent",
            "Mozilla/5.0 (compatible; trade-eyes-keeper instrument audit)",
        )
        self.http = http or requests.Session()

    def _query(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = self.http.get(
            url,
            params=params,
            headers={"User-Agent": self.user_agent, "Referer": self.REFERER},
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json().get("result", [])
        return result if isinstance(result, list) else []

    def fetch(self, code: str, start: date, end: date) -> StatementFetchResult:
        normalized = str(code).strip()
        attempts: list[dict[str, Any]] = []
        if not normalized.startswith(("5", "6", "9")):
            return StatementFetchResult(
                statements=[],
                attempts=[{
                    "source": "sse_xbrl",
                    "status": "not_applicable",
                    "reason": "not an SSE security",
                }],
            )
        disclosures = self._query(
            self.QUERY_URL,
            {
                "isPagination": "true",
                "sqlId": "COMMON_SSE_PL_XBRL_YJGL",
                "stockId": normalized,
                "startYear": str(start.year - 1),
                "endYear": str(end.year),
                "reportPeriodId": "5000,1000",
                "type": "inParams",
                "pageHelp.pageSize": "100",
                "pageHelp.pageNo": "1",
                "pageHelp.beginPage": "1",
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": "1",
            },
        )
        availability: dict[tuple[int, str], date] = {}
        for row in disclosures:
            published_at = _date(row.get("ACTUAL_DATE"))
            try:
                year = int(row.get("REPORT_YEAR"))
            except (TypeError, ValueError):
                continue
            period_id = str(row.get("REPORT_PERIOD_ID", ""))
            if (
                published_at is not None
                and published_at <= end
                and period_id in {"5000", "1000"}
            ):
                availability[(year, period_id)] = published_at
        attempts.append({
            "source": "sse_xbrl_disclosures",
            "status": "success" if availability else "empty",
            "rows": len(availability),
        })

        statements: dict[tuple[int, str], FinancialStatementSnapshot] = {}
        for year in sorted({key[0] for key in availability}):
            rows = self._query(
                self.QUERY_URL,
                {
                    "isPagination": "false",
                    "sqlId": "COMMON_SSE_PL_XBRL_YJGL_XQ",
                    "reportYear": str(year),
                    "stockId": normalized,
                },
            )
            attempts.append({
                "source": "sse_xbrl_performance",
                "year": year,
                "status": "success" if rows else "empty",
                "rows": len(rows),
            })
            for row in rows:
                period_id = str(row.get("REPORT_PERIOD_ID", ""))
                published_at = availability.get((year, period_id))
                if published_at is None:
                    continue
                period_end = (
                    date(year, 12, 31)
                    if period_id == "5000"
                    else date(year, 6, 30)
                )
                if not start <= period_end <= end:
                    continue
                net_income = _float(row.get("S2090_0040"))
                adjusted_income = _float(row.get("S2090_0050"))
                operating_cash_flow = _float(row.get("S2090_0060"))
                reported_roe = _float(row.get("S2090_0130"))
                average_equity = None
                if (
                    net_income is not None
                    and reported_roe is not None
                    and abs(reported_roe) > 1e-9
                ):
                    average_equity = net_income * 10_000.0 / (
                        reported_roe / 100.0
                    )
                statements[(year, period_id)] = FinancialStatementSnapshot(
                    period_end=period_end,
                    published_at=published_at,
                    period_type="year" if period_id == "5000" else "quarter",
                    is_cumulative=True,
                    currency="CNY",
                    accounting_standard="PRC-GAAP",
                    source="sse_xbrl_performance",
                    source_url=self.SOURCE_URL,
                    average_parent_equity=average_equity,
                    revenue=_float(row.get("S2020_0010")),
                    net_income_parent=(
                        net_income * 10_000.0 if net_income is not None else None
                    ),
                    adjusted_net_income_parent=(
                        adjusted_income * 10_000.0
                        if adjusted_income is not None
                        else None
                    ),
                    basic_eps=_float(row.get("S2090_0090")),
                    operating_cash_flow=(
                        operating_cash_flow * 10_000.0
                        if operating_cash_flow is not None
                        else None
                    ),
                    reported_roe=reported_roe,
                    diagnostics=[
                        "sse_xbrl_performance_revenue_is_cny",
                        "sse_xbrl_performance_profit_and_cash_flow_are_10k_cny",
                        "average_equity_derived_from_parent_profit_and_reported_roe",
                    ],
                )

        years = sorted({year for year, _ in availability})
        if years:
            year_list = ",".join(str(year) for year in years)
            for period_id in ("5000", "1000"):
                balance_rows = self._legacy_rows(
                    "COMMON_MAP_BALANCESHEET_C",
                    normalized,
                    year_list,
                    period_id,
                    attempts,
                )
                cash_rows = self._legacy_rows(
                    "COMMON_MAP_CASHFLOW_C",
                    normalized,
                    year_list,
                    period_id,
                    attempts,
                )
                by_year: dict[int, dict[str, Any]] = {}
                for row in balance_rows:
                    try:
                        year = int(row["REPORT_YEAR"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    by_year.setdefault(year, {})["parent_equity"] = _float(
                        row.get("S2010_0770")
                    )
                for row in cash_rows:
                    try:
                        year = int(row["REPORT_YEAR"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    by_year.setdefault(year, {}).update({
                        "operating_cash_flow": _float(row.get("S2030_0250")),
                        "capital_expenditures": _float(row.get("S2030_0320")),
                    })
                for year, values in by_year.items():
                    snapshot = statements.get((year, period_id))
                    if snapshot is None:
                        continue
                    for field, value in values.items():
                        if value is not None:
                            setattr(snapshot, field, value)
                    if (
                        snapshot.operating_cash_flow is not None
                        and snapshot.capital_expenditures is not None
                    ):
                        snapshot.free_cash_flow = (
                            snapshot.operating_cash_flow
                            - snapshot.capital_expenditures
                        )
                        snapshot.diagnostics.append(
                            "free_cash_flow=operating_cash_flow-capital_expenditures"
                        )
                    if any(value is not None for value in values.values()):
                        snapshot.source = (
                            "sse_xbrl_performance+sse_xbrl_full_statement"
                        )
                        snapshot.diagnostics.append(
                            "supplemented_from_free_sse_full_xbrl_statement"
                        )
        return StatementFetchResult(
            statements=sorted(
                statements.values(),
                key=lambda item: (item.period_end, item.published_at or date.max),
            ),
            attempts=attempts,
        )

    def _legacy_rows(
        self,
        sql_id: str,
        code: str,
        years: str,
        period_id: str,
        attempts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            rows = self._query(
                self.LEGACY_QUERY_URL,
                {
                    "isPagination": "false",
                    "sqlId": sql_id,
                    "type": "inParams",
                    "REPORT_YEAR": years,
                    "STOCK_ID": code,
                    "REPORT_PERIOD_ID": period_id,
                },
            )
            attempts.append({
                "source": "sse_xbrl_full_statement",
                "sql_id": sql_id,
                "period_id": period_id,
                "status": "success" if rows else "empty",
                "rows": len(rows),
            })
            return rows
        except Exception as exc:
            attempts.append({
                "source": "sse_xbrl_full_statement",
                "sql_id": sql_id,
                "period_id": period_id,
                "status": "failed",
                "reason": str(exc),
            })
            return []


class CninfoAnnualReportProvider:
    """Parse real annual-report PDFs from the official CNINFO disclosure site."""

    SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
    ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    STATIC_ROOT = "https://static.cninfo.com.cn/"
    REFERER = "https://www.cninfo.com.cn/new/index"
    PDF_CACHE_CONTRACT = "cninfo-pdf-parse-5"
    NUMBER = r"[-−]?[0-9][0-9,]*(?:\.[0-9]+)?"
    MONEY = (
        r"[-−]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
        r"(?:\.[0-9]{2})?"
    )

    def __init__(
        self,
        config: dict | None = None,
        http: requests.Session | None = None,
    ):
        audit = ((config or {}).get("instrument_audit", {}) or {})
        settings = ((config or {}).get("point_in_time_data", {}) or {})
        output_dir = Path(settings.get("output_dir", "data/point_in_time"))
        self.pdf_cache_dir = Path(
            settings.get(
                "cninfo_pdf_cache_dir",
                output_dir / "provider_cache" / "cninfo",
            )
        )
        self.timeout = int(audit.get("timeout_seconds", 20))
        self.user_agent = audit.get(
            "user_agent",
            "Mozilla/5.0 (compatible; trade-eyes-keeper instrument audit)",
        )
        self.http = http or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Referer": self.REFERER,
            "Origin": "https://www.cninfo.com.cn",
        }

    def _pdf_cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.pdf_cache_dir / f"{digest}.json"

    def _read_pdf_cache(self, url: str) -> dict[str, Any] | None:
        path = self._pdf_cache_path(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable CNINFO PDF cache: %s", path)
            return None
        if (
            payload.get("contract") != self.PDF_CACHE_CONTRACT
            or payload.get("source_url") != url
            or not isinstance(payload.get("values"), dict)
            or not payload["values"]
        ):
            logger.warning("Ignoring invalid CNINFO PDF cache: %s", path)
            return None
        return payload

    def _write_pdf_cache(
        self,
        url: str,
        content: bytes,
        values: dict[str, float],
        parser_name: str,
    ) -> Path:
        self.pdf_cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._pdf_cache_path(url)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "contract": self.PDF_CACHE_CONTRACT,
                    "source_url": url,
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "parser": parser_name,
                    "parsed_at": datetime.now().isoformat(),
                    "values": values,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def fetch(self, code: str, start: date, end: date) -> StatementFetchResult:
        normalized = str(code).strip()
        attempts: list[dict[str, Any]] = []
        if not normalized.startswith(("0", "2", "3", "5", "6", "9")):
            return StatementFetchResult(
                statements=[],
                attempts=[{
                    "source": "cninfo_annual_report",
                    "status": "not_applicable",
                    "reason": "not an A-share security",
                }],
            )
        org_id = self._org_id(normalized)
        announcements = self._annual_announcements(
            normalized, org_id, start, end
        )
        attempts.append({
            "source": "cninfo_annual_report_index",
            "status": "success" if announcements else "empty",
            "rows": len(announcements),
        })
        statements: list[FinancialStatementSnapshot] = []
        for year, announcement in sorted(announcements.items()):
            url = self.STATIC_ROOT + str(announcement["adjunctUrl"]).lstrip("/")
            try:
                cached = self._read_pdf_cache(url)
                cache_hit = cached is not None
                if cached is not None:
                    values = {
                        str(key): float(value)
                        for key, value in cached["values"].items()
                    }
                    parser_name = str(cached.get("parser", "unknown"))
                else:
                    response = self.http.get(
                        url,
                        headers=self._headers,
                        timeout=max(self.timeout, 60),
                    )
                    response.raise_for_status()
                    values, parser_name = self._parse_pdf(response.content)
                    self._write_pdf_cache(
                        url,
                        response.content,
                        values,
                        parser_name,
                    )
                published_at = datetime.fromtimestamp(
                    float(announcement["announcementTime"]) / 1000.0,
                    tz=timezone(timedelta(hours=8)),
                ).date()
                diagnostics = [
                    f"cninfo_pdf_parser:{parser_name}",
                    "values_parsed_from_official_annual_report_labels",
                ]
                if cache_hit:
                    diagnostics.append("cninfo_pdf_parse_cache_hit")
                if (
                    values.get("operating_cash_flow") is not None
                    and values.get("capital_expenditures") is not None
                ):
                    values["free_cash_flow"] = (
                        values["operating_cash_flow"]
                        - values["capital_expenditures"]
                    )
                    diagnostics.append(
                        "free_cash_flow=operating_cash_flow-capital_expenditures"
                    )
                statements.append(
                    FinancialStatementSnapshot(
                        period_end=date(year, 12, 31),
                        published_at=published_at,
                        period_type="year",
                        is_cumulative=True,
                        currency="CNY",
                        accounting_standard="PRC-GAAP",
                        source="cninfo_annual_report",
                        source_url=url,
                        diagnostics=diagnostics,
                        **values,
                    )
                )
                attempts.append({
                    "source": "cninfo_annual_report",
                    "year": year,
                    "status": "cached" if cache_hit else "success",
                    "fields": sorted(values),
                    "url": url,
                })
            except Exception as exc:
                attempts.append({
                    "source": "cninfo_annual_report",
                    "year": year,
                    "status": "failed",
                    "reason": str(exc),
                    "url": url,
                })
        return StatementFetchResult(statements=statements, attempts=attempts)

    def _org_id(self, code: str) -> str:
        response = self.http.post(
            self.SEARCH_URL,
            params={"keyWord": code, "maxNum": 10},
            headers=self._headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        for row in response.json():
            if str(row.get("code", "")) == code and row.get("orgId"):
                return str(row["orgId"])
        raise ValueError(f"CNINFO orgId not found for {code}")

    def _annual_announcements(
        self,
        code: str,
        org_id: str,
        start: date,
        end: date,
    ) -> dict[int, dict[str, Any]]:
        is_sse = str(code).startswith(("5", "6", "9"))
        response = self.http.post(
            self.ANNOUNCEMENT_URL,
            data={
                "pageNum": 1,
                "pageSize": 50,
                "column": "sse" if is_sse else "szse",
                "tabName": "fulltext",
                "plate": "sh" if is_sse else "sz",
                "stock": f"{code},{org_id}",
                "searchkey": "",
                "secid": "",
                "category": "category_ndbg_szsh;",
                "trade": "",
                "seDate": f"{start.isoformat()}~{end.isoformat()}",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            headers=self._headers,
            timeout=max(self.timeout, 45),
        )
        response.raise_for_status()
        selected: dict[int, dict[str, Any]] = {}
        for row in response.json().get("announcements", []) or []:
            title = re.sub(
                r"<[^>]+>", "", str(row.get("announcementTitle", ""))
            )
            compact = re.sub(r"\s+", "", title)
            match = re.search(r"(20\d{2})年?年度报告", compact)
            if not match or "摘要" in compact or not row.get("adjunctUrl"):
                continue
            year = int(match.group(1))
            period_end = date(year, 12, 31)
            published_at = datetime.fromtimestamp(
                float(row.get("announcementTime", 0)) / 1000.0,
                tz=timezone(timedelta(hours=8)),
            ).date()
            if not start <= period_end <= end or published_at > end:
                continue
            current = selected.get(year)
            if current is None or row.get("announcementTime", 0) > current.get(
                "announcementTime", 0
            ):
                selected[year] = row
        return selected

    @staticmethod
    def _declared_statement_unit(compact: str) -> str | None:
        units: set[str] = set()
        for pattern in (
            r"(?:财务附注中报表|财务报表)的单位为[:：]?"
            r"(?:人民币)?(元|千元|万元|百万元)",
            r"金额单位为(?:人民币)?(元|千元|万元|百万元)",
            r"货币单位均以(?:人民币)?(元|千元|万元|百万元)列示",
        ):
            units.update(re.findall(pattern, compact))
        return next(iter(units)) if len(units) == 1 else None

    @classmethod
    def _parse_pdf(cls, content: bytes) -> tuple[dict[str, float], str]:
        logging.getLogger("pdfminer").setLevel(logging.WARNING)
        pages: Iterable[str]
        try:
            import pdfplumber

            def iter_pdfplumber_pages() -> Iterable[str]:
                with pdfplumber.open(io.BytesIO(content)) as pdf:
                    for page in pdf.pages:
                        yield page.extract_text() or ""

            pages = iter_pdfplumber_pages()
            parser_name = "pdfplumber"
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = (page.extract_text() or "" for page in reader.pages)
            parser_name = "pypdf"
        values: dict[str, float] = {}
        patterns: dict[str, tuple[str, ...]] = {
            "revenue": (
                rf"营业(?:总)?收入[（(](?P<unit>元|千元|万元)[）)](?P<value>{cls.MONEY})",
            ),
            "net_income_parent": (
                rf"归属于上市公司股东的净利润[（(](?P<unit>元|千元|万元)[）)](?P<value>{cls.MONEY})",
            ),
            "adjusted_net_income_parent": (
                rf"归属于上市公司股东的扣除非经常性损益的净利润"
                rf"[（(](?P<unit>元|千元|万元)[）)](?P<value>{cls.MONEY})",
            ),
            "operating_cash_flow": (
                rf"经营活动产生的现金流量净额[（(](?P<unit>元|千元|万元)[）)](?P<value>{cls.MONEY})",
                rf"经营活动产生的现金流量净额(?P<value>{cls.MONEY})",
            ),
            "capital_expenditures": (
                rf"购建固定资产、无形资产和其他长期资产支付的现金(?P<value>{cls.MONEY})",
            ),
            "parent_equity": (
                rf"归属于上市公司股东的净资产[（(](?P<unit>元|千元|万元)[）)](?P<value>{cls.MONEY})",
                rf"归属于母公司所有者权益合计(?P<value>{cls.MONEY})",
            ),
            "basic_eps": (
                rf"基本每股收益[（(]元/?股[）)](?P<value>{cls.NUMBER})",
            ),
            "diluted_eps": (
                rf"稀释每股收益[（(]元/?股[）)](?P<value>{cls.NUMBER})",
            ),
            "reported_roe": (
                rf"加权平均净资产收益率(?P<value>{cls.NUMBER})%",
            ),
        }
        patterns.update({
            "cash_and_cash_equivalents": (
                rf"\u5e74\u672b\u73b0\u91d1\u53ca\u73b0\u91d1"
                rf"\u7b49\u4ef7\u7269\u4f59\u989d(?P<value>{cls.MONEY})",
            ),
            "restricted_cash": (
                rf"\u53d7\u9650\u5236\u7684\u8d27\u5e01\u8d44\u91d1"
                rf"(?P<value>{cls.MONEY})",
            ),
            "short_term_borrowings": (
                rf"\u77ed\u671f\u501f\u6b3e(?P<value>{cls.MONEY})",
            ),
            "current_portion_noncurrent_debt": (
                rf"\u4e00\u5e74\u5185\u5230\u671f\u7684\u975e\u6d41"
                rf"\u52a8\u8d1f\u503a(?:[一二三四五六七八九十]+"
                rf"[\uff08(]\d+"
                rf"[\uff09)])?(?P<value>{cls.MONEY})",
            ),
            "long_term_borrowings": (
                rf"\u957f\u671f\u501f\u6b3e(?P<value>{cls.MONEY})",
            ),
            "bonds_payable": (
                rf"\u5e94\u4ed8\u503a\u5238(?:[一二三四五六七八九十]+"
                rf"[\uff08(]"
                rf"\d+[\uff09)])?(?P<value>{cls.MONEY})",
            ),
            "lease_liabilities": (
                rf"\u79df\u8d41\u8d1f\u503a(?P<value>{cls.MONEY})",
            ),
            "interest_expense": (
                rf"\u5229\u606f\u8d39\u7528(?:[\uff08(]a[\uff09)])?"
                rf"(?P<value>[\uff08(]?{cls.MONEY}[\uff09)]?)",
            ),
            "income_tax_expense": (
                rf"\u51cf[:\uff1a]?\u6240\u5f97\u7a0e(?:[\uff08(]"
                rf"\u8d39\u7528[\uff09)]/\u8d37\u9879)?"
                rf"(?:[一二三四五六七八九十]+[\uff08(]\d+"
                rf"[\uff09)])?"
                rf"[\uff08(](?P<value>{cls.MONEY})[\uff09)]",
            ),
            "profit_before_tax": (
                rf"(?:\u4e09\u3001)?\u5229\u6da6\u603b\u989d"
                rf"(?P<value>{cls.MONEY})",
            ),
        })
        bank_table_prefix = (
            r"\u4eba\u6c11\u5e01"
            r"(?P<unit>\u767e\u4e07\u5143).*?"
        )
        bank_patterns: dict[str, tuple[str, ...]] = {
            "revenue": (
                bank_table_prefix
                + rf"\u8425\u4e1a\u6536\u5165(?P<value>{cls.MONEY})",
            ),
            "net_income_parent": (
                bank_table_prefix
                + rf"\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c"
                rf"\u7684\u51c0\u5229\u6da6(?P<value>{cls.MONEY})",
            ),
            "adjusted_net_income_parent": (
                bank_table_prefix
                + rf"\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e"
                rf"\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c\u7684"
                rf"\u51c0\u5229\u6da6(?:[\uff08(]\d+[\uff09)])?"
                rf"(?P<value>{cls.MONEY})",
            ),
            "operating_cash_flow": (
                bank_table_prefix
                + rf"\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1"
                rf"\u6d41\u91cf\u51c0\u989d(?P<value>{cls.MONEY})",
            ),
            "capital_expenditures": (
                bank_table_prefix
                + rf"\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62"
                rf"\u8d44\u4ea7\u548c\u5176\u4ed6\u957f\u671f\u8d44\u4ea7\u652f"
                rf"\u4ed8\u7684\u73b0\u91d1[\uff08(]?"
                rf"(?P<value>{cls.MONEY})",
            ),
            "parent_equity": (
                bank_table_prefix
                + rf"\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c\u7684"
                rf"\u6743\u76ca(?P<value>{cls.MONEY})",
            ),
            "basic_eps": (
                bank_table_prefix
                + rf"\u57fa\u672c\u6bcf\u80a1\u6536\u76ca"
                rf"(?:[\uff08(]\d+[\uff09)])?(?P<value>{cls.NUMBER})",
            ),
            "diluted_eps": (
                bank_table_prefix
                + rf"\u7a00\u91ca\u6bcf\u80a1\u6536\u76ca"
                rf"(?:[\uff08(]\d+[\uff09)])?(?P<value>{cls.NUMBER})",
            ),
        }
        layout_patterns: dict[str, tuple[str, ...]] = {
            "net_income_parent": (
                bank_table_prefix
                + rf"\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c\u7684"
                rf"\u51c0\u5229\u6da6(?P<value>{cls.MONEY})",
            ),
            "adjusted_net_income_parent": (
                bank_table_prefix
                + rf"\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f\u76ca\u540e"
                rf"\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c\u7684"
                rf"\u51c0\u5229\u6da6(?P<value>{cls.MONEY})",
                rf"\u5f52\u5c5e\u4e8e\u4e0a\u5e02\u516c\u53f8\u80a1\u4e1c"
                rf"\u7684\u6263\u9664\u975e\u7ecf\u5e38\u6027\u635f"
                rf"(?P<value>{cls.MONEY})(?:{cls.MONEY}%?){{0,3}}"
                rf"\u76ca\u7684\u51c0\u5229\u6da6[\uff08(]"
                rf"(?P<unit>\u5143|\u5343\u5143|\u4e07\u5143|\u767e\u4e07\u5143)"
                rf"[\uff09)]",
            ),
            "parent_equity": (
                rf"\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u6240\u6709\u8005"
                rf"\u6743\u76ca(?P<value>{cls.MONEY})",
                rf"\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u80a1\u4e1c"
                rf"\u6743\u76ca\u5408\u8ba1(?P<value>{cls.MONEY})",
                rf"\u5f52\u5c5e\u4e8e\u6bcd\u516c\u53f8\u6240\u6709\u8005"
                rf"\u6743\u76ca[\uff08(]\u6216(?P<value>{cls.MONEY})"
                rf"(?:{cls.MONEY})?[\u80a1\u4e1c\u6743\u76ca\uff09)]*"
                rf"\u5408\u8ba1",
                rf"\u5f52\u5c5e\u4e8e\u672c\u516c\u53f8\u80a1\u4e1c"
                rf"(?P<value>{cls.MONEY})\u7684\u51c0\u8d44\u4ea7",
                bank_table_prefix
                + rf"\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c"
                rf"\u6743\u76ca\u5408\u8ba1(?P<value>{cls.MONEY})",
                rf"\u5f52\u5c5e\u4e8e\u672c\u884c\u80a1\u4e1c"
                rf"\u6743\u76ca\u5408\u8ba1(?P<value>{cls.MONEY})",
            ),
            "capital_expenditures": (
                rf"\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62"
                rf"\u8d44\u4ea7\u548c\u5176\u4ed6\u957f\u671f\u8d44\u4ea7"
                rf"\u652f(?P<value>{cls.MONEY})(?:{cls.MONEY}){{0,3}}"
                rf"\u4ed8\u7684\u73b0\u91d1",
                rf"\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62"
                rf"\u8d44\u4ea7\u548c\u5176(?P<value>{cls.MONEY})"
                rf"(?:{cls.MONEY}){{0,2}}\u4ed6\u957f\u671f\u8d44\u4ea7"
                rf"\u652f\u4ed8\u7684\u73b0\u91d1",
                rf"\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u3001\u65e0\u5f62"
                rf"\u8d44\u4ea7\u548c\u5176\u4ed6\u957f\u671f\u8d44\u4ea7"
                rf"\u652f\u4ed8\u7684\u73b0\u91d1[\uff08(]"
                rf"(?P<value>{cls.MONEY})",
                bank_table_prefix
                + rf"\u8d2d\u5efa\u56fa\u5b9a\u8d44\u4ea7\u548c\u5176\u4ed6"
                rf"\u8d44\u4ea7\u6240\u652f\u4ed8\u7684\u73b0\u91d1"
                rf"[\uff08(]?(?P<value>{cls.MONEY})",
            ),
        }
        for extra_patterns in (bank_patterns, layout_patterns):
            for field, candidates in extra_patterns.items():
                patterns[field] = (*patterns[field], *candidates)
        statement_unit: str | None = None
        statement_markers = (
            "合并资产负债表",
            "母公司资产负债表",
            "合并现金流量表",
            "母公司现金流量表",
            "合并及公司资产负债表",
            "合并及公司现金流量表",
        )
        for text in pages:
            compact = re.sub(r"\s+", "", text).replace("−", "-")
            page_units = set(
                re.findall(
                    r"单位[:：](?:人民币)?"
                    r"(元|千元|万元|百万元)",
                    compact,
                )
            )
            declared_unit = cls._declared_statement_unit(compact)
            if declared_unit is not None:
                statement_unit = declared_unit
            elif len(page_units) == 1 and any(
                marker in compact for marker in statement_markers
            ):
                statement_unit = next(iter(page_units))
            for field, candidates in patterns.items():
                if field in values:
                    continue
                for pattern in candidates:
                    match = re.search(pattern, compact)
                    if not match:
                        continue
                    raw_number = match.group("value")
                    accounting_negative = str(raw_number).startswith(
                        ("(", "\uff08")
                    )
                    number = _float(str(raw_number).strip("()\uff08\uff09"))
                    if number is None:
                        continue
                    if accounting_negative:
                        number = -abs(number)
                    unit = match.groupdict().get("unit")
                    if unit is None:
                        if len(page_units) == 1:
                            unit = next(iter(page_units))
                        elif statement_unit is not None:
                            unit = statement_unit
                        else:
                            continue
                    multiplier = {
                        "元": 1.0,
                        "千元": 1_000.0,
                        "万元": 10_000.0,
                    }.get(unit)
                    if unit == "\u767e\u4e07\u5143":
                        multiplier = 1_000_000.0
                    if multiplier is None:
                        continue
                    if field not in {
                        "basic_eps",
                        "diluted_eps",
                        "reported_roe",
                    }:
                        number *= multiplier
                    values[field] = number
                    break
            if all(field in values for field in patterns):
                break
        if not values:
            raise ValueError(
                "no audited financial labels parsed from CNINFO PDF"
            )
        return values, parser_name


class HkexStatementProvider:
    """Read dated HKEX annual/interim disclosures into PIT statements.

    HKEX's title search is the disclosure-date authority.  The report PDF is
    only used to obtain accounting values and period end; it never supplies a
    synthetic filing date.  The conservative English parser intentionally
    skips an ambiguous line rather than guessing a financial value.
    """

    ACTIVE_STOCKS_URL = (
        "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
    )
    TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
    ROOT_URL = "https://www1.hkexnews.hk/"
    REFERER = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=EN"
    MAX_REPORTS_DEFAULT = 36
    _DATE_FORMATS = ("%d/%m/%Y", "%d %B %Y", "%Y-%m-%d")
    _MONTH_NAMES = (
        "january|february|march|april|may|june|july|august|september|"
        "october|november|december"
    )
    _NUMBER = r"(?:\(?[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?[-−]?\d+(?:\.\d+)?\)?)"

    def __init__(
        self,
        config: dict | None = None,
        http: requests.Session | None = None,
    ):
        audit = ((config or {}).get("instrument_audit", {}) or {})
        settings = ((config or {}).get("point_in_time_data", {}) or {})
        self.timeout = int(audit.get("timeout_seconds", 20))
        self.user_agent = audit.get(
            "user_agent",
            "Mozilla/5.0 (compatible; trade-eyes-keeper point-in-time-data)",
        )
        self.max_reports = max(
            1,
            int(settings.get("hkex_max_reports_per_code", self.MAX_REPORTS_DEFAULT)),
        )
        self.http = http or requests.Session()
        self._stock_ids: dict[str, str] | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Referer": self.REFERER,
            "Accept-Language": "en-US,en;q=0.9",
        }

    @staticmethod
    def _code(code: str) -> str:
        normalized = str(code).strip().upper().replace(".HK", "")
        if not normalized.isdigit():
            raise ValueError(f"HKEX code must be numeric: {code}")
        return normalized.zfill(5)

    def _load_stock_ids(self) -> dict[str, str]:
        if self._stock_ids is not None:
            return self._stock_ids
        response = self.http.get(
            self.ACTIVE_STOCKS_URL,
            headers=self._headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("list", []) if isinstance(payload, dict) else payload
        result: dict[str, str] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("c", "")).strip()
            # ``i`` is the title-search internal identifier.  ``s`` is a
            # separate security identifier and returns an empty result set.
            stock_id = str(row.get("i", "")).strip()
            if code and stock_id:
                result[code.zfill(5)] = stock_id
        if not result:
            raise ValueError("HKEX active-stock list returned no identifiers")
        self._stock_ids = result
        return result

    @classmethod
    def _date(cls, value: str) -> date | None:
        cleaned = re.sub(r"\s+", " ", str(value)).strip()
        embedded = re.search(
            rf"\d{{1,2}}/\d{{1,2}}/20\d{{2}}|"
            rf"\d{{1,2}}\s+(?:{cls._MONTH_NAMES})\s+20\d{{2}}|"
            r"20\d{2}-\d{2}-\d{2}",
            cleaned,
            flags=re.IGNORECASE,
        )
        if embedded:
            cleaned = embedded.group(0)
        for fmt in cls._DATE_FORMATS:
            try:
                return datetime.strptime(cleaned[: len("31 December 2000")], fmt).date()
            except ValueError:
                continue
        return None

    @classmethod
    def _period_end(cls, text: str) -> date | None:
        compact = re.sub(r"\s+", " ", text).replace("−", "-")
        named = re.search(
            rf"(?:year|period|six months|half[- ]year)\s+ended\s+"
            rf"(\d{{1,2}}\s+(?:{cls._MONTH_NAMES})\s+20\d{{2}})",
            compact,
            flags=re.IGNORECASE,
        )
        if named:
            return cls._date(named.group(1))
        numeric = re.search(
            r"(?:year|period|six months|half[- ]year)\s+ended\s+"
            r"(\d{1,2}[/-]\d{1,2}[/-]20\d{2})",
            compact,
            flags=re.IGNORECASE,
        )
        if numeric:
            raw = numeric.group(1).replace("-", "/")
            for fmt in ("%d/%m/%Y", "%Y/%m/%d"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
        return None

    @staticmethod
    def _document_kind(headline: str) -> str | None:
        lower = re.sub(r"\s+", " ", headline).lower()
        if any(
            item in lower
            for item in ("annual report", "annual results", "final results")
        ):
            return "year"
        if any(item in lower for item in ("interim", "half-year", "half year")):
            return "half_year"
        return None

    def _announcements(
        self, code: str, start: date, end: date
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        stock_id = self._load_stock_ids().get(self._code(code))
        if not stock_id:
            raise ValueError(f"HKEX stock identifier not found for {code}")
        response = self.http.post(
            self.TITLE_SEARCH_URL,
            data={
                "lang": "EN",
                "market": "SEHK",
                "searchType": "1",
                "stockId": stock_id,
                "category": "0",
                "documentType": "-1",
                "t1code": "40000",
                "t2Gcode": "-2",
                "t2code": "-2",
                # Reports for a period are normally released in the following
                # year, so the disclosure range intentionally extends one year.
                "from": f"{start.year:04d}0101",
                "to": f"{end.year + 1:04d}1231",
                "MB-Daterange": "0",
            },
            headers=self._headers,
            timeout=max(self.timeout, 45),
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        result: list[dict[str, object]] = []
        for row in soup.select("tr"):
            headline_node = row.select_one(".headline")
            link = row.select_one(".doc-link a[href]") or row.select_one(
                "a[href$='.pdf'], a[href*='.pdf?']"
            )
            release_node = row.select_one(".release-time")
            if headline_node is None or link is None or release_node is None:
                continue
            headline = headline_node.get_text(" ", strip=True)
            kind = self._document_kind(headline)
            published_at = self._date(release_node.get_text(" ", strip=True))
            href = str(link.get("href") or "").strip()
            if kind is None or published_at is None or not href:
                continue
            if published_at > end:
                continue
            result.append(
                {
                    "kind": kind,
                    "headline": headline,
                    "published_at": published_at,
                    "url": urljoin(self.ROOT_URL, href),
                }
            )
        # Preserve newest disclosures while preventing a malformed search page
        # from causing an unbounded PDF crawl.
        result.sort(
            key=lambda item: (
                item["published_at"],
                item["kind"] == "year",
                str(item["url"]),
            ),
            reverse=True,
        )
        attempts = [{
            "source": "hkex_title_search",
            "status": "success" if result else "empty",
            "stock_id": stock_id,
            "rows": len(result),
            "url": response.url,
        }]
        return result[: self.max_reports], attempts

    @classmethod
    def _extract_pdf_text(cls, content: bytes) -> tuple[str, str]:
        logging.getLogger("pdfminer").setLevel(logging.WARNING)
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(content)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages), "pdfplumber"
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return text, "pypdf"

    @classmethod
    def _currency_and_multiplier(cls, text: str) -> tuple[str, float, str]:
        sample = text[:30_000]
        upper = sample.upper()
        currency = (
            "HKD"
            if "HK$" in upper or "HKD" in upper
            else "CNY"
            if "RMB" in upper or "CNY" in upper or "人民币" in sample
            else "USD"
            if "US$" in upper or "USD" in upper
            else "HKD"
        )
        unit_match = re.search(
            r"(?:HK\$|RMB|US\$|USD|HKD|CNY)?\s*['’]?\s*"
            r"(\d{3}|\d{6})\b",
            sample,
            flags=re.IGNORECASE,
        )
        if unit_match:
            unit = unit_match.group(1)
            return currency, 1_000.0 if unit == "000" else 1_000_000.0, f"'{unit}"
        word_match = re.search(
            r"(?:amounts?|figures?|except per share data|unless otherwise stated)"
            r".{0,60}?\b(thousand|thousands|million|millions)\b",
            sample,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if word_match:
            word = word_match.group(1).lower()
            return (
                currency,
                1_000_000.0 if word.startswith("million") else 1_000.0,
                word,
            )
        # Most HKEX result announcements declare units in their statement
        # headers.  Do not silently scale a number when the declaration is
        # absent; line values then remain unavailable.
        raise ValueError("HKEX report has no unambiguous statement unit")

    @classmethod
    def _line_value(cls, text: str, patterns: tuple[str, ...]) -> float | None:
        for line in text.splitlines():
            normalized = re.sub(r"\s+", " ", line).replace("−", "-")
            for pattern in patterns:
                match = re.search(pattern, normalized, flags=re.IGNORECASE)
                if not match:
                    continue
                tail = normalized[match.end() :]
                # A note reference is commonly placed between the label and
                # statement amount.  Prefer a grouped amount (or a value with
                # at least four digits) over a one/two digit note number.
                tokens = re.findall(cls._NUMBER, tail)
                parsed: list[tuple[float, str]] = []
                for token in tokens:
                    negative = token.startswith(("(", "-")) or "(" in token
                    value = _float(token.strip("()"))
                    if value is not None:
                        parsed.append((-abs(value) if negative else value, token))
                for value, token in parsed:
                    if "," in token or abs(value) >= 1_000:
                        return value
                if parsed:
                    return parsed[0][0]
        return None

    @classmethod
    def _parse_pdf(cls, content: bytes) -> tuple[dict[str, float], str, date, str]:
        text, parser_name = cls._extract_pdf_text(content)
        period_end = cls._period_end(text)
        if period_end is None:
            raise ValueError("HKEX report period end was not found")
        currency, multiplier, unit = cls._currency_and_multiplier(text)
        labels: dict[str, tuple[str, ...]] = {
            "revenue": (r"\b(?:revenue|turnover|operating revenue)\b",),
            "net_income_parent": (
                r"\bprofit attributable to (?:owners|equity holders|shareholders)\b",
                r"\bprofit attributable to owners of the company\b",
            ),
            "parent_equity": (
                r"\b(?:total )?equity attributable to "
                r"(?:owners|equity holders|shareholders)\b",
            ),
            "operating_cash_flow": (
                r"\bnet cash (?:generated from|from) operating activities\b",
            ),
            "capital_expenditures": (
                r"\b(?:purchase of|additions to) property,? plant and equipment\b",
                r"\bcapital expenditure(?:s)?\b",
            ),
            "basic_eps": (r"\bbasic earnings per share\b",),
            "diluted_eps": (r"\bdiluted earnings per share\b",),
            "diluted_average_shares": (
                r"\bweighted average number of (?:ordinary )?shares\b",
            ),
        }
        values: dict[str, float] = {}
        for field, patterns in labels.items():
            value = cls._line_value(text, patterns)
            if value is None:
                continue
            if field in {"basic_eps", "diluted_eps"}:
                values[field] = value
            elif field == "capital_expenditures":
                values[field] = abs(value) * multiplier
            else:
                values[field] = value * multiplier
        if (
            values.get("operating_cash_flow") is not None
            and values.get("capital_expenditures") is not None
        ):
            values["free_cash_flow"] = (
                values["operating_cash_flow"] - values["capital_expenditures"]
            )
        if not values:
            raise ValueError("no audited HKEX financial labels parsed from PDF")
        values["_currency"] = currency
        values["_unit"] = unit
        return values, parser_name, period_end, currency

    def fetch(self, code: str, start: date, end: date) -> StatementFetchResult:
        attempts: list[dict[str, Any]] = []
        statements: dict[tuple[date, str], FinancialStatementSnapshot] = {}
        try:
            documents, search_attempts = self._announcements(code, start, end)
            attempts.extend(search_attempts)
        except Exception as exc:
            return StatementFetchResult(
                statements=[],
                attempts=[{
                    "source": "hkex_title_search",
                    "status": "failed",
                    "reason": str(exc),
                }],
            )
        for document in documents:
            url = str(document["url"])
            try:
                response = self.http.get(
                    url,
                    headers=self._headers,
                    timeout=max(self.timeout, 60),
                )
                response.raise_for_status()
                values, parser_name, period_end, currency = self._parse_pdf(
                    response.content
                )
                if not start <= period_end <= end:
                    attempts.append({
                        "source": "hkex_results_pdf",
                        "status": "out_of_range",
                        "url": url,
                        "period_end": period_end.isoformat(),
                    })
                    continue
                kind = str(document["kind"])
                snapshot = FinancialStatementSnapshot(
                    period_end=period_end,
                    published_at=document["published_at"],
                    period_type=kind,
                    is_cumulative=True,
                    currency=currency,
                    accounting_standard="HKFRS",
                    source="hkex_results_pdf",
                    source_url=url,
                    total_shares=values.get("diluted_average_shares"),
                    common_shares_outstanding=values.get("diluted_average_shares"),
                    diluted_average_shares=values.get("diluted_average_shares"),
                    parent_equity=values.get("parent_equity"),
                    revenue=values.get("revenue"),
                    net_income_parent=values.get("net_income_parent"),
                    basic_eps=values.get("basic_eps"),
                    diluted_eps=values.get("diluted_eps"),
                    operating_cash_flow=values.get("operating_cash_flow"),
                    capital_expenditures=values.get("capital_expenditures"),
                    free_cash_flow=values.get("free_cash_flow"),
                    diagnostics=[
                        "hkex_release_date_from_title_search",
                        f"hkex_pdf_parser:{parser_name}",
                        f"hkex_statement_unit:{values['_unit']}",
                        "free_cash_flow=operating_cash_flow-capital_expenditures"
                        if values.get("free_cash_flow") is not None
                        else "hkex_free_cash_flow_unavailable",
                    ],
                )
                key = (snapshot.period_end, snapshot.period_type)
                current = statements.get(key)
                if current is None or (
                    _statement_completeness(snapshot),
                    snapshot.published_at or date.min,
                ) > (
                    _statement_completeness(current),
                    current.published_at or date.min,
                ):
                    statements[key] = snapshot
                attempts.append({
                    "source": "hkex_results_pdf",
                    "status": "success",
                    "url": url,
                    "period_end": period_end.isoformat(),
                    "parser": parser_name,
                })
            except Exception as exc:
                attempts.append({
                    "source": "hkex_results_pdf",
                    "status": "failed",
                    "url": url,
                    "reason": str(exc),
                })
        return StatementFetchResult(
            statements=sorted(
                statements.values(),
                key=lambda item: (item.period_end, item.published_at or date.max),
            ),
            attempts=attempts,
        )


STATEMENT_VALUE_FIELDS = (
    "total_shares",
    "common_shares_outstanding",
    "diluted_average_shares",
    "parent_equity",
    "average_parent_equity",
    "book_value_per_share",
    "cash_and_cash_equivalents",
    "restricted_cash",
    "short_term_borrowings",
    "current_portion_noncurrent_debt",
    "long_term_borrowings",
    "bonds_payable",
    "lease_liabilities",
    "revenue",
    "net_income_parent",
    "adjusted_net_income_parent",
    "basic_eps",
    "diluted_eps",
    "operating_cash_flow",
    "capital_expenditures",
    "free_cash_flow",
    "reported_roe",
    "interest_expense",
    "income_tax_expense",
    "profit_before_tax",
)


def _statement_completeness(item: FinancialStatementSnapshot) -> int:
    return sum(
        getattr(item, field) is not None for field in STATEMENT_VALUE_FIELDS
    )


def merge_statement_sources(
    base: Iterable[FinancialStatementSnapshot],
    supplements: Iterable[FinancialStatementSnapshot],
) -> list[FinancialStatementSnapshot]:
    """Create disclosure-time revisions with official fields overriding errors."""

    result = [item.copy(deep=True) for item in base]
    for supplement in sorted(
        supplements,
        key=lambda item: (item.period_end, item.published_at or date.max),
    ):
        candidates = [
            item
            for item in result
            if item.period_end == supplement.period_end
            and item.period_type == supplement.period_type
            and item.published_at is not None
            and supplement.published_at is not None
            and item.published_at <= supplement.published_at
        ]
        if candidates:
            merged = max(
                candidates,
                key=lambda item: (
                    item.published_at or date.min,
                    _statement_completeness(item),
                    item.source,
                ),
            ).copy(deep=True)
            for candidate in sorted(
                candidates,
                key=lambda item: (
                    item.published_at or date.min,
                    _statement_completeness(item),
                    item.source,
                ),
                reverse=True,
            ):
                for field in STATEMENT_VALUE_FIELDS:
                    if getattr(merged, field) is None:
                        value = getattr(candidate, field)
                        if value is not None:
                            setattr(merged, field, value)
                merged.source = "+".join(
                    dict.fromkeys([
                        *merged.source.split("+"),
                        *candidate.source.split("+"),
                    ])
                )
                merged.diagnostics = list(
                    dict.fromkeys([
                        *merged.diagnostics,
                        *candidate.diagnostics,
                    ])
                )
        else:
            merged = supplement.copy(deep=True)
        prior_source = merged.source
        for field in STATEMENT_VALUE_FIELDS:
            value = getattr(supplement, field)
            if value is not None:
                previous = getattr(merged, field)
                if previous is not None and previous != value:
                    merged.diagnostics.append(
                        "official_value_conflict:"
                        f"{field}:{prior_source}->{supplement.source}"
                    )
                setattr(merged, field, value)
        merged.published_at = supplement.published_at
        merged.currency = supplement.currency or merged.currency
        merged.accounting_standard = (
            supplement.accounting_standard or merged.accounting_standard
        )
        merged.source = "+".join(
            dict.fromkeys(
                part
                for source in (merged.source, supplement.source)
                for part in source.split("+")
            )
        )
        merged.source_url = supplement.source_url or merged.source_url
        merged.diagnostics = list(
            dict.fromkeys([
                *merged.diagnostics,
                *supplement.diagnostics,
                f"official_fields_overlaid_from:{supplement.source}",
            ])
        )
        result.append(merged)
    selected: dict[tuple[date, date, str], FinancialStatementSnapshot] = {}
    for item in result:
        if item.published_at is None:
            continue
        key = (item.period_end, item.published_at, item.source)
        selected[key] = item
    return sorted(
        selected.values(),
        key=lambda item: (
            item.period_end,
            item.published_at or date.max,
            item.source,
        ),
    )


class PointInTimeFundamentalStore:
    """Revision-preserving JSON store keyed by real disclosure date."""

    def __init__(self, root: str | Path = "data/point_in_time"):
        self.root = Path(root)
        self.statement_dir = self.root / "fundamentals"

    @staticmethod
    def _safe_code(code: str) -> str:
        return str(code).replace(".", "-").replace("/", "-")

    def _path(self, code: str) -> Path:
        return self.statement_dir / f"{self._safe_code(code)}.statements.json"

    def read_all(self, code: str) -> list[FinancialStatementSnapshot]:
        path = self._path(code)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("statements", [])
        statements = []
        for item in rows:
            statement = FinancialStatementSnapshot(**item)
            if (
                "cninfo_annual_report" in statement.source
                and statement.source_url
            ):
                match = re.search(
                    r"/finalpage/(20\d{2}-\d{2}-\d{2})/",
                    statement.source_url,
                )
                disclosed = _date(match.group(1)) if match else None
                if disclosed is not None and statement.published_at != disclosed:
                    statement.published_at = disclosed
            if (
                statement.source == "baostock_profit"
                and statement.period_end.month == 12
                and statement.is_cumulative
                and statement.period_type == "quarter"
            ):
                statement.period_type = "year"
                statement.diagnostics.append(
                    "legacy_baostock_q4_period_type_normalized"
                )
            statements.append(statement)
        return statements

    def upsert(
        self,
        code: str,
        statements: Iterable[FinancialStatementSnapshot],
        *,
        replace_sources: set[str] | None = None,
    ) -> Path:
        existing = self.read_all(code)
        if replace_sources:
            existing = [
                item for item in existing
                if not replace_sources.intersection(item.source.split("+"))
            ]
        selected: dict[
            tuple[date, date, str], FinancialStatementSnapshot
        ] = {}
        for item in [*existing, *statements]:
            if item.published_at is None:
                logger.warning(
                    "Rejecting undated statement from PIT store: %s %s",
                    code,
                    item.period_end,
                )
                continue
            key = (
                item.period_end,
                item.published_at,
                item.source,
            )
            current = selected.get(key)
            if (
                current is None
                or self._completeness(item) >= self._completeness(current)
            ):
                selected[key] = item
        ordered = sorted(
            selected.values(),
            key=lambda item: (
                item.period_end,
                item.published_at or date.max,
                item.source,
            ),
        )
        self.statement_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(code)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "code": str(code),
                    "contract": "point-in-time-statements-1",
                    "updated_at": datetime.now().isoformat(),
                    "statements": [json.loads(item.json()) for item in ordered],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    @staticmethod
    def _completeness(item: FinancialStatementSnapshot) -> int:
        return sum(
            getattr(item, name) is not None
            for name in (
                "total_shares",
                "common_shares_outstanding",
                "parent_equity",
                "average_parent_equity",
                "revenue",
                "net_income_parent",
                "adjusted_net_income_parent",
                "operating_cash_flow",
                "capital_expenditures",
                "free_cash_flow",
                "diluted_eps",
                "reported_roe",
                "cash_and_cash_equivalents",
                "restricted_cash",
                "short_term_borrowings",
                "current_portion_noncurrent_debt",
                "long_term_borrowings",
                "bonds_payable",
                "lease_liabilities",
                "interest_expense",
                "income_tax_expense",
                "profit_before_tax",
            )
        )

    def as_of(
        self, code: str, evaluation_date: date
    ) -> list[FinancialStatementSnapshot]:
        """Return the latest known revision of every report period."""
        available = [
            item
            for item in self.read_all(code)
            if item.period_end <= evaluation_date
            and item.published_at is not None
            and item.published_at <= evaluation_date
        ]
        latest: dict[tuple[date, str], FinancialStatementSnapshot] = {}
        for item in available:
            key = (item.period_end, item.period_type)
            current = latest.get(key)
            if current is None or (
                item.published_at or date.min,
                self._completeness(item),
            ) > (
                current.published_at or date.min,
                self._completeness(current),
            ):
                latest[key] = item
        return sorted(
            latest.values(),
            key=lambda item: (item.period_end, item.published_at or date.max),
        )


FUNDAMENTAL_FEATURE_NAMES = (
    "pe_ttm",
    "pb",
    "roe_ttm",
    "revenue_yoy",
    "revenue_qoq",
    "net_income_yoy",
    "net_income_qoq",
    "financial_age_days",
)


@dataclass(frozen=True)
class FundamentalFeaturePanel:
    dates: list[date]
    symbols: list[str]
    feature_names: tuple[str, ...]
    values: np.ndarray
    availability_mask: np.ndarray


def adjust_statement_shares(
    statements: Iterable[FinancialStatementSnapshot],
    actions: Iterable[CorporateAction],
    evaluation_date: date,
) -> list[FinancialStatementSnapshot]:
    """Roll reported shares through known share-changing actions.

    Cash dividends never alter shares.  Rights issues are not inferred from an
    adjustment factor because their proceeds change equity; an explicit
    ``share_multiplier`` is required.  This transformation changes per-share
    denominators only and never rewrites historical profit/equity totals.
    """
    known_actions = [
        action
        for action in actions
        if action.ex_date <= evaluation_date
        and action.share_multiplier is not None
        and action.share_multiplier > 0
        and (action.published_at is None or action.published_at <= evaluation_date)
    ]
    adjusted: list[FinancialStatementSnapshot] = []
    for statement in statements:
        copy = statement.copy(deep=True)
        multiplier = 1.0
        for action in known_actions:
            if statement.period_end < action.ex_date:
                multiplier *= float(action.share_multiplier)
        if multiplier != 1.0:
            for name in (
                "total_shares",
                "common_shares_outstanding",
                "diluted_average_shares",
            ):
                value = getattr(copy, name)
                if value is not None:
                    setattr(copy, name, float(value) * multiplier)
            if copy.book_value_per_share is not None:
                copy.book_value_per_share = (
                    float(copy.book_value_per_share) / multiplier
                )
            copy.diagnostics.append(
                f"shares_adjusted_for_disclosed_actions:{multiplier:.12g}"
            )
        adjusted.append(copy)
    return adjusted


class FundamentalFeaturePanelBuilder:
    """Build no-lookahead daily features from raw prices and filing history."""

    def __init__(self, store: PointInTimeFundamentalStore):
        self.store = store

    def build(
        self,
        bundles: dict[str, PriceHistoryBundle],
        *,
        dates: Iterable[date] | None = None,
    ) -> FundamentalFeaturePanel:
        symbols = list(bundles)
        if dates is None:
            date_sets = [
                set(pd.to_datetime(bundle.prices["date"]).dt.date)
                for bundle in bundles.values()
            ]
            panel_dates = sorted(set().union(*date_sets)) if date_sets else []
        else:
            panel_dates = sorted({_date(item) for item in dates if _date(item)})
        values = np.full(
            (len(panel_dates), len(symbols), len(FUNDAMENTAL_FEATURE_NAMES)),
            np.nan,
            dtype=np.float32,
        )
        mask = np.zeros_like(values, dtype=bool)
        price_maps = {
            code: {
                timestamp.date(): float(raw_close)
                for timestamp, raw_close in zip(
                    pd.to_datetime(bundle.prices["date"]),
                    pd.to_numeric(bundle.prices["raw_close"], errors="coerce"),
                )
                if np.isfinite(raw_close) and raw_close > 0
            }
            for code, bundle in bundles.items()
        }
        for date_index, evaluation_date in enumerate(panel_dates):
            for symbol_index, code in enumerate(symbols):
                raw_price = price_maps[code].get(evaluation_date)
                if raw_price is None:
                    continue
                statements = self.store.as_of(code, evaluation_date)
                if not statements:
                    continue
                statements = adjust_statement_shares(
                    statements,
                    bundles[code].actions,
                    evaluation_date,
                )
                company = derive_company_fundamentals(
                    statements,
                    current_price=MetricValue(
                        value=raw_price,
                        status=MetricStatus.OBSERVED,
                        as_of=evaluation_date,
                        source="point_in_time_raw_close",
                    ),
                    evaluation_date=evaluation_date,
                )
                latest_publication = max(
                    item.published_at for item in statements if item.published_at
                )
                feature_values = {
                    "pe_ttm": company.pe_ttm.value,
                    "pb": company.pb.value,
                    "roe_ttm": company.roe_ttm.value,
                    "revenue_yoy": _growth_value(company, "revenue_yoy"),
                    "revenue_qoq": _growth_value(company, "revenue_qoq"),
                    "net_income_yoy": _growth_value(company, "net_income_yoy"),
                    "net_income_qoq": _growth_value(company, "net_income_qoq"),
                    "financial_age_days": float(
                        (evaluation_date - latest_publication).days
                    ),
                }
                for feature_index, name in enumerate(FUNDAMENTAL_FEATURE_NAMES):
                    value = feature_values[name]
                    if value is not None and np.isfinite(value):
                        values[date_index, symbol_index, feature_index] = value
                        mask[date_index, symbol_index, feature_index] = True
        return FundamentalFeaturePanel(
            dates=panel_dates,
            symbols=symbols,
            feature_names=FUNDAMENTAL_FEATURE_NAMES,
            values=values,
            availability_mask=mask,
        )


def _growth_value(company, key: str) -> float | None:
    metric = company.growth.get(key)
    if metric is None or metric.status != MetricStatus.DERIVED:
        return None
    return metric.value_pct
