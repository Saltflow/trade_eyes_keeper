"""Current dividend events for the daily-report event feed.

This module is deliberately reporting-only.  It does not alter the historical
corporate-action contract used by the optimizer or backtester: it merely
collects announced or currently unsettled distributions for the daily email.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup

from ..utils.etf_detector import is_etf, is_reit
from .etf_valuation import ETF_VALUATION_REFERENCES


logger = logging.getLogger(__name__)


FUND_DISTRIBUTION_URL = "https://fundf10.eastmoney.com/fhsp_{code}.html"
ETNET_HK_DIVIDEND_URL = (
    "https://content.etnet.com.hk/content/firstshanghai/tc/quote_dividend.php?"
    "code={code}"
)
YAHOO_QUOTE_URL = "https://finance.yahoo.com/quote/{ticker}/"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

DIVIDEND_PENDING = "未除权"
DIVIDEND_EX_UNPAID = "已除权未派息"


def _as_date(value: object) -> date | None:
    """Parse only explicit calendar dates; never infer a payment date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _finite_positive(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) and number > 0 else None


def _event_status(
    ex_date: date | None, payment_date: date | None, as_of: date
) -> str | None:
    """Classify only the two actionable stages requested by the report."""
    if ex_date is None or payment_date is None:
        return None
    if as_of < ex_date:
        return DIVIDEND_PENDING
    if payment_date is not None and ex_date <= as_of < payment_date:
        return DIVIDEND_EX_UNPAID
    return None


