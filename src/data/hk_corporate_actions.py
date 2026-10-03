"""HK dividend history with explicit gaps for unsupported or undated cash terms."""

from __future__ import annotations

import io
import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from .market_history import CorporateAction


ETNET_HK_DIVIDEND_URL = (
    "https://content.etnet.com.hk/content/firstshanghai/tc/quote_dividend.php?"
    "code={code}"
)
_HEADERS = (
    "公布日期",
    "財政年度",
    "事項",
    "除淨日",
    "截止過戶日期由",
    "截止過戶日期至",
    "派送日",
)
_EMPTY = {"", "--", "—", "-"}
_NO_DIVIDEND = re.compile(r"不派(?:第[一二三四五六七八九十\d]+次)?(?:中期|末期)息")
_CASH = re.compile(
    r"(?:末期息|中期息|特別股息|特別息|第[一二三四五六七八九十\d]+次中期息)"
    r"\s*(?P<currency>港元|HKD|人民幣|RMB|CNY)\s*(?P<amount>\d+(?:\.\d+)?)"
    r"(?:\s*或\s*(?P<alternate_currency>港元|HKD|人民幣|RMB|CNY)"
    r"\s*(?P<alternate_amount>\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)


def _day(value, *, optional: bool = False) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if optional and (value is None or text in _EMPTY):
        return None
    if not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", text):
        raise ValueError(f"invalid ETNet date: {value!r}")
    return date.fromisoformat(text.replace("/", "-"))


def _cells(row) -> list[str]:
    return [
        cell.get_text(" ", strip=True)
        for cell in row.find_all(["td", "th"], recursive=False)
    ]


def parse_etnet_dividend_html(
    html: str | bytes,
    code: str,
    start: date,
    end: date,
    *,
    source_url: str | None = None,
) -> list[CorporateAction]:
    """Keep unsupported events visible, and never invent HKD announcement dates.

    ETNet supplies dates for announcements, ex-entitlement and payment, but
    book-closure bounds are not record dates. A dividend originally denominated
    in RMB may have a subsequently added HKD equivalent. Its HKD publication
    date is unknown here, even when the earnings announcement date is present.
    Such cash therefore retains ``published_at=None`` and cannot pass PIT
    readiness. An in-kind distribution keeps both cash/share amounts unknown.
    """
    from .market_history import CorporateAction

    code = str(code).strip()
    if not re.fullmatch(r"\d{5}", code):
        raise ValueError(f"unsupported HK code: {code!r}")
    start, end = _day(start), _day(end)
    if end < start:
        raise ValueError("corporate-action window ends before it starts")
    if isinstance(html, bytes):
        html = html.decode("utf-8-sig")
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if rows and tuple(_cells(rows[0])) == _HEADERS:
            tables.append(rows[1:])
    if len(tables) != 1:
        raise ValueError(f"{code}: absent or ambiguous ETNet dividend table")
    url = source_url or ETNET_HK_DIVIDEND_URL.format(code=code.lstrip("0") or "0")
    oldest_ex_date = None
    selected = {}
    for row in tables[0]:
        cells = _cells(row)
        if len(cells) != len(_HEADERS):
            raise ValueError(f"{code}: incomplete ETNet dividend row")
        announced = _day(cells[0])
        particulars = cells[2]
        ex_date = _day(cells[3], optional=True)
        if _NO_DIVIDEND.fullmatch(particulars):
            if ex_date is not None:
                raise ValueError(f"{code}: no-dividend row has an ex-date")
            continue
        if ex_date is None:
            # An undated announced action might fall in the requested window.
            if announced <= end:
                raise ValueError(f"{code}: undated ETNet distribution: {particulars}")
            continue
        oldest_ex_date = min(oldest_ex_date or ex_date, ex_date)
        if not start <= ex_date <= end:
            continue
        payable = _day(cells[6], optional=True)
        book_start = _day(cells[4], optional=True)
        book_end = _day(cells[5], optional=True)
        if announced > ex_date or (payable is not None and payable < ex_date):
            raise ValueError(f"{code}: inconsistent ETNet dates on {ex_date}")
        if book_start and book_end and book_start > book_end:
            raise ValueError(f"{code}: inverted book-closure dates on {ex_date}")
        diagnostics = [
            "etnet_particulars=" + particulars,
            "etnet_announcement_date=" + announced.isoformat(),
            "etnet_financial_year=" + cells[1],
        ]
        if book_start:
            diagnostics.append("book_closed_from=" + book_start.isoformat())
        if book_end:
            diagnostics.append("book_closed_until=" + book_end.isoformat())
        match = _CASH.fullmatch(particulars)
        cash = None
        multiplier = None
        currency = None
        publication = announced
        action_type = "unsupported_distribution"
        if match:
            primary_currency = match.group("currency").upper()
            alternate_currency = (match.group("alternate_currency") or "").upper()
            primary_amount = Decimal(match.group("amount"))
            alternate_raw = match.group("alternate_amount")
            if primary_amount <= 0 or (
                alternate_raw is not None and Decimal(alternate_raw) <= 0
            ):
                raise ValueError(f"{code}: nonpositive ETNet dividend on {ex_date}")
            if primary_currency in {"港元", "HKD"}:
                if alternate_currency in {"港元", "HKD"}:
                    raise ValueError(f"{code}: ambiguous HKD amounts on {ex_date}")
                cash, multiplier, currency = float(primary_amount), 1.0, "HKD"
                action_type = "cash_dividend"
                diagnostics.append("cash_stock_components_complete")
            else:
                publication = None
                diagnostics.append("hkd_cash_publication_date_unverified")
                if alternate_currency in {"港元", "HKD"}:
                    cash, multiplier, currency = (
                        float(Decimal(alternate_raw)),
                        1.0,
                        "HKD",
                    )
                    action_type = "cash_dividend"
                else:
                    action_type = "unresolved_cash_dividend"
                    diagnostics.append("hkd_cash_amount_missing")
        else:
            diagnostics.append("unsupported_non_cash_or_ambiguous_distribution")
            if re.search(
                r"股派\s*(?:[\d.]+|[一二三四五六七八九十]+)\s*股", particulars
            ) or any(term in particulars for term in ("實物", "ADS")):
                action_type = "in_kind_distribution"
            elif any(term in particulars for term in ("配股", "認購", "供股")):
                action_type = "rights_issue"
        action = CorporateAction(
            code=code,
            action_type=action_type,
            ex_date=ex_date,
            published_at=publication,
            payable_date=payable,
            cash_per_share=cash,
            share_multiplier=multiplier,
            currency=currency,
            source="etnet_dividend_history",
            source_url=url,
            diagnostics=diagnostics,
        )
        existing = selected.get(ex_date)
        if existing is not None:
            compare = (
                "action_type",
                "cash_per_share",
                "share_multiplier",
                "payable_date",
            )
            if (
                any(
                    getattr(existing, field) != getattr(action, field)
                    for field in compare
                )
                or existing.diagnostics[0] != action.diagnostics[0]
            ):
                raise ValueError(
                    f"{code}: ambiguous same-day distributions on {ex_date}"
                )
            if existing.published_at and action.published_at:
                action.published_at = max(existing.published_at, action.published_at)
        selected[ex_date] = action
    if oldest_ex_date is None or oldest_ex_date > start:
        raise ValueError(
            f"{code}: ETNet history does not reach requested start {start}"
        )
    return [selected[key] for key in sorted(selected)]


class EtnetCorporateActionProvider:
    """One bounded historical page request, preserving all unsupported events."""

    def __init__(self, config: dict | None = None, http=None):
        self.http = http or requests.Session()
        settings = (config or {}).get("point_in_time_data", {}) or {}
        market = settings.get("market_history", {}) or {}
        self.timeout = float(market.get("action_timeout_seconds", 20))

    def fetch(self, code: str, start: date, end: date) -> list[CorporateAction]:
        code = str(code).strip()
        if not re.fullmatch(r"\d{5}", code):
            raise ValueError(f"unsupported HK code: {code!r}")
        url = ETNET_HK_DIVIDEND_URL.format(code=code.lstrip("0") or "0")
        response = self.http.get(url, timeout=self.timeout)
        response.raise_for_status()
        return parse_etnet_dividend_html(
            response.content, code, start, end, source_url=url
        )


_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_NAMED_DATE = (
    rf"(?:\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}|(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}})"
)


def _named_day(value: str) -> date:
    text = re.sub(r"\s+", " ", value).strip()
    for pattern in ("%d %B %Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, pattern).date()  # noqa: DTZ007 -- date only
        except ValueError:
            continue
    raise ValueError(f"invalid HKEX announcement date: {value!r}")


def _per_share_amounts(text: str, currency: str) -> set[Decimal]:
    token = r"(?:HKD|HK\$)" if currency == "HKD" else r"(?:RMB|CNY)"
    return {
        Decimal(amount) / (10 if unit else 1)
        for amount, unit in re.findall(
            token + r"\s*(\d+(?:\.\d+)?)\s+per\s+(?:(10|ten)\s+)?"
            r"(?:(?:H|ordinary|existing)\s+)*shares?\b",
            text,
            re.IGNORECASE,
        )
    }


def match_hkex_cash_notice(
    text: str,
    action: CorporateAction,
    *,
    published_at: date,
    source_url: str,
    listed_code: str | None = None,
) -> CorporateAction | None:
    """Match a filing by issuer, dated entitlement and gross cash terms.

    Modern forms identify the ex-date directly. Older implementation notices
    may omit it, in which case their explicit payment date must match ETNet's
    payment date and their unique cash amount must match the same event. For
    RMB-only ETNet entries the filing must confirm the declared RMB cash too.
    """
    if action.action_type not in {"cash_dividend", "unresolved_cash_dividend"}:
        return None
    if urlsplit(source_url).hostname not in {"www.hkexnews.hk", "www1.hkexnews.hk"}:
        raise ValueError("HK cash evidence must come from an official HKEX filing")
    published_at = _day(published_at)
    if published_at > action.ex_date:
        return None
    original_notice = next(
        (
            item.split("=", 1)[1]
            for item in action.diagnostics
            if item.startswith("etnet_announcement_date=")
        ),
        None,
    )
    if original_notice and published_at < _day(original_notice):
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    code_match = re.search(
        r"Stock\s+Code\s*[:：]?\s*(\d{1,5})\b", compact, re.IGNORECASE
    )
    if code_match is not None and code_match.group(1).zfill(5) != action.code:
        return None
    if code_match is None and listed_code != action.code:
        return None
    ex_match = re.search(
        r"Ex[- ]dividend date\s+(" + _NAMED_DATE + ")", compact, re.IGNORECASE
    )
    explicit_ex = _named_day(ex_match.group(1)) if ex_match else None
    if explicit_ex is not None and explicit_ex != action.ex_date:
        return None
    payment_dates = set()
    weekday = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    payment_patterns = (
        r"Payment date\s+(" + _NAMED_DATE + ")",
        r"\b(?:pay|paid|payable|distribute)\b(?:(?![.!?]\s).){0,200}?"
        r"\b(?:on(?:\s+or)?(?:\s+around)?|around)\s+"
        + weekday
        + "("
        + _NAMED_DATE
        + ")",
    )
    for pattern in payment_patterns:
        payment_dates.update(
            _named_day(value) for value in re.findall(pattern, compact, re.IGNORECASE)
        )
    if explicit_ex is None and (
        action.payable_date is None or action.payable_date not in payment_dates
    ):
        return None
    if (
        action.payable_date is not None
        and payment_dates
        and action.payable_date not in payment_dates
    ):
        raise ValueError(
            f"{action.code}: conflicting official payment date on {action.ex_date}"
        )
    is_form = "Cash Dividend Announcement for Equity Issuer" in compact
    amount_text = compact
    if is_form:
        amount_text = compact.split("Information relating to withholding tax", 1)[0]
    particulars = next(
        (
            item.split("=", 1)[1]
            for item in action.diagnostics
            if item.startswith("etnet_particulars=")
        ),
        "",
    )
    expected_terms = _CASH.fullmatch(particulars)
    declared = None
    if expected_terms and expected_terms.group("currency").upper() in {
        "RMB",
        "CNY",
        "人民幣",
    }:
        declared = Decimal(expected_terms.group("amount"))
        if declared not in _per_share_amounts(amount_text, "RMB"):
            return None
    elif action.cash_per_share is None:
        return None
    amounts = _per_share_amounts(amount_text, "HKD")
    conversion = None
    if not amounts and declared is not None:
        rates = {
            Decimal(value)
            for value in re.findall(
                r"(?:applicable )?exchange rate for calculating.{0,100}?"
                r"HK\$\s*1\s*=\s*RMB\s*(\d+(?:\.\d+)?)",
                amount_text,
                re.IGNORECASE,
            )
        }
        if len(rates) == 1 and next(iter(rates)) > 0:
            rate = next(iter(rates))
            amounts = {declared / rate}
            conversion = f"hkd_cash_from_disclosed_rmb_and_fx={declared}/{rate}"
    if len(amounts) != 1:
        return None
    (cash,) = amounts
    if cash <= 0:
        raise ValueError(
            f"{action.code}: invalid official HKD cash on {action.ex_date}"
        )
    if action.cash_per_share is not None and cash != Decimal(
        str(action.cash_per_share)
    ):
        raise ValueError(
            f"{action.code}: conflicting official HKD cash on {action.ex_date}"
        )
    form_notice = re.search(
        r"Announcement date\s+(" + _NAMED_DATE + ")", compact, re.IGNORECASE
    )
    if form_notice and _named_day(form_notice.group(1)) > published_at:
        raise ValueError(f"{action.code}: filing predates its stated announcement")
    result = deepcopy(action)
    result.action_type = "cash_dividend"
    result.cash_per_share = float(cash)
    result.share_multiplier = 1.0
    result.currency = "HKD"
    result.published_at = published_at
    if action.payable_date is None and len(payment_dates) == 1:
        result.payable_date = next(iter(payment_dates))
    if is_form:
        record = re.search(
            r"Record date\s+(" + _NAMED_DATE + ")", compact, re.IGNORECASE
        )
        if record:
            result.record_date = _named_day(record.group(1))
    result.source = "hkex_dividend_notice"
    result.source_url = source_url
    result.diagnostics = [
        item
        for item in result.diagnostics
        if item
        not in {
            "hkd_cash_publication_date_unverified",
            "hkd_cash_amount_missing",
        }
    ]
    result.diagnostics.extend(
        [
            "cash_stock_components_complete",
            "hkex_cash_evidence_matched",
            "hkex_event_match="
            + ("ex_date" if explicit_ex else "payable_date_and_cash"),
            "hkex_published_at=" + published_at.isoformat(),
        ]
    )
    if conversion:
        result.diagnostics.append(conversion)
    if code_match is None:
        result.diagnostics.append("issuer_identity=hkex_title_search")
    result.diagnostics = list(dict.fromkeys(result.diagnostics))
    return result


class HkexDividendEvidenceProvider:
    """Use bounded official searches to resolve previously unknown HKD dates."""

    def __init__(self, config: dict | None = None, http=None):
        from ..instruments.point_in_time import HkexStatementProvider

        self.provider = HkexStatementProvider(config, http=http)
        settings = (config or {}).get("point_in_time_data", {}) or {}
        self.max_documents = max(
            1, int(settings.get("hkex_dividend_max_documents", 48))
        )
        self._text_cache = {}

    def _notice_text(self, content: bytes) -> str:
        """Retain later implementation clauses in small official notices."""
        import pdfplumber

        if len(content) > 16 * 1024 * 1024:
            raise ValueError("HKEX cash notice exceeds the document size limit")
        with pdfplumber.open(io.BytesIO(content)) as reader:
            if len(reader.pages) > 32:
                raise ValueError("HKEX cash notice exceeds the 32-page evidence limit")
            opening, _ = self.provider._extract_pdf_text(content)
            # The statement extractor intentionally keeps the opening eight
            # pages. AGM notices can put final cash/FX terms on later pages.
            remainder = [page.extract_text() or "" for page in reader.pages[8:]]
        return "\f".join([opening, *remainder])

    def _announcements(
        self, code: str, start: date, end: date, title: str
    ) -> list[dict]:
        provider = self.provider
        stock_id = provider._load_stock_ids().get(code)
        if not stock_id:
            raise ValueError(f"HKEX stock identifier not found for {code}")
        response = provider.http.post(
            provider.TITLE_SEARCH_URL,
            data={
                "lang": "EN",
                "market": "SEHK",
                "searchType": "1",
                "stockId": stock_id,
                "category": "0",
                "documentType": "-1",
                "t1code": "-2",
                "t2Gcode": "-2",
                "t2code": "-2",
                "from": start.strftime("%Y%m%d"),
                "to": end.strftime("%Y%m%d"),
                "MB-Daterange": "0",
                "title": title,
            },
            headers=provider._headers,
            timeout=max(provider.timeout, 45),
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for row in soup.select("tr"):
            link = row.select_one(".doc-link a[href]")
            release = row.select_one(".release-time")
            issuer = row.select_one(".stock-short-code")
            if link is None or release is None:
                continue
            listed_codes = (
                re.findall(r"\b\d{5}\b", issuer.get_text(" ", strip=True))
                if issuer
                else []
            )
            if code not in listed_codes:
                raise ValueError(
                    f"{code}: incomplete HKEX issuer identity in search response"
                )
            published = provider._date(release.get_text(" ", strip=True))
            url = urljoin(provider.ROOT_URL, str(link.get("href") or ""))
            if published is None or not start <= published <= end:
                continue
            if urlsplit(url).hostname not in {"www.hkexnews.hk", "www1.hkexnews.hk"}:
                raise ValueError("HKEX search returned a nonofficial document URL")
            records.append({"published_at": published, "url": url, "listed_code": code})
        count = re.search(
            r"Total records found:\s*(\d+)", soup.get_text(" ", strip=True)
        )
        if count is None or int(count.group(1)) != len(records):
            raise ValueError(f"{code}: incomplete HKEX dividend search response")
        return sorted(
            records, key=lambda row: (row["published_at"], row["url"]), reverse=True
        )

    def refine(self, actions: list[CorporateAction]) -> list[CorporateAction]:
        """Return copies, preserving gaps when no causal official match exists."""
        result = deepcopy(actions)
        missing = [
            a
            for a in result
            if a.published_at is None
            and a.action_type
            in {
                "cash_dividend",
                "unresolved_cash_dividend",
            }
        ]
        for code in sorted({action.code for action in missing}):
            targets = [a for a in missing if a.code == code]
            start = min(
                _day(item.split("=", 1)[1])
                for action in targets
                for item in action.diagnostics
                if item.startswith("etnet_announcement_date=")
            )
            end = max(action.ex_date for action in targets)
            downloaded = 0
            for title in ("dividend", "poll results"):
                if not targets:
                    break
                records = self._announcements(code, start, end, title)
                for action in targets[:]:
                    for record in records:
                        published = record["published_at"]
                        announced = next(
                            _day(item.split("=", 1)[1])
                            for item in action.diagnostics
                            if item.startswith("etnet_announcement_date=")
                        )
                        if not announced <= published <= action.ex_date:
                            continue
                        url = record["url"]
                        if url not in self._text_cache:
                            if downloaded >= self.max_documents:
                                break
                            response = self.provider.http.get(
                                url,
                                headers=self.provider._headers,
                                timeout=max(self.provider.timeout, 30),
                            )
                            response.raise_for_status()
                            downloaded += 1
                            self._text_cache[url] = self._notice_text(response.content)
                        try:
                            matched = match_hkex_cash_notice(
                                self._text_cache[url],
                                action,
                                published_at=published,
                                source_url=url,
                                listed_code=record.get("listed_code"),
                            )
                        except ValueError as exc:
                            action.diagnostics.append(
                                "hkex_cash_evidence_conflict=" + str(exc)
                            )
                            targets.remove(action)
                            break
                        if matched is not None:
                            result[result.index(action)] = matched
                            targets.remove(action)
                            break
        return result


class HkCorporateActionProvider:
    """Read dated HK history and resolve cash terms against official filings."""

    def __init__(self, config: dict | None = None, http=None):
        self.history = EtnetCorporateActionProvider(config, http=http)
        self.evidence = HkexDividendEvidenceProvider(config, http=http)

    def fetch(self, code: str, start: date, end: date) -> list[CorporateAction]:
        return self.evidence.refine(self.history.fetch(code, start, end))
