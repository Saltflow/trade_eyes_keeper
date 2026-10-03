"""US history from Nasdaq, with separately evidenced corporate actions.

The public historical endpoint supplies OHLC and volume, not an adjusted-price
or corporate-action contract. A successful HTTP response alone is therefore
insufficient to create a backtest bundle. Prices can be inspected independently;
bundle construction requires complete, dated dividend and split evidence.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urljoin, urlsplit

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

from .market_calendar import _load_calendar
from .market_history import (
    CorporateAction,
    PriceHistoryBundle,
    corporate_action_issues,
)

NASDAQ_HISTORY_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
STATMUSE_MONTH_URL = (
    "https://www.statmuse.com/money/ask/{symbol}-stock-price-in-{month}-{year}"
)
VANGUARD_DISTRIBUTIONS_URL = (
    "https://advisors.vanguard.com/investments/products/api/funds/"
    "{fund_id}/pricing/distributions"
)
DIVIDEND_INVESTOR_HISTORY_URL = "https://www.dividendinvestor.com/pdy/"
DIVIDEND_INVESTOR_NEWS_URL = "https://www.dividendinvestor.com/dividend-news/"
STOCKSCAN_HISTORY_URL = "https://stockscan.io/stocks/{symbol}/dividend-history"
SPLIT_HISTORY_URL = "https://www.splithistory.com/{symbol}/"
PUBLIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
logger = logging.getLogger(__name__)


def _symbol(code: str) -> str:
    value = str(code).strip().upper().replace("/", ".").replace("-", ".")
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?", value):
        raise ValueError(f"invalid US symbol: {code}")
    return value


def _number(value: object, label: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(str(value).strip().replace(",", "").removeprefix("$"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"missing or invalid {label}: {value!r}") from exc
    if not np.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"missing or invalid {label}: {value!r}")
    return number


def _public_url(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
    )


def _sessions(start: date, end: date) -> pd.DatetimeIndex:
    sessions = pd.DatetimeIndex([])
    for year in range(start.year, end.year + 1):
        calendar, _ = _load_calendar("XNYS", year)
        # A year boundary can be a weekend beyond calendar.last_session.
        # Filter the real schedule without asking the calendar to treat that
        # non-session boundary as an in-range session label.
        selected = calendar.sessions[
            (calendar.sessions >= pd.Timestamp(max(start, date(year, 1, 1))))
            & (calendar.sessions <= pd.Timestamp(min(end, date(year, 12, 31))))
        ]
        sessions = sessions.union(selected)
    return sessions


@dataclass(frozen=True)
class CorporateActionEvidence:
    """Caller-audited action coverage, including evidence for no actions.

    An empty actions list does not establish the absence of dividends or splits.
    Both coverage assertions and their public source URLs must be supplied from
    observed evidence. There is deliberately no default empty evidence object.
    """

    code: str
    start: date
    end: date
    actions: tuple[CorporateAction, ...]
    source_urls: tuple[str, ...]
    dividends_complete: bool = False
    splits_complete: bool = False

    def validate(self, code: str, start: date, end: date) -> None:
        if _symbol(self.code) != _symbol(code):
            raise ValueError("corporate action evidence is for a different symbol")
        if self.start > start or self.end < end:
            raise ValueError(
                "corporate action evidence does not cover requested window"
            )
        if not self.dividends_complete or not self.splits_complete:
            raise ValueError("complete dividend and split evidence is required")
        if not self.source_urls or not all(map(_public_url, self.source_urls)):
            raise ValueError("corporate action evidence requires public source URLs")
        for action in self.actions:
            if _symbol(action.code) != _symbol(code):
                raise ValueError("corporate action is for a different symbol")
            if not self.start <= action.ex_date <= self.end:
                raise ValueError("corporate action lies outside evidence window")
        relevant = [a for a in self.actions if start <= a.ex_date <= end]
        issues = corporate_action_issues(relevant)
        if issues:
            raise ValueError("; ".join(issues))
        if any(a.share_multiplier not in (None, 1.0) for a in relevant):
            # Nasdaq's historical endpoint does not declare an unadjusted split
            # basis. Applying another split could double-adjust the same prices.
            raise ValueError(
                "Nasdaq split-bearing history has an unverified price basis"
            )


@dataclass(frozen=True)
class DividendPublication:
    """Observed announcement matched to an official ex-date and cash amount."""

    ex_date: date
    cash_per_share: float
    published_at: date
    source_url: str
    record_date: date | None = None
    payable_date: date | None = None
    response_sha256: str | None = None
    basis: str = "observed_announcement"

    def validate(self, action: CorporateAction) -> None:
        if (
            self.ex_date != action.ex_date
            or not np.isclose(
                self.cash_per_share, action.cash_per_share, rtol=0, atol=1e-6
            )
            or self.published_at > action.ex_date
            or not _public_url(self.source_url)
            or (self.record_date is not None and self.record_date != action.record_date)
            or (
                self.payable_date is not None
                and self.payable_date != action.payable_date
            )
        ):
            raise ValueError(
                f"announcement does not match official distribution: {action.ex_date}"
            )


def _english_date(value: str) -> date:
    value = " ".join(value.replace(".", "").split())
    for pattern in (
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%b-%d-%Y",
        "%B-%d-%Y",
        "%m/%d/%Y",
    ):
        try:
            # These source values are date labels, not instants in a timezone.
            return datetime.strptime(value, pattern).date()  # noqa: DTZ007
        except ValueError:
            pass
    raise ValueError(f"invalid public history date: {value!r}")


def _compact_date(value: str) -> date:
    # date.fromisoformat did not accept YYYYMMDD until Python 3.11.
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


class DividendInvestorPublicationProvider:
    """Match real declaration records or contemporaneous news to issuer cash.

    The public history page carries a limited rolling window. Older audited
    publications may be supplied by the caller, with their original provenance.
    New distributions are discovered from the public history and ticker news
    index, without generating announcement dates or URLs from guessed dates.
    The provider returns only matched records; missing dates remain missing and
    are rejected by the corporate-action contract before a bundle is accepted.
    """

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        self.timeout = int(
            ((config or {}).get("instrument_audit") or {}).get("timeout_seconds", 20)
        )
        self.max_news_articles = 16

    def _read(self, url: str, *, params: dict | None = None):
        response = self.http.get(
            url,
            params=params,
            headers={"User-Agent": PUBLIC_USER_AGENT},
            timeout=self.timeout,
        )
        response.raise_for_status()
        if urlsplit(response.url).hostname != "www.dividendinvestor.com":
            raise ValueError("dividend history redirected to an unexpected host")
        return response

    def fetch(
        self,
        code: str,
        actions: list[CorporateAction],
        *,
        known_publications: Mapping[date, DividendPublication] | None = None,
    ) -> dict[date, DividendPublication]:
        symbol = _symbol(code)
        expected = {}
        for action in actions:
            if _symbol(action.code) != symbol:
                raise ValueError("official distribution is for a different symbol")
            if action.ex_date in expected:
                raise ValueError("multiple official distributions on one ex-date")
            expected[action.ex_date] = action
        if not expected:
            return {}
        result = {}
        for day, publication in (known_publications or {}).items():
            if day in expected:
                publication.validate(expected[day])
                result[day] = publication

        if set(expected) <= set(result):
            return result

        response = self._read(
            DIVIDEND_INVESTOR_HISTORY_URL,
            params={"proj_div_yield": "10", "symbol": symbol},
        )
        for publication in self.parse_history(symbol, response):
            day = publication.ex_date
            if day in expected:
                publication.validate(expected[day])
                # Preserve an independently observed later publication date,
                # which is conservative for point-in-time availability.
                if (
                    day not in result
                    or result[day].published_at < publication.published_at
                ):
                    result[day] = publication

        if set(expected) <= set(result):
            return result
        response = self._read(
            DIVIDEND_INVESTOR_NEWS_URL, params={"symbol": symbol.lower()}
        )
        soup = BeautifulSoup(response.content, "html.parser")
        if f"({symbol})" not in soup.get_text(" ", strip=True):
            raise ValueError("dividend news index does not identify requested symbol")
        visited = set()
        article_count = 0
        for link in soup.find_all("a", href=True):
            url = urljoin(DIVIDEND_INVESTOR_NEWS_URL, link["href"])
            parsed = urlsplit(url)
            if (
                parsed.hostname != "www.dividendinvestor.com"
                or not re.match(r"^/dividend-news/\d{8}/", parsed.path)
                or "declared-a-dividend" not in parsed.path
                or f"-{symbol.lower()}-" not in parsed.path
                or url in visited
            ):
                continue
            visited.add(url)
            article_day = _compact_date(parsed.path.split("/")[2])
            if article_day > max(expected):
                continue
            article_count += 1
            if article_count > self.max_news_articles:
                break
            article = self._read(url)
            try:
                publication = self.parse_article(symbol, article)
            except ValueError as exc:
                logger.warning(f"Discarded inconsistent dividend article {url}: {exc}")
                continue
            if publication.ex_date in expected:
                try:
                    publication.validate(expected[publication.ex_date])
                except ValueError as exc:
                    logger.warning(
                        f"Discarded mismatched dividend article {url}: {exc}"
                    )
                    continue
                result[publication.ex_date] = publication
            if set(expected) <= set(result):
                break
        return result

    @staticmethod
    def parse_history(code: str, response) -> list[DividendPublication]:
        symbol = _symbol(code)
        soup = BeautifulSoup(response.content, "html.parser")
        identity = soup.find(
            "a",
            href=re.compile(rf"/dividend-history-detail/{re.escape(symbol.lower())}/$"),
        )
        if identity is None:
            raise ValueError("dividend history does not identify requested symbol")
        headers = [
            "Year",
            "Declaration Date",
            "Ex-Dividend Date",
            "Record Date",
            "Payable Date",
            "Dividend $ Amount",
        ]
        selected = []
        for table in soup.find_all("table"):
            first = table.find("tr")
            if (
                first
                and [
                    c.get_text(" ", strip=True)
                    for c in first.find_all(["td", "th"], recursive=False)
                ]
                == headers
            ):
                selected.append(table)
        if len(selected) != 1:
            raise ValueError(
                "declared distribution history table is missing or ambiguous"
            )
        result = []
        seen = set()
        digest = hashlib.sha256(response.content).hexdigest()
        for row in selected[0].find_all("tr")[1:]:
            cells = [
                c.get_text(" ", strip=True)
                for c in row.find_all(["td", "th"], recursive=False)
            ]
            if cells and re.fullmatch(r"\d{4} Total\s*:?", cells[0]):
                continue
            if len(cells) != 6:
                raise ValueError("malformed declared distribution history row")
            year, declared, ex_day, record_day, pay_day, cash = cells
            publication = DividendPublication(
                ex_date=_english_date(ex_day),
                cash_per_share=_number(cash, "declared dividend cash"),
                published_at=_english_date(declared),
                source_url=response.url,
                record_date=_english_date(record_day),
                payable_date=_english_date(pay_day),
                response_sha256=digest,
                basis="secondary_history_declaration_date",
            )
            if (
                str(publication.ex_date.year) != year
                or publication.published_at > publication.ex_date
                or publication.record_date < publication.ex_date
                or publication.payable_date < publication.record_date
                or publication.ex_date in seen
            ):
                raise ValueError("inconsistent declared distribution history row")
            seen.add(publication.ex_date)
            result.append(publication)
        if not result:
            raise ValueError("declared distribution history is empty")
        return result

    @staticmethod
    def parse_article(code: str, response) -> DividendPublication:
        symbol = _symbol(code)
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if not title.startswith(f"{symbol} Dividend Announcement "):
            raise ValueError("dividend announcement is a stub or wrong symbol")
        title_date = re.search(r"(\d{1,2}/\d{1,2}/\d{4})$", title)
        url_date = re.search(r"/dividend-news/(\d{8})/", urlsplit(response.url).path)
        if not title_date or not url_date:
            raise ValueError("dividend announcement publication date is missing")
        published = _english_date(title_date.group(1))
        if published != _compact_date(url_date.group(1)):
            raise ValueError("dividend announcement publication dates disagree")
        detail = soup.find("div", class_="rdate")
        if detail is None:
            raise ValueError("dividend announcement lacks dated cash details")
        fields = {}
        for item in detail.get_text("\n", strip=True).splitlines():
            key, separator, value = item.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        try:
            declared = _english_date(fields["Dividend Declaration Date"])
            ex_day = _english_date(fields["Dividend Ex Date"])
            record = _english_date(fields["Dividend Record Date"])
            payable = _english_date(fields["Dividend Payment Date"])
            cash = _number(fields["Dividend Amount"], "announced dividend cash")
        except KeyError as exc:
            raise ValueError("dividend announcement details are incomplete") from exc
        if (
            declared > published
            or published > ex_day
            or (ex_day - published).days > 366
            or record < ex_day
            or payable < record
        ):
            raise ValueError("dividend announcement dates are inconsistent")
        return DividendPublication(
            ex_date=ex_day,
            cash_per_share=cash,
            published_at=published,
            source_url=response.url,
            record_date=record,
            payable_date=payable,
            response_sha256=hashlib.sha256(response.content).hexdigest(),
            basis="contemporaneous_dividend_news",
        )


class StockScanPublicationProvider:
    """Fill older declarations from a complete dated secondary history.

    Every row must match issuer cash, record and payment dates. A long gap
    between a claimed declaration and the ex-date is rejected because the
    current site demonstrably carries stale declaration dates on newer rows.
    """

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        self.timeout = int(
            ((config or {}).get("instrument_audit") or {}).get("timeout_seconds", 20)
        )

    def fetch(
        self, code: str, actions: list[CorporateAction]
    ) -> dict[date, DividendPublication]:
        symbol = _symbol(code)
        if not actions:
            return {}
        expected = {action.ex_date: action for action in actions}
        if len(expected) != len(actions):
            raise ValueError("multiple official distributions on one ex-date")
        url = STOCKSCAN_HISTORY_URL.format(symbol=quote(symbol))
        response = self.http.get(
            url, headers={"User-Agent": PUBLIC_USER_AGENT}, timeout=self.timeout
        )
        response.raise_for_status()
        parsed = urlsplit(response.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "stockscan.io"
            or parsed.path != f"/stocks/{symbol}/dividend-history"
        ):
            raise ValueError("dividend history redirected to an unexpected source")
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if f"({symbol}) Dividend History" not in title:
            raise ValueError("dividend history does not identify requested symbol")
        tables = soup.select("table.prediction-table.dividend-table")
        if len(tables) != 1:
            raise ValueError("declared dividend history table is missing or ambiguous")
        headers = [
            cell.get_text(" ", strip=True) for cell in tables[0].select("thead th")
        ]
        if headers != [
            "Ex/EFF Date",
            "Type",
            "Cash amount",
            "Declaration date",
            "Record date",
            "Payment date",
        ]:
            raise ValueError("declared dividend history has unexpected columns")
        digest = hashlib.sha256(response.content).hexdigest()
        result = {}
        for row in tables[0].select("tbody tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
            if len(cells) != 6 or cells[1] != "CD":
                continue
            try:
                ex_day = _english_date(cells[0])
                if ex_day not in expected:
                    continue
                declared = _english_date(cells[3])
                if not 0 <= (ex_day - declared).days <= 14:
                    raise ValueError("declaration date is implausibly remote")
                publication = DividendPublication(
                    ex_date=ex_day,
                    cash_per_share=_number(cells[2], "declared dividend cash"),
                    published_at=declared,
                    source_url=response.url,
                    record_date=_english_date(cells[4]),
                    payable_date=_english_date(cells[5]),
                    response_sha256=digest,
                    basis="secondary_full_history_declaration_date",
                )
                publication.validate(expected[ex_day])
            except ValueError as exc:
                logger.warning(
                    f"Discarded inconsistent full-history dividend row {symbol} {cells[0]}: {exc}"
                )
                continue
            if ex_day in result:
                raise ValueError("duplicate declaration in secondary dividend history")
            result[ex_day] = publication
        return result


def _berkshire_dividend_year_from_text(text: str) -> int | None:
    """Read the annual-report declaration despite PDF character spacing loss."""
    compact = re.sub(r"\s+", "", text).lower()
    match = re.search(
        r"dividendsberkshirehasnotdeclaredacashdividendsince(\d{4})",
        compact,
    )
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class SplitHistoryEvidence:
    code: str
    start: date
    end: date
    splits: tuple[tuple[date, float], ...]
    source_url: str
    response_sha256: str
    observed_at: datetime

    def require_no_splits(self) -> None:
        if any(self.start <= day <= self.end for day, _ in self.splits):
            raise ValueError(
                "split-bearing Nasdaq history has an unverified price basis"
            )


class SplitHistoryProvider:
    """Read an explicitly counted public split history independently of Yahoo."""

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        self.timeout = int(
            ((config or {}).get("instrument_audit") or {}).get("timeout_seconds", 20)
        )

    def fetch(self, code: str, start: date, end: date) -> SplitHistoryEvidence:
        if end < start or end > datetime.now(timezone.utc).date():
            raise ValueError("split evidence window is invalid or in the future")
        symbol = _symbol(code).replace(".", "")
        url = SPLIT_HISTORY_URL.format(symbol=symbol.lower())
        response = self.http.get(
            url, headers={"User-Agent": PUBLIC_USER_AGENT}, timeout=self.timeout
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if title != f"{symbol} Split History":
            raise ValueError("split history does not identify requested symbol")
        text = soup.get_text(" ", strip=True)
        count = re.search(
            rf"has (\d+) splits? in our {re.escape(symbol)} split history database",
            text,
        )
        if count is None:
            raise ValueError("split history does not state complete record count")
        expected = int(count.group(1))
        tables = [
            t
            for t in soup.find_all("table")
            if not t.find("table")
            and t.get_text(" ", strip=True).startswith(
                f"{symbol} Split History Table Date Ratio"
            )
        ]
        splits = []
        if len(tables) == 1:
            for row in tables[0].find_all("tr")[2:]:
                cells = [
                    c.get_text(" ", strip=True)
                    for c in row.find_all("td", recursive=False)
                ]
                if len(cells) != 2:
                    raise ValueError("split history contains a malformed row")
                ratio = re.fullmatch(r"([0-9.]+) for ([0-9.]+)", cells[1])
                if ratio is None:
                    raise ValueError("split history contains an invalid ratio")
                multiplier = _number(ratio.group(1), "new shares") / _number(
                    ratio.group(2), "old shares"
                )
                splits.append((_english_date(cells[0]), multiplier))
        elif expected != 0 or tables:
            raise ValueError("split history table is missing or ambiguous")
        if len(splits) != expected or len({s[0] for s in splits}) != len(splits):
            raise ValueError("split history record count is incomplete or duplicated")
        return SplitHistoryEvidence(
            code=str(code),
            start=start,
            end=end,
            splits=tuple(splits),
            source_url=response.url,
            response_sha256=hashlib.sha256(response.content).hexdigest(),
            observed_at=datetime.now(timezone.utc),
        )


class NasdaqMarketHistoryProvider:
    """Fetch complete daily OHLC and derive qfq only with verified actions."""

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        audit = (config or {}).get("instrument_audit", {}) or {}
        self.timeout = int(audit.get("timeout_seconds", 20))
        self.page_size = 5000
        self.max_pages = 20
        self.headers = {
            "User-Agent": PUBLIC_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        }

    def fetch_raw(
        self, code: str, start: date, end: date, *, assetclass: str
    ) -> pd.DataFrame:
        """Read reported prices without declaring an action/adjustment contract.

        ``assetclass`` is explicit because the endpoint distinguishes stocks
        from ETFs. The returned frame records source URLs and response hashes
        in ``attrs``. Nothing is persisted by this provider.
        """
        if end < start:
            raise ValueError("history end precedes start")
        if assetclass not in {"stocks", "etf"}:
            raise ValueError("Nasdaq assetclass must be stocks or etf")
        symbol = _symbol(code)
        url = NASDAQ_HISTORY_URL.format(symbol=quote(symbol, safe="."))
        offset = 0
        total: int | None = None
        records: list[dict] = []
        repairs: list[dict] = []
        seen_dates: set[date] = set()
        pages: list[dict] = []
        for _ in range(self.max_pages):
            params = {
                "assetclass": assetclass,
                "fromdate": start.isoformat(),
                "todate": end.isoformat(),
                "limit": self.page_size,
                "offset": offset,
            }
            response = self.http.get(
                url, params=params, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise TypeError("invalid Nasdaq response object")
            status = body.get("status") or {}
            if str(status.get("rCode", 200)) != "200":
                raise ValueError(f"Nasdaq API error: {status}")
            data = body.get("data")
            if data is None:
                raise ValueError(f"Nasdaq history unavailable: {body.get('message')}")
            if not isinstance(data, dict):
                raise TypeError("invalid Nasdaq history data object")
            if _symbol(data.get("symbol", "")) != symbol:
                raise ValueError("Nasdaq response symbol does not match request")
            try:
                count = int(str(data["totalRecords"]).replace(",", ""))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Nasdaq totalRecords is missing or invalid") from exc
            if count <= 0:
                raise ValueError(f"empty Nasdaq history for {code}")
            if total is not None and total != count:
                raise ValueError("Nasdaq totalRecords changed during pagination")
            total = count
            rows = (data.get("tradesTable") or {}).get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValueError("Nasdaq pagination ended before totalRecords")
            for row in rows:
                try:
                    parsed = self._price_row(row)
                except ValueError as exc:
                    if not str(exc).startswith("Nasdaq OHLC relationship is invalid"):
                        raise
                    parsed, repair = self._repair_ohlc_row(symbol, row)
                    repairs.append(repair)
                day = parsed["date"]
                if not start <= day <= end:
                    raise ValueError(
                        f"Nasdaq returned a date outside the window: {day}"
                    )
                if day in seen_dates:
                    raise ValueError(f"Nasdaq repeated a date during pagination: {day}")
                seen_dates.add(day)
                records.append(parsed)
            pages.append(
                {
                    "url": response.url,
                    "offset": offset,
                    "rows": len(rows),
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                }
            )
            offset += len(rows)
            if offset > total:
                raise ValueError("Nasdaq returned more rows than totalRecords")
            if offset == total:
                break
        else:
            raise ValueError("Nasdaq pagination exceeded the bounded page limit")
        frame = pd.DataFrame(records).sort_values("date").reset_index(drop=True)
        frame["date"] = pd.to_datetime(frame["date"])
        expected = _sessions(start, end)
        actual = pd.DatetimeIndex(frame["date"])
        missing = expected.difference(actual)
        extra = actual.difference(expected)
        if len(missing) or len(extra):
            raise ValueError(
                "Nasdaq session coverage mismatch: "
                f"missing={len(missing)} {missing.strftime('%Y-%m-%d').tolist()[:5]}; "
                f"unexpected={extra.strftime('%Y-%m-%d').tolist()[:5]}"
            )
        frame.attrs = {
            "source": "nasdaq_historical",
            "currency": "USD",
            "code": str(code),
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "total_records": total,
            "pages": pages,
            "ohlc_repairs": repairs,
            "adjustment_contract": "requires_independent_action_evidence",
        }
        return frame

    def _repair_ohlc_row(self, symbol: str, row: dict) -> tuple[dict, dict]:
        """Replace one impossible OHLC field only when a second feed agrees."""
        original = self._price_row(row, validate_ohlc=False)
        day = original["date"]
        url = STATMUSE_MONTH_URL.format(
            symbol=quote(symbol.lower(), safe="."),
            month=day.strftime("%B").lower(),
            year=day.year,
        )
        try:
            response = self.http.get(
                url, headers={"User-Agent": PUBLIC_USER_AGENT}, timeout=self.timeout
            )
            response.raise_for_status()
            source = urlsplit(response.url)
            if (
                source.scheme != "https"
                or source.hostname != "www.statmuse.com"
                or source.path != urlsplit(url).path
            ):
                raise ValueError("unexpected independent OHLC source")
            soup = BeautifulSoup(response.content, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if not title.casefold().startswith(f"{symbol} Stock Price In ".casefold()):
                raise ValueError("independent OHLC page has the wrong symbol")
            matching = []
            for table in soup.find_all("table"):
                headers = [
                    cell.get_text(" ", strip=True) for cell in table.find_all("th")
                ]
                if headers != ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]:
                    continue
                for tr in table.find_all("tr"):
                    cells = [
                        cell.get_text(" ", strip=True) for cell in tr.find_all("td")
                    ]
                    if len(cells) != 6:
                        continue
                    try:
                        candidate_day = _english_date(cells[0])
                    except ValueError:
                        continue
                    if candidate_day == day:
                        matching.append(cells)
            if len(matching) != 1:
                raise ValueError("independent OHLC date is missing or duplicated")
            cells = matching[0]
            independent = {
                f"raw_{key}": _number(cells[index], f"independent {key}")
                for index, key in enumerate(("open", "high", "low", "close"), 1)
            }
            volume = _number(cells[5], "independent volume", allow_zero=True)
            if abs(volume - original["volume"]) > max(1, original["volume"] * 0.01):
                raise ValueError("independent OHLC volume differs too much")
            invalid_high = original["raw_high"] < max(
                original["raw_open"], original["raw_close"]
            )
            invalid_low = original["raw_low"] > min(
                original["raw_open"], original["raw_close"]
            )
            if invalid_high == invalid_low:
                raise ValueError("ambiguous OHLC correction")
            corrected = "raw_high" if invalid_high else "raw_low"
            for field in ("raw_open", "raw_high", "raw_low", "raw_close"):
                if field == corrected:
                    continue
                if not np.isclose(
                    original[field], independent[field], rtol=0, atol=0.01
                ):
                    raise ValueError(f"independent OHLC disagrees on {field}")
            repaired = {**original, corrected: independent[corrected]}
            if repaired["raw_high"] < max(
                repaired["raw_open"], repaired["raw_close"]
            ) or repaired["raw_low"] > min(repaired["raw_open"], repaired["raw_close"]):
                raise ValueError("independent OHLC does not repair the price bar")
        except (requests.RequestException, ValueError, IndexError) as exc:
            raise ValueError(
                f"Nasdaq OHLC relationship is invalid: {day}; "
                f"independent correction unavailable: {exc}"
            ) from exc
        repair = {
            "date": day.isoformat(),
            "field": corrected,
            "nasdaq_value": original[corrected],
            "verified_value": independent[corrected],
            "source_url": response.url,
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
        }
        logger.warning(
            f"Repaired invalid Nasdaq {symbol} {day} {corrected} using {response.url}"
        )
        return repaired, repair

    @staticmethod
    def _price_row(row: dict, *, validate_ohlc: bool = True) -> dict:
        try:
            month, day_number, year = row["date"].split("/")
            day = date(int(year), int(month), int(day_number))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Nasdaq row has an invalid date") from exc
        values = {
            f"raw_{key}": _number(row.get(key), f"{day} {key}")
            for key in ("open", "high", "low", "close")
        }
        volume = _number(row.get("volume"), f"{day} volume", allow_zero=True)
        if not volume.is_integer():
            raise ValueError(f"Nasdaq row has fractional share volume: {day}")
        if validate_ohlc and (
            values["raw_high"] < max(values["raw_open"], values["raw_close"])
            or values["raw_low"] > min(values["raw_open"], values["raw_close"])
        ):
            raise ValueError(f"Nasdaq OHLC relationship is invalid: {day}")
        return {"date": day, **values, "volume": volume, "tradable": volume > 0}

    def fetch(
        self,
        code: str,
        start: date,
        end: date,
        *,
        assetclass: str,
        action_evidence: CorporateActionEvidence | None = None,
    ) -> PriceHistoryBundle:
        if action_evidence is None:
            raise ValueError(
                "Nasdaq history requires independent corporate action evidence"
            )
        action_evidence.validate(code, start, end)
        frame = self.fetch_raw(code, start, end, assetclass=assetclass)
        actions = [a for a in action_evidence.actions if start <= a.ex_date <= end]
        factors = np.ones(len(frame), dtype=float)
        cash_by_day: dict[date, float] = {}
        for action in actions:
            cash_by_day[action.ex_date] = cash_by_day.get(action.ex_date, 0.0) + (
                action.cash_per_share or 0.0
            )
        for ex_date, cash in sorted(cash_by_day.items()):
            before = frame["date"].dt.date < ex_date
            if not before.any():
                continue
            previous_close = float(frame.loc[before, "raw_close"].iloc[-1])
            if cash >= previous_close:
                raise ValueError(
                    f"cash distribution is not price-adjustable: {ex_date}"
                )
            factors[before.to_numpy()] *= 1.0 - cash / previous_close
        frame["qfq_factor"] = factors
        for key in ("open", "high", "low", "close"):
            frame[f"qfq_{key}"] = frame[f"raw_{key}"] * frame["qfq_factor"]
        return PriceHistoryBundle(
            code=str(code),
            prices=frame,
            actions=actions,
            source="nasdaq_historical_with_verified_actions",
            currency="USD",
            diagnostics=[
                "price_pages:" + json.dumps(frame.attrs["pages"], sort_keys=True),
                "ohlc_repairs:"
                + json.dumps(frame.attrs["ohlc_repairs"], sort_keys=True),
                "action_evidence:" + json.dumps(action_evidence.source_urls),
                "qfq_method:cash_ex_date_previous_raw_close_latest_factor_one",
            ],
        ).validate()


class VanguardDistributionProvider:
    """Official cash/ex-date history; never invent an announcement date."""

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        self.timeout = int(
            ((config or {}).get("instrument_audit") or {}).get("timeout_seconds", 20)
        )

    def fetch(
        self,
        code: str,
        fund_id: str,
        start: date,
        end: date,
        *,
        publications: Mapping[date, DividendPublication] | None = None,
    ) -> list[CorporateAction]:
        if not re.fullmatch(r"\d{4}", str(fund_id)):
            raise ValueError("Vanguard fund_id must be an explicit four-digit fund ID")
        if end < start:
            raise ValueError("distribution end precedes start")
        url = VANGUARD_DISTRIBUTIONS_URL.format(fund_id=fund_id)
        response = self.http.get(
            url,
            params={"hasDistributionYield": "false"},
            headers={"User-Agent": PUBLIC_USER_AGENT, "Accept": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError("Vanguard distribution history is empty or invalid")
        actions = []
        seen: set[tuple[date, str]] = set()
        for row in rows:
            try:
                ex_date = date.fromisoformat(row["exDividendDate"])
                record_date = date.fromisoformat(row["recordDate"])
                payable_date = date.fromisoformat(row["payableDate"])
                kind = row["typeCode"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Vanguard distribution has invalid dates/type"
                ) from exc
            if not start <= ex_date <= end:
                continue
            if kind not in {"INC", "CGST", "CGLT", "ROC"}:
                raise ValueError(f"unsupported Vanguard distribution type: {kind}")
            key = (ex_date, kind)
            if key in seen:
                raise ValueError(f"duplicate Vanguard distribution: {key}")
            seen.add(key)
            cash = _number(row.get("amount"), "Vanguard cash distribution")
            diagnostics = [f"vanguard_distribution_type:{kind}"]
            action = CorporateAction(
                code=str(code),
                action_type="cash_dividend",
                ex_date=ex_date,
                record_date=record_date,
                payable_date=payable_date,
                cash_per_share=cash,
                currency="USD",
                source="vanguard_distribution_history",
                source_url=response.url,
                diagnostics=diagnostics,
            )
            publication = (publications or {}).get(ex_date)
            if publication is not None:
                publication.validate(action)
                action.published_at = publication.published_at
                action.diagnostics.extend(
                    [
                        f"publication_evidence:{publication.source_url}",
                        f"publication_basis:{publication.basis}",
                    ]
                )
                if publication.response_sha256:
                    action.diagnostics.append(
                        f"publication_response_sha256:{publication.response_sha256}"
                    )
            else:
                action.diagnostics.append(
                    "publication_date_unavailable_in_vanguard_feed"
                )
            actions.append(action)
        return sorted(actions, key=lambda action: action.ex_date)


class USDisclosedMarketHistoryProvider:
    """Nasdaq prices plus issuer actions and persistent publication evidence.

    Cache files live under the configured PIT output directory. Every accepted
    publication retains the public response bytes and their digest; a later
    issuer event must acquire its own matching evidence. A previous end date is
    never silently treated as proof for a later, unobserved corporate action.
    """

    def __init__(self, config: dict | None = None, http=None):
        self.config = config or {}
        self.http = http or requests.Session()
        settings = self.config.get("point_in_time_data") or {}
        market = settings.get("market_history") or {}
        self.cache_dir = Path(
            market.get(
                "us_publication_cache_dir",
                str(
                    Path(settings.get("output_dir", "data/point_in_time"))
                    / "us_publications"
                ),
            )
        )
        self.fund_ids = market.get("vanguard_fund_ids") or {}
        self.timeout = int(
            (self.config.get("instrument_audit") or {}).get("timeout_seconds", 20)
        )
        self.records: dict[str, str] = {}

    def get(self, url: str, **kwargs):
        """Record public source bytes for reproducible action/price provenance."""
        response = self.http.get(url, **kwargs)
        response.raise_for_status()
        digest = hashlib.sha256(response.content).hexdigest()
        folder = self.cache_dir / "responses"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{digest}.bin"
        if not path.exists():
            path.write_bytes(response.content)
        self.records[response.url] = digest
        return response

    def _read_public(self, url: str):
        return self.get(
            url, headers={"User-Agent": PUBLIC_USER_AGENT}, timeout=self.timeout
        )

    def _fund_id(self, code: str) -> str:
        if code in self.fund_ids:
            value = str(self.fund_ids[code])
            if not re.fullmatch(r"\d{4}", value):
                raise ValueError("configured Vanguard fund_id must have four digits")
            return value
        response = self._read_public(
            "https://investor.vanguard.com/investment-products/etfs/profile/"
            + code.lower()
        )
        soup = BeautifulSoup(response.content, "html.parser")
        node = soup.find(attrs={"data-vgn-funds-profile": True})
        if node is None:
            raise ValueError("Vanguard issuer metadata is unavailable for symbol")
        overview = json.loads(node["data-vgn-funds-profile"]).get("overview") or {}
        if overview.get("iovTickerSymbol") != f"{code}.P":
            raise ValueError("Vanguard issuer metadata does not match requested symbol")
        value = str(overview.get("fundId", ""))
        if not re.fullmatch(r"\d{4}", value):
            raise ValueError("Vanguard issuer metadata lacks a valid fund ID")
        return value

    def _load_publications(self, code: str) -> dict[date, DividendPublication]:
        path = self.cache_dir / f"{code}.json"
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or payload.get("code") != code:
            raise ValueError("publication cache identity/schema mismatch")
        result = {}
        for row in payload.get("publications", []):
            values = dict(row)
            for field in ("ex_date", "published_at", "record_date", "payable_date"):
                values[field] = (
                    date.fromisoformat(values[field]) if values.get(field) else None
                )
            publication = DividendPublication(**values)
            digest = publication.response_sha256 or ""
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("publication cache lacks source response digest")
            source = self.cache_dir / "responses" / f"{digest}.bin"
            if (
                not source.exists()
                or hashlib.sha256(source.read_bytes()).hexdigest() != digest
            ):
                raise ValueError(
                    "publication cache source bytes are missing or changed"
                )
            if publication.ex_date in result:
                raise ValueError("publication cache has duplicate ex-date")
            result[publication.ex_date] = publication
        return result

    def _save_publications(self, code: str, publications) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{code}.json"
        payload = {
            "schema_version": 1,
            "code": code,
            "publications": [asdict(p) for _, p in sorted(publications.items())],
        }
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _berkshire_no_dividends(self, code: str, start: date) -> tuple[str, ...]:
        """Corroborate issuer's all-history statement with a fresh second source.

        This is an issuer-specific reader, not a hardcoded zero-dividend flag.
        Removal of either statement fails closed, including after a first cash
        distribution changes the company's policy or its recorded history.
        """
        import pdfplumber

        reports = self._read_public("https://www.berkshirehathaway.com/reports.html")
        soup = BeautifulSoup(reports.content, "html.parser")
        years = []
        for link in soup.find_all("a", href=True):
            match = re.fullmatch(r"(\d{4})ar/linksannual\d{2}\.html", link["href"])
            if match:
                years.append(int(match.group(1)))
        if not years:
            raise ValueError("Berkshire annual report index is unavailable")
        year = max(years)
        annual_url = f"https://www.berkshirehathaway.com/{year}ar/{year}ar.pdf"
        annual = self._read_public(annual_url)
        if not annual.content.startswith(b"%PDF"):
            raise ValueError("Berkshire annual report is not a PDF")
        last_dividend_year = None
        identity_seen = False
        with pdfplumber.open(io.BytesIO(annual.content)) as document:
            for page in document.pages:
                text = " ".join((page.extract_text() or "").split())
                identity_seen = identity_seen or "BRK.B" in text
                year = _berkshire_dividend_year_from_text(text)
                if year is not None and identity_seen:
                    last_dividend_year = year
                    break
        if last_dividend_year is None or start.year <= last_dividend_year:
            raise ValueError(
                "issuer no-dividend statement does not cover requested history"
            )
        current = self._read_public(
            "https://companiesmarketcap.com/berkshire-hathaway/dividends/"
        )
        current_soup = BeautifulSoup(current.content, "html.parser")
        title = (
            current_soup.title.get_text(" ", strip=True) if current_soup.title else ""
        )
        text = current_soup.get_text(" ", strip=True)
        if (
            "Berkshire Hathaway" not in title
            or "(BRK-B)" not in title
            or "We have found no dividend history for this company" not in text
        ):
            raise ValueError(
                "independent current dividend history no longer confirms absence"
            )
        return annual.url, current.url

    def fetch(self, code: str, start: date, end: date) -> PriceHistoryBundle:
        symbol = _symbol(code)
        if end < start or end > datetime.now(timezone.utc).date():
            raise ValueError("US history window is invalid or in the future")
        self.records = {}
        split_proof = SplitHistoryProvider(self.config, http=self).fetch(
            symbol, start, end
        )
        split_proof.require_no_splits()
        if symbol in {"BRK.A", "BRK.B"}:
            if symbol != "BRK.B":
                # The current corroborating page identifies Class B only.
                raise ValueError(
                    "independent dividend evidence does not identify this share class"
                )
            sources = self._berkshire_no_dividends(symbol, start)
            actions: list[CorporateAction] = []
            assetclass = "stocks"
        else:
            fund_id = self._fund_id(symbol)
            issuer_actions = VanguardDistributionProvider(self.config, http=self).fetch(
                symbol, fund_id, date.min, end
            )
            if not issuer_actions or min(a.ex_date for a in issuer_actions) > start:
                raise ValueError(
                    "Vanguard distribution history does not establish requested start coverage"
                )
            actions = [a for a in issuer_actions if start <= a.ex_date <= end]
            known = self._load_publications(symbol)
            publications = DividendInvestorPublicationProvider(
                self.config, http=self
            ).fetch(
                symbol,
                actions,
                known_publications=known,
            )
            missing_actions = [
                action for action in actions if action.ex_date not in publications
            ]
            if missing_actions:
                publications.update(
                    StockScanPublicationProvider(self.config, http=self).fetch(
                        symbol, missing_actions
                    )
                )
            known.update(publications)
            self._save_publications(symbol, known)
            for action in actions:
                publication = publications.get(action.ex_date)
                if publication is not None:
                    publication.validate(action)
                    action.published_at = publication.published_at
                    action.diagnostics.extend(
                        [
                            f"publication_evidence:{publication.source_url}",
                            f"publication_basis:{publication.basis}",
                            f"publication_response_sha256:{publication.response_sha256}",
                        ]
                    )
            problems = corporate_action_issues(actions)
            if problems:
                raise ValueError(
                    "US publication evidence incomplete: " + "; ".join(problems)
                )
            sources = tuple(
                sorted(
                    {a.source_url for a in issuer_actions if a.source_url}
                    | {p.source_url for p in publications.values()}
                )
            )
            assetclass = "etf"
        proof = CorporateActionEvidence(
            code=str(code),
            start=start,
            end=end,
            actions=tuple(actions),
            source_urls=(*sources, split_proof.source_url),
            dividends_complete=True,
            splits_complete=True,
        )
        bundle = NasdaqMarketHistoryProvider(self.config, http=self).fetch(
            code,
            start,
            end,
            assetclass=assetclass,
            action_evidence=proof,
        )
        bundle.diagnostics.extend(
            [
                "corporate_action_source_responses:"
                + json.dumps(self.records, sort_keys=True),
                "split_evidence_observed_at:" + split_proof.observed_at.isoformat(),
            ]
        )
        return bundle