class DailyDividendEventResolver:
    """Prepare currently actionable stock and fund distributions for one run."""

    def __init__(
        self,
        config: dict,
        *,
        http_get: Callable[..., requests.Response] = requests.get,
    ) -> None:
        self.config = config
        self._http_get = http_get
        settings = config.get("announcements", {}) or {}
        self.timeout = int(settings.get("distribution_timeout_seconds", 10))
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )

    def resolve(
        self,
        announcements: dict[str, list] | None,
        stock_data: pd.DataFrame | None,
        *,
        as_of: date | None = None,
    ) -> list[dict[str, object]]:
        """Return deduplicated pending distributions for visible instruments."""
        report_date = as_of or date.today()
        codes = self._codes(stock_data)
        events = self._announcement_events(announcements, report_date)
        events.extend(self._stored_action_events(codes, report_date))

        for code in codes:
            if self._is_mainland_fund(code):
                events.extend(self._mainland_fund_events(code, report_date))
            elif self._is_hk_listed(code):
                events.extend(self._hk_dividend_events(code, report_date))
            elif self._is_overseas_equity_etf(code):
                events.extend(self._yahoo_etf_events(code, report_date))

        selected: dict[tuple[object, ...], dict[str, object]] = {}
        for event in events:
            key = (
                event["code"],
                event["ex_date"],
                event.get("payment_date"),
                event.get("cash_per_share"),
                event.get("currency"),
            )
            existing = selected.get(key)
            if existing is None or self._source_rank(event) < self._source_rank(existing):
                selected[key] = event

        return sorted(
            selected.values(),
            key=lambda item: (
                0 if item["status"] == DIVIDEND_EX_UNPAID else 1,
                str(item["ex_date"]),
                str(item["code"]),
            ),
        )

    @staticmethod
    def _codes(stock_data: pd.DataFrame | None) -> list[str]:
        if stock_data is None or "stock_code" not in stock_data:
            return []
        return list(
            dict.fromkeys(
                str(code).strip().upper()
                for code in stock_data["stock_code"].tolist()
                if str(code).strip()
            )
        )

    @staticmethod
    def _source_rank(event: dict[str, object]) -> int:
        source = str(event.get("source", ""))
        return {
            "announcement": 0,
            "fund_distribution": 1,
            "hk_dividend_schedule": 1,
            "stored_corporate_action": 2,
            "yahoo_calendar": 3,
        }.get(source, 9)

    @staticmethod
    def _event(
        *,
        code: str,
        ex_date: date,
        payment_date: date | None,
        cash_per_share: float | None,
        status: str,
        source: str,
        currency: str | None = None,
    ) -> dict[str, object]:
        return {
            "code": code,
            "ex_date": ex_date.isoformat(),
            "payment_date": payment_date.isoformat() if payment_date else None,
            "cash_per_share": cash_per_share,
            "currency": currency,
            "status": status,
            "source": source,
        }

    def _announcement_events(
        self, announcements: dict[str, list] | None, as_of: date
    ) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for raw_code, items in (announcements or {}).items():
            code = str(raw_code).strip().upper()
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                dividend = item.get("llm_extracted_dividend") or {}
                if not isinstance(dividend, dict) or not dividend.get("success"):
                    continue
                cash = _finite_positive(dividend.get("cash_dividend_per_share"))
                ex_date = _as_date(dividend.get("ex_rights_date"))
                payment_date = _as_date(dividend.get("payment_date"))
                status = _event_status(ex_date, payment_date, as_of)
                if status is None or ex_date is None:
                    continue
                events.append(
                    self._event(
                        code=code,
                        ex_date=ex_date,
                        payment_date=payment_date,
                        cash_per_share=cash,
                        currency=None,
                        status=status,
                        source="announcement",
                    )
                )
        return events

    def _stored_action_events(
        self, codes: Iterable[str], as_of: date
    ) -> list[dict[str, object]]:
        """Use dated local actions when the upstream source supplied payable dates."""
        try:
            from .market_history import PointInTimeMarketStore

            settings = self.config.get("point_in_time_data", {}) or {}
            store = PointInTimeMarketStore(
                settings.get("output_dir", "data/point_in_time")
            )
        except Exception as exc:  # pragma: no cover - optional report enrichment
            logger.debug("日报公司行为存储不可用: %s", exc)
            return []

        events = []
        for code in codes:
            try:
                bundle = store.read(code)
            except Exception as exc:
                logger.debug("读取 %s 的日报公司行为失败: %s", code, exc)
                continue
            if bundle is None:
                continue
            for action in bundle.actions:
                cash = _finite_positive(action.cash_per_share)
                status = _event_status(action.ex_date, action.payable_date, as_of)
                if cash is None or status is None:
                    continue
                events.append(
                    self._event(
                        code=code,
                        ex_date=action.ex_date,
                        payment_date=action.payable_date,
                        cash_per_share=cash,
                        currency=getattr(action, "currency", None),
                        status=status,
                        source="stored_corporate_action",
                    )
                )
        return events

    @staticmethod
    def _is_mainland_fund(code: str) -> bool:
        return code.isdigit() and len(code) == 6 and (is_etf(code) or is_reit(code))

    @staticmethod
    def _is_hk_listed(code: str) -> bool:
        return code.isdigit() and len(code) == 5

    @staticmethod
    def _is_overseas_equity_etf(code: str) -> bool:
        reference = ETF_VALUATION_REFERENCES.get(code)
        return reference is not None and reference.kind in {
            "vanguard_sp500",
            "yahoo_fund",
        }

    def _get(self, url: str, **kwargs: object) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("User-Agent", self.user_agent)
        headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8",
        )
        headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        response = self._http_get(url, headers=headers, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response

    def _mainland_fund_events(
        self, code: str, as_of: date
    ) -> list[dict[str, object]]:
        """Read explicit ex-date and payment-date fields from a fund record."""
        try:
            html = self._get(FUND_DISTRIBUTION_URL.format(code=code)).text
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            logger.info("ETF %s 分红表暂不可用: %s", code, exc)
            return []

        events = []
        for table in soup.find_all("table"):
            headers = [cell.get_text(" ", strip=True) for cell in table.select("th")]
            if not {"除息日", "分红发放日"}.issubset(headers):
                continue
            for row in table.select("tr"):
                cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
                if len(cells) < 5:
                    continue
                ex_date = _as_date(cells[2])
                payment_date = _as_date(cells[4])
                status = _event_status(ex_date, payment_date, as_of)
                if status is None or ex_date is None:
                    continue
                amount_match = re.search(
                    r"现金\s*([0-9]+(?:\.[0-9]+)?)", cells[3]
                )
                per_ten = _finite_positive(
                    amount_match.group(1) if amount_match else None
                )
                events.append(
                    self._event(
                        code=code,
                        ex_date=ex_date,
                        payment_date=payment_date,
                        cash_per_share=per_ten / 10.0 if per_ten is not None else None,
                        currency="CNY",
                        status=status,
                        source="fund_distribution",
                    )
                )
        return events

    def _hk_dividend_events(self, code: str, as_of: date) -> list[dict[str, object]]:
        """Read dated HK distribution schedules, independent of LLM extraction."""
        try:
            html = self._get(
                ETNET_HK_DIVIDEND_URL.format(code=code.lstrip("0") or "0")
            ).text
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            logger.info("港股 %s 分红日历暂不可用: %s", code, exc)
            return []

        events = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [
                cell.get_text(" ", strip=True) for cell in rows[0].find_all("td")
            ]
            if not {"事項", "除淨日", "派送日"}.issubset(headers):
                continue
            event_index = headers.index("事項")
            ex_index = headers.index("除淨日")
            payment_index = headers.index("派送日")
            for row in rows[1:]:
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
                if len(cells) <= max(event_index, ex_index, payment_index):
                    continue
                ex_date = _as_date(cells[ex_index])
                payment_date = _as_date(cells[payment_index])
                status = _event_status(ex_date, payment_date, as_of)
                if status is None or ex_date is None:
                    continue
                amount_match = re.search(
                    r"(?:港元|HKD)\s*([0-9]+(?:\.[0-9]+)?)",
                    cells[event_index],
                    flags=re.IGNORECASE,
                )
                events.append(
                    self._event(
                        code=code,
                        ex_date=ex_date,
                        payment_date=payment_date,
                        cash_per_share=_finite_positive(
                            amount_match.group(1) if amount_match else None
                        ),
                        currency="HKD",
                        status=status,
                        source="hk_dividend_schedule",
                    )
                )
        return events

    def _yahoo_etf_events(self, code: str, as_of: date) -> list[dict[str, object]]:
        """Use Yahoo's public ETF calendar only when both dates are explicit."""
        try:
            result = self._yahoo_quote_summary(code)
            calendar = result.get("calendarEvents", {})
            ex_date = self._yahoo_date(calendar.get("exDividendDate"))
            payment_date = self._yahoo_date(calendar.get("dividendDate"))
            status = _event_status(ex_date, payment_date, as_of)
            if status is None or ex_date is None:
                return []
            return [
                self._event(
                    code=code,
                    ex_date=ex_date,
                    payment_date=payment_date,
                    cash_per_share=self._yahoo_dividend_amount(code, ex_date, as_of),
                    currency=None,
                    status=status,
                    source="yahoo_calendar",
                )
            ]
        except Exception as exc:
            logger.info("海外 ETF %s 分红日历暂不可用: %s", code, exc)
            return []

    def _yahoo_quote_summary(self, code: str) -> dict[str, Any]:
        page = self._get(YAHOO_QUOTE_URL.format(ticker=code)).text
        # Yahoo's Svelte page stores the response JSON as an escaped string in
        # an application/json script.  Parse the raw script body: BeautifulSoup
        # can discard or rewrite this payload before its two JSON layers decode.
        for match in re.finditer(
            r'<script\b[^>]*\btype=["\']application/json["\'][^>]*>'
            r"(.*?)</script>",
            page,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            try:
                outer = json.loads(html_lib.unescape(match.group(1)))
                body = outer.get("body") if isinstance(outer, dict) else None
                if not isinstance(body, str):
                    continue
                result = json.loads(body).get("quoteSummary", {}).get("result", [])
                if result and "calendarEvents" in result[0]:
                    return result[0]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        raise ValueError("Yahoo 未返回 ETF 分红日历")

    @staticmethod
    def _yahoo_date(value: object) -> date | None:
        if isinstance(value, dict):
            raw = value.get("raw")
            if raw is not None:
                try:
                    return datetime.fromtimestamp(float(raw), tz=timezone.utc).date()
                except (TypeError, ValueError, OSError, OverflowError):
                    return None
            value = value.get("fmt")
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
            except (TypeError, ValueError, OSError, OverflowError):
                return None
        return _as_date(value)

    def _yahoo_dividend_amount(
        self, code: str, ex_date: date, as_of: date
    ) -> float | None:
        """A past ex-date has an explicit distribution amount in chart events."""
        if ex_date > as_of:
            return None
        try:
            end = int(
                datetime.combine(as_of, datetime.min.time())
                .replace(tzinfo=timezone.utc)
                .timestamp()
            ) + 86_400
            start = end - int(timedelta(days=367).total_seconds())
            payload = self._get(
                YAHOO_CHART_URL.format(ticker=code),
                params={
                    "period1": start,
                    "period2": end,
                    "interval": "1d",
                    "events": "div,splits",
                },
            ).json()
            dividends = (
                payload.get("chart", {})
                .get("result", [{}])[0]
                .get("events", {})
                .get("dividends", {})
            )
            for item in dividends.values():
                if self._yahoo_date(item.get("date")) == ex_date:
                    return _finite_positive(item.get("amount"))
        except Exception as exc:
            logger.debug("Yahoo %s 分红金额暂不可用: %s", code, exc)
        return None
