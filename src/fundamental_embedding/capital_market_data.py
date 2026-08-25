"""Official, dated capital-market inputs for cost-of-capital estimation."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
import requests

from src.fundamental_embedding.capital_cost import CapitalMarketAssumptions


CHINABOND_HISTORY_URL = (
    "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
)
CSI300_FACTSHEET_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "indices/detail/files/zh_CN/000300factsheet.pdf"
)


@dataclass(frozen=True)
class IndexFactsheet:
    as_of: date
    trailing_pe: float
    price_to_book: float
    dividend_yield: float
    source_url: str


@dataclass(frozen=True)
class ImpliedEquityRiskPremium:
    """Forward required return solved from observable index fundamentals."""

    market_risk_premium: float
    low: float
    high: float
    required_market_return: float
    initial_growth: float
    roe_proxy: float
    current_payout_ratio: float
    terminal_growth: float
    scenarios: tuple[dict[str, float], ...]
    method: str = "five_year_roe_payout_fade_implied_return"


def parse_csi300_factsheet(
    content: bytes,
    *,
    source_url: str = CSI300_FACTSHEET_URL,
) -> IndexFactsheet:
    """Parse dated CSI 300 valuation fields from the official PDF."""

    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as document:
        text = "\n".join(page.extract_text() or "" for page in document.pages)
    date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    pe_match = re.search(r"滚动市盈率\s*([0-9]+(?:\.[0-9]+)?)", text)
    pb_match = re.search(r"市净率\s*([0-9]+(?:\.[0-9]+)?)", text)
    yield_match = re.search(r"股息率\s*([0-9]+(?:\.[0-9]+)?)%", text)
    if not date_match or not pe_match or not pb_match or not yield_match:
        raise ValueError(
            "CSI 300 factsheet is missing date, PE, PB, or dividend yield"
        )
    trailing_pe = float(pe_match.group(1))
    price_to_book = float(pb_match.group(1))
    dividend_yield = float(yield_match.group(1)) / 100.0
    if (
        trailing_pe <= 0
        or price_to_book <= 0
        or not 0.0 <= dividend_yield <= 0.20
    ):
        raise ValueError("CSI 300 factsheet valuation fields are invalid")
    return IndexFactsheet(
        as_of=date(*(int(item) for item in date_match.groups())),
        trailing_pe=trailing_pe,
        price_to_book=price_to_book,
        dividend_yield=dividend_yield,
        source_url=source_url,
    )


def _index_present_value(
    *,
    required_return: float,
    earnings_yield: float,
    current_payout: float,
    initial_growth: float,
    roe_proxy: float,
    terminal_growth: float,
    projection_years: int,
) -> float:
    if required_return <= terminal_growth:
        return float("inf")
    terminal_payout = min(max(1.0 - terminal_growth / roe_proxy, 0.0), 1.0)
    earnings = earnings_yield
    value = 0.0
    for year in range(1, projection_years + 1):
        progress = year / projection_years
        growth = initial_growth + progress * (terminal_growth - initial_growth)
        payout = current_payout + progress * (terminal_payout - current_payout)
        earnings *= 1.0 + growth
        value += earnings * payout / ((1.0 + required_return) ** year)
    terminal_dividend = (
        earnings * (1.0 + terminal_growth) * terminal_payout
    )
    terminal_value = terminal_dividend / (required_return - terminal_growth)
    return value + terminal_value / ((1.0 + required_return) ** projection_years)


def _solve_implied_market_return(
    *,
    earnings_yield: float,
    current_payout: float,
    initial_growth: float,
    roe_proxy: float,
    terminal_growth: float,
    projection_years: int,
) -> float:
    low = terminal_growth + 1e-5
    high = 0.30
    if _index_present_value(
        required_return=high,
        earnings_yield=earnings_yield,
        current_payout=current_payout,
        initial_growth=initial_growth,
        roe_proxy=roe_proxy,
        terminal_growth=terminal_growth,
        projection_years=projection_years,
    ) > 1.0:
        raise ValueError("implied market return is above the solver bound")
    for _ in range(100):
        middle = (low + high) / 2.0
        value = _index_present_value(
            required_return=middle,
            earnings_yield=earnings_yield,
            current_payout=current_payout,
            initial_growth=initial_growth,
            roe_proxy=roe_proxy,
            terminal_growth=terminal_growth,
            projection_years=projection_years,
        )
        if value > 1.0:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def implied_equity_risk_premium(
    factsheet: IndexFactsheet,
    risk_free_rate: float,
    *,
    projection_years: int = 5,
    terminal_growth_rates: tuple[float, ...] = (0.02, 0.03, 0.04),
    base_terminal_growth: float = 0.03,
) -> ImpliedEquityRiskPremium:
    """Solve a forward CSI 300 ERP from PE, PB, dividends, and ROE.

    Price is normalized to one.  Current aggregate ROE is PB/PE, current
    payout is dividend yield / earnings yield, and growth is ROE times the
    retention ratio.  Growth and payout fade over five years to each stated
    terminal-growth scenario.  This is an auditable forward model, not the
    old ``earnings yield - government yield`` shortcut.
    """

    if projection_years < 1:
        raise ValueError("projection_years must be positive")
    earnings_yield = 1.0 / factsheet.trailing_pe
    roe_proxy = factsheet.price_to_book / factsheet.trailing_pe
    if roe_proxy <= max(terminal_growth_rates):
        raise ValueError("index ROE proxy must exceed terminal growth")
    current_payout = factsheet.dividend_yield / earnings_yield
    current_payout = min(max(current_payout, 0.0), 1.0)
    initial_growth = roe_proxy * (1.0 - current_payout)
    scenarios = []
    for terminal_growth in terminal_growth_rates:
        required_return = _solve_implied_market_return(
            earnings_yield=earnings_yield,
            current_payout=current_payout,
            initial_growth=initial_growth,
            roe_proxy=roe_proxy,
            terminal_growth=terminal_growth,
            projection_years=projection_years,
        )
        scenarios.append(
            {
                "terminal_growth": terminal_growth,
                "required_market_return": required_return,
                "market_risk_premium": required_return - risk_free_rate,
                "terminal_payout_ratio": 1.0 - terminal_growth / roe_proxy,
            }
        )
    base = min(
        scenarios,
        key=lambda item: abs(item["terminal_growth"] - base_terminal_growth),
    )
    premiums = [item["market_risk_premium"] for item in scenarios]
    if not all(0.0 < item < 0.20 for item in premiums):
        raise ValueError("implied CSI 300 ERP is outside valid bounds")
    return ImpliedEquityRiskPremium(
        market_risk_premium=base["market_risk_premium"],
        low=min(premiums),
        high=max(premiums),
        required_market_return=base["required_market_return"],
        initial_growth=initial_growth,
        roe_proxy=roe_proxy,
        current_payout_ratio=current_payout,
        terminal_growth=base["terminal_growth"],
        scenarios=tuple(scenarios),
    )


def parse_chinabond_ten_year_yield(
    content: str,
    *,
    as_of: date,
) -> float:
    """Parse the 10-year government-curve yield; return a decimal rate."""

    tables = pd.read_html(io.StringIO(content))
    for table in tables:
        if "日期" not in table.columns and not table.empty:
            promoted = [str(value).strip() for value in table.iloc[0].tolist()]
            if "日期" in promoted and "10年" in promoted:
                table = table.iloc[1:].copy()
                table.columns = promoted
        if "日期" not in table.columns or "10年" not in table.columns:
            continue
        selected = table[
            (table["日期"].astype(str) == as_of.isoformat())
            & table["曲线名称"].astype(str).str.contains("中债国债收益率曲线")
        ]
        if selected.empty:
            continue
        value = float(pd.to_numeric(selected.iloc[0]["10年"], errors="raise"))
        rate = value / 100.0
        if not 0.0 < rate < 0.20:
            raise ValueError("ChinaBond 10-year yield is outside valid bounds")
        return rate
    raise ValueError(f"ChinaBond 10-year yield unavailable for {as_of}")


class OfficialCapitalMarketDataProvider:
    """Fetch a transparent CAPM input set from public official sources."""

    def __init__(
        self,
        *,
        http: Any | None = None,
        timeout: int = 30,
        user_agent: str = "trade-eyes-keeper capital-cost research",
    ):
        self.http = http or requests.Session()
        self.timeout = int(timeout)
        self.headers = {"User-Agent": user_agent}

    def fetch_csi300_factsheet(self) -> IndexFactsheet:
        response = self.http.get(
            CSI300_FACTSHEET_URL,
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return parse_csi300_factsheet(response.content)

    def fetch_chinabond_ten_year_yield(self, as_of: date) -> float:
        response = self.http.get(
            CHINABOND_HISTORY_URL,
            params={
                "startDate": as_of.isoformat(),
                "endDate": as_of.isoformat(),
                "gjqx": "0",
                "qxId": "ycqx",
                "locale": "cn_ZH",
            },
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return parse_chinabond_ten_year_yield(response.text, as_of=as_of)

    def fetch_assumptions(self) -> CapitalMarketAssumptions:
        factsheet = self.fetch_csi300_factsheet()
        risk_free_rate = self.fetch_chinabond_ten_year_yield(factsheet.as_of)
        implied = implied_equity_risk_premium(factsheet, risk_free_rate)
        return CapitalMarketAssumptions(
            as_of=factsheet.as_of,
            risk_free_rate=risk_free_rate,
            market_risk_premium=implied.market_risk_premium,
            risk_free_source=(
                f"{CHINABOND_HISTORY_URL};10Y government yield;"
                f"as_of={factsheet.as_of.isoformat()}"
            ),
            market_risk_premium_source=(
                f"{factsheet.source_url};CSI300 PE={factsheet.trailing_pe:.4f};"
                f"PB={factsheet.price_to_book:.4f};"
                f"dividend_yield={factsheet.dividend_yield:.6f};"
                "five_year_roe_payout_fade;terminal_g=2%/3%/4%"
            ),
            market_risk_premium_method=implied.method,
            market_risk_premium_low=implied.low,
            market_risk_premium_high=implied.high,
            market_risk_premium_inputs={
                "trailing_pe": factsheet.trailing_pe,
                "price_to_book": factsheet.price_to_book,
                "dividend_yield": factsheet.dividend_yield,
                "roe_proxy": implied.roe_proxy,
                "current_payout_ratio": implied.current_payout_ratio,
                "initial_growth": implied.initial_growth,
                "required_market_return": implied.required_market_return,
                "base_terminal_growth": implied.terminal_growth,
            },
        )
