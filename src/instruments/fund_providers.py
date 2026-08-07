"""Audit-only fund profile providers.

The Eastmoney adapter is an explicitly secondary disclosure mirror used only
to improve the current data-quality report.  It is not a price fallback and
its output is never exposed to optimization.  A configured issuer source
always overrides it.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

from .classifier import detect_market
from .models import (
    FundHolding,
    FundProfile,
    InstrumentType,
    MetricStatus,
    MetricValue,
)
from .providers import HttpProvider, ProviderPayload, _float


def _text_after_header(soup: BeautifulSoup, label: str) -> Optional[str]:
    header = soup.find("th", string=lambda value: value and label in value)
    if header is None:
        return None
    cell = header.find_next_sibling("td")
    return cell.get_text(" ", strip=True) if cell else None


def _parse_percent(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", text)
    return float(match.group(1)) if match else None


def _parse_chinese_amount(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*(万|亿|万亿)?", text)
    if not match:
        return None
    value = float(match.group(1))
    multiplier = {
        None: 1.0,
        "万": 10_000.0,
        "亿": 100_000_000.0,
        "万亿": 1_000_000_000_000.0,
    }[match.group(2)]
    return value * multiplier


class EastmoneyFundProfileProvider(HttpProvider):
    """Current fund facts and reported top holdings for mainland funds."""

    BASIC_URL = "https://fundf10.eastmoney.com/jbgk_{code}.html"
    HOLDINGS_URL = (
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        "?type=jjcc&code={code}&topline=10&year=&month=&rt=0.123"
    )
    NAV_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"

    def fetch(
        self,
        code: str,
        evaluation_date: date,
        instrument_type: InstrumentType,
    ) -> ProviderPayload:
        payload = ProviderPayload()
        normalized = str(code).strip()
        if (
            detect_market(normalized) != "a_share"
            or instrument_type in {InstrumentType.EQUITY, InstrumentType.REIT}
        ):
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_disclosure_mirror",
                    "status": "not_applicable",
                }
            )
            return payload

        fund = FundProfile()
        self._fetch_basic(normalized, evaluation_date, fund, payload)
        self._fetch_nav(normalized, evaluation_date, fund, payload)
        self._fetch_holdings(normalized, evaluation_date, fund, payload)
        payload.fund = fund
        return payload

    def _fetch_basic(
        self,
        code: str,
        evaluation_date: date,
        fund: FundProfile,
        payload: ProviderPayload,
    ) -> None:
        try:
            response = self._get(
                self.BASIC_URL.format(code=code),
                headers={
                    "Referer": f"https://fundf10.eastmoney.com/jbgk_{code}.html"
                },
            )
            text = response.content.decode("utf-8", errors="replace")
            soup = BeautifulSoup(text, "html.parser")
            full_name = _text_after_header(soup, "基金全称")
            issuer = _text_after_header(soup, "基金管理人")
            tracking = _text_after_header(soup, "跟踪标的")
            fund_type = _text_after_header(soup, "基金类型")
            aum_text = _text_after_header(soup, "净资产规模")
            fee_text = _text_after_header(soup, "管理费率")
            if full_name:
                payload.metadata["name"] = full_name
            fund.issuer = issuer
            fund.tracking_index = tracking
            fund.asset_class = fund_type
            aum = _parse_chinese_amount(aum_text)
            if aum is not None:
                aum_date_match = re.search(
                    r"(\d{4})年(\d{2})月(\d{2})日",
                    aum_text or "",
                )
                aum_date = (
                    date(
                        int(aum_date_match.group(1)),
                        int(aum_date_match.group(2)),
                        int(aum_date_match.group(3)),
                    )
                    if aum_date_match
                    else evaluation_date
                )
                fund.aum = MetricValue(
                    value=aum,
                    status=MetricStatus.OBSERVED,
                    as_of=aum_date,
                    source="eastmoney_fund_disclosure_mirror",
                    source_url=response.url,
                    currency="CNY",
                    confidence=0.7,
                )
            fee = _parse_percent(fee_text)
            if fee is not None:
                fund.expense_ratio = MetricValue(
                    value=fee,
                    status=MetricStatus.OBSERVED,
                    as_of=evaluation_date,
                    source="eastmoney_fund_disclosure_mirror",
                    source_url=response.url,
                    confidence=0.7,
                    note="管理费率；不含托管费等其他费用",
                )
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_basic",
                    "status": "success",
                    "url": response.url,
                }
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_basic",
                    "status": "failed",
                    "reason": str(exc),
                }
            )

    def _fetch_nav(
        self,
        code: str,
        evaluation_date: date,
        fund: FundProfile,
        payload: ProviderPayload,
    ) -> None:
        try:
            response = self._get(
                self.NAV_URL.format(code=code),
                headers={"Referer": f"https://fund.eastmoney.com/{code}.html"},
            )
            match = re.search(
                r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);",
                response.text,
                re.DOTALL,
            )
            if not match:
                raise ValueError("Data_netWorthTrend not found")
            observations = json.loads(match.group(1))
            usable = []
            for observation in observations:
                timestamp = observation.get("x")
                nav = _float(observation.get("y"))
                if timestamp is None or nav is None:
                    continue
                nav_date = datetime.fromtimestamp(
                    float(timestamp) / 1000.0,
                    tz=timezone.utc,
                ).date()
                if nav_date <= evaluation_date:
                    usable.append((nav_date, nav))
            if not usable:
                raise ValueError("no NAV on or before evaluation date")
            nav_date, nav = max(usable, key=lambda item: item[0])
            fund.nav_per_unit = MetricValue(
                value=nav,
                status=MetricStatus.OBSERVED,
                as_of=nav_date,
                source="eastmoney_fund_nav_mirror",
                source_url=response.url,
                currency="CNY",
                confidence=0.7,
            )
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_nav",
                    "status": "success",
                    "url": response.url,
                }
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_nav",
                    "status": "failed",
                    "reason": str(exc),
                }
            )

    def _fetch_holdings(
        self,
        code: str,
        evaluation_date: date,
        fund: FundProfile,
        payload: ProviderPayload,
    ) -> None:
        try:
            response = self._get(
                self.HOLDINGS_URL.format(code=code),
                headers={
                    "Referer": (
                        f"https://fundf10.eastmoney.com/ccmx_{code}.html"
                    )
                },
            )
            text = response.content.decode("utf-8", errors="replace")
            soup = BeautifulSoup(text, "html.parser")
            table = next(
                (
                    candidate
                    for candidate in soup.find_all("table")
                    if "股票代码" in candidate.get_text()
                    and "占净值" in candidate.get_text()
                ),
                None,
            )
            if table is None:
                raise ValueError("top-holdings table not found")
            heading = table.find_previous("h4")
            heading_text = heading.get_text(" ", strip=True) if heading else text
            date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", heading_text)
            holdings_date = (
                date(
                    int(date_match.group(1)),
                    int(date_match.group(2)),
                    int(date_match.group(3)),
                )
                if date_match
                else None
            )
            if holdings_date and holdings_date > evaluation_date:
                raise ValueError("holdings date is after evaluation date")
            holdings = []
            for row in table.select("tbody tr"):
                cells = row.find_all("td")
                if len(cells) < 7:
                    continue
                holding_code = cells[1].get_text(" ", strip=True)
                holding_name = cells[2].get_text(" ", strip=True)
                weight = _parse_percent(cells[6].get_text(" ", strip=True))
                if not holding_code or weight is None:
                    continue
                holdings.append(
                    FundHolding(
                        code=holding_code,
                        name=holding_name,
                        weight=weight / 100.0,
                        as_of=holdings_date,
                        source="eastmoney_fund_disclosure_mirror",
                    )
                )
                if len(holdings) >= 10:
                    break
            if not holdings:
                raise ValueError("top-holdings table has no usable rows")
            fund.holdings_as_of = holdings_date
            fund.top_holdings = holdings
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_holdings",
                    "status": "success",
                    "url": response.url,
                    "as_of": holdings_date.isoformat() if holdings_date else None,
                }
            )
        except Exception as exc:
            payload.attempts.append(
                {
                    "source": "eastmoney_fund_holdings",
                    "status": "failed",
                    "reason": str(exc),
                }
            )
