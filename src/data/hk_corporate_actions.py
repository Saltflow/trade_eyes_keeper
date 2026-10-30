"""HK dividend history with explicit gaps for unsupported or undated cash terms."""

from __future__ import annotations

import email.utils
import hashlib
import io
import json
import math
import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import islice
from pathlib import Path
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
    r"\s*(?P<currency>港元|HKD|人民幣|RMB|CNY|美元|USD|US\$)\s*(?P<amount>\d+(?:\.\d+)?)"
    r"(?:\s*或\s*(?P<alternate_currency>港元|HKD|人民幣|RMB|CNY|美元|USD|US\$)"
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
            elif primary_currency in {"美元", "USD", "US$"}:
                cash, multiplier, currency = float(primary_amount), 1.0, "USD"
                publication = None
                action_type = "unresolved_cash_dividend"
                diagnostics.append("foreign_cash_requires_official_confirmation")
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
    token = {"HKD": r"(?:HKD|HK\$)", "RMB": r"(?:RMB|CNY)", "USD": r"(?:USD|US\$)"}[
        currency
    ]
    return {
        Decimal(amount) / (10 if unit else 1)
        for amount, unit in re.findall(
            token + r"\s*(\d+(?:\.\d+)?)\s*per\s*(?:(10|ten)\s+)?"
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
        r"Ex[- ]dividend date\s*[:：]?\s*(" + _NAMED_DATE + ")", compact, re.IGNORECASE
    )
    explicit_ex = _named_day(ex_match.group(1)) if ex_match else None
    if explicit_ex is not None and explicit_ex != action.ex_date:
        return None
    payment_dates = set()
    weekday = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    payment_patterns = (
        r"Payment date\s*[:：]?\s*(?:(?:is expected to be|will be|is)\s+)?(?:on\s+)?(" + _NAMED_DATE + ")",
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
    approximate_payment_dates = {
        _named_day(value)
        for value in re.findall(
            r"\b(?:on\s+(?:or\s+)?around|around)\s+"
            + weekday
            + "("
            + _NAMED_DATE
            + ")",
            compact,
            re.IGNORECASE,
        )
    }
    firm_payment_dates = payment_dates - approximate_payment_dates
    firm_payment_dates.update(
        _named_day(value)
        for value in re.findall(
            r"Payment date\s*[:：]?\s*(?:(?:is expected to be|will be|is)\s+)?(?:on\s+)?(" + _NAMED_DATE + ")",
            compact,
            re.IGNORECASE,
        )
    )
    register_dates = {
        _named_day(value)
        for value in re.findall(
            r"\bH\s+share\s+register\s+of\s+members.{0,80}?\bon\s+("
            + _NAMED_DATE
            + ")",
            compact,
            re.IGNORECASE,
        )
    }
    book_end = next(
        (
            _day(item.split("=", 1)[1])
            for item in action.diagnostics
            if item.startswith("book_closed_until=")
        ),
        None,
    )
    record_match = len(register_dates) == 1 and book_end in register_dates
    payable_match = (
        action.payable_date is not None and action.payable_date in payment_dates
    )
    if action.currency == "USD":
        if explicit_ex is None and not payable_match:
            return None
        if action.payable_date is not None and payment_dates and not payable_match:
            raise ValueError(
                f"{action.code}: conflicting official payment date on {action.ex_date}"
            )
    if explicit_ex is None and not payable_match and not record_match:
        return None
    if (
        action.payable_date is not None
        and firm_payment_dates
        and action.payable_date not in firm_payment_dates
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
    if action.currency == "USD":
        form_notice = re.search(
            r"Announcement date\s+(" + _NAMED_DATE + ")", compact, re.IGNORECASE
        )
        if form_notice and _named_day(form_notice.group(1)) > published_at:
            raise ValueError(f"{action.code}: filing predates its stated announcement")
        ordinary_payment_dates = {
            _named_day(value)
            for value in re.findall(
                "(" + _NAMED_DATE + r")\s+for holders of ordinary shares",
                compact,
                re.IGNORECASE,
            )
        }
        if ordinary_payment_dates and action.payable_date not in ordinary_payment_dates:
            raise ValueError(
                f"{action.code}: ordinary-share payment date conflicts with event"
            )
        if is_form:
            usd_text = amount_text
        else:
            # Only this announcement's approval, not EPS, ADS cash or past payments.
            approval = re.search(
                r"(?:board of directors|Board).{0,100}?approved.{0,40}?dividend\b",
                compact,
                re.IGNORECASE,
            )
            definition = re.search(
                r"[“\"][^”\"]{0,30}Dividend[”\"]\s+the dividend of .{0,350}?board of directors.{0,180}?(?:paid|payable).{0,100}?(?:"
                + _NAMED_DATE
                + r")",
                compact,
                re.IGNORECASE,
            )
            if definition:
                usd_text = definition.group(0)
            elif approval:
                usd_text = compact[approval.start() :]
            else:
                return None
            usd_text = re.split(
                r"(?:NetEase|The Company|Company) (?:paid|has paid)|Starting in|Dividend Policy",
                usd_text,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
        usd_amounts = _per_share_amounts(usd_text, "USD")
        if usd_amounts != {Decimal(str(action.cash_per_share))}:
            return None
        result = deepcopy(action)
        result.action_type = "cash_dividend"
        result.published_at = published_at
        result.source = "hkex_dividend_notice"
        result.source_url = source_url
        result.declared_currency = "USD"
        result.declared_cash_per_share = action.cash_per_share
        result.diagnostics = [
            item
            for item in result.diagnostics
            if item != "foreign_cash_requires_official_confirmation"
        ]
        result.diagnostics.extend(
            [
                "hkex_cash_evidence_matched",
                "cash_stock_components_complete",
                "issuer_identity=hkex_title_search"
                if code_match is None
                else "issuer_identity=filing_stock_code",
            ]
        )
        return result
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
    approximate_mismatch = bool(
        record_match and approximate_payment_dates and not payable_match
    )
    if approximate_mismatch:
        # A filing's "around" date is an estimate, not proof that a later
        # ETNet settlement date is false. Neither date is treated as verified.
        result.payable_date = None
    if record_match:
        result.record_date = next(iter(register_dates))
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
            + (
                "ex_date"
                if explicit_ex
                else "payable_date_and_cash"
                if payable_match
                else "record_date_and_cash"
            ),
            "hkex_published_at=" + published_at.isoformat(),
        ]
    )
    if approximate_mismatch:
        result.diagnostics.append("payable_date_unverified_approximate_hkex_notice")
    if conversion:
        result.diagnostics.append(conversion)
    if code_match is None:
        result.diagnostics.append("issuer_identity=hkex_title_search")
    result.diagnostics = list(dict.fromkeys(result.diagnostics))
    return result


_NOTICE_MAX_BYTES = 16 * 1024 * 1024
_NOTICE_MAX_PAGES = 32
_NOTICE_MAX_TEXT_CHARS = 2 * 1024 * 1024


class _BoundedNoticeText(io.StringIO):
    """Stop retaining extracted text as soon as the evidence budget is exceeded."""

    def write(self, text: str) -> int:
        if self.tell() + len(text) > _NOTICE_MAX_TEXT_CHARS:
            raise ValueError("HKEX cash notice exceeds the extracted text limit")
        return super().write(text)


def _notice_retry_delay(exc: Exception) -> float | None:
    """Allow one short retry for transport failures, never parsing/evidence errors."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return 1.0
    if not isinstance(exc, requests.HTTPError):
        return None
    response = exc.response
    if getattr(response, "status_code", None) not in (429, 500, 502, 503, 504):
        return None
    value = (getattr(response, "headers", {}) or {}).get("Retry-After")
    if not value:
        return 1.0
    try:
        delay = float(value)
        if delay < 0:
            return None
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    # Do not hammer a server asking us to wait longer than this bounded retry.
    return max(0.0, delay) if math.isfinite(delay) and delay <= 5.0 else None


class HkexDividendEvidenceProvider:
    """Use bounded official searches to resolve previously unknown HKD dates."""

    def __init__(self, config: dict | None = None, http=None):
        from ..instruments.point_in_time import HkexStatementProvider

        self.provider = HkexStatementProvider(config, http=http)
        settings = (config or {}).get("point_in_time_data", {}) or {}
        self.max_documents = max(
            1, int(settings.get("hkex_dividend_max_documents", 48))
        )
        self._text_cache: OrderedDict[str, str] = OrderedDict()
        self.cache_root = (
            Path(settings.get("output_dir", "data/point_in_time"))
            / "hkex_dividend_sources"
        )

    def _download_notice(self, url: str) -> bytes:
        """Bound response buffering, including absent or misleading length headers."""
        response = self.provider.http.get(
            url,
            headers=self.provider._headers,
            timeout=max(self.provider.timeout, 30),
            stream=True,
        )
        try:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(content) + len(chunk) > _NOTICE_MAX_BYTES:
                    raise ValueError("HKEX cash notice exceeds the document size limit")
                content.extend(chunk)
            return bytes(content)
        finally:
            response.close()

    def _notice_text(self, content: bytes, *, prospectus: bool = False) -> str:
        """Read complete short notices; never treat a truncated filing as evidence.

        Enumerate at most 33 pages and reject long documents before any layout
        extraction. Extract each accepted page once, including implementation
        clauses after page eight, without the financial-statement selector or
        its fallback to a second parser. Byte/page/text limits bound retained
        evidence; they are not a CPU deadline for arbitrary malformed PDFs.
        """
        if prospectus:
            return self._prospectus_text(content, prospectus=True)
        from pdfminer.converter import PDFPageAggregator
        from pdfminer.layout import LAParams, LTTextLine
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdfpage import PDFPage

        if len(content) > _NOTICE_MAX_BYTES:
            raise ValueError("HKEX cash notice exceeds the document size limit")
        with io.BytesIO(content) as stream, _BoundedNoticeText() as output:
            pages = list(
                islice(
                    PDFPage.get_pages(stream, caching=False),
                    _NOTICE_MAX_PAGES + 1,
                )
            )
            if len(pages) > _NOTICE_MAX_PAGES:
                raise ValueError("HKEX cash notice exceeds the 32-page evidence limit")
            manager = PDFResourceManager(caching=False)
            with PDFPageAggregator(
                manager, laparams=LAParams(char_margin=1.0, word_margin=0.1)
            ) as device:
                interpreter = PDFPageInterpreter(manager, device)

                def text_lines(item):
                    if isinstance(item, LTTextLine):
                        yield item
                    else:
                        for child in getattr(item, "_objs", ()):
                            yield from text_lines(child)

                for page in pages:
                    interpreter.process_page(page)
                    # HKEX forms emit labels and values in separate columns.
                    # Preserve the statement extractor's visual-row ordering,
                    # but retain every page and never retry with another parser.
                    line_items = [
                        (
                            float(item.x0),
                            (float(item.y0) + float(item.y1)) / 2.0,
                            str(item.get_text()).strip(),
                        )
                        for item in text_lines(device.get_result())
                        if str(item.get_text()).strip()
                    ]
                    row_groups: list[list[tuple[float, float, str]]] = []
                    for item in sorted(line_items, key=lambda value: -value[1]):
                        if row_groups and abs(item[1] - row_groups[-1][0][1]) <= 1.5:
                            row_groups[-1].append(item)
                        else:
                            row_groups.append([item])
                    for row in row_groups:
                        for item in sorted(row, key=lambda value: value[0]):
                            output.write(item[2])
                            output.write(" ")
                        output.write("\n")
                    output.write("\f")
            return output.getvalue()

    def _prospectus_text(self, content: bytes, *, prospectus: bool = False) -> str:
        """Retain later implementation clauses in small official notices."""
        import pdfplumber

        if len(content) > 16 * 1024 * 1024:
            raise ValueError("HKEX cash notice exceeds the document size limit")
        with pdfplumber.open(io.BytesIO(content)) as reader:
            if len(reader.pages) > 32 and not prospectus:
                raise ValueError("HKEX cash notice exceeds the 32-page evidence limit")
            if prospectus:
                if len(reader.pages) > 1000:
                    raise ValueError("HKEX prospectus exceeds 1000 pages")
                return "\f".join(
                    page.extract_text() or "" for page in reader.pages[:64]
                )
            opening, _ = self.provider._extract_pdf_text(content)
            # The statement extractor intentionally keeps the opening eight
            # pages. AGM notices can put final cash/FX terms on later pages.
            remainder = [page.extract_text() or "" for page in reader.pages[8:]]
        return "\f".join([opening, *remainder])

    def _cached_notice(self, url: str, *, prospectus: bool = False) -> str:
        key = hashlib.sha256(
            (url + ("#opening64" if prospectus else "#bounded-v1")).encode()
        ).hexdigest()
        path = self.cache_root / (key + ".json")
        if path.exists():
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("url") != url or hashlib.sha256(
                item["text"].encode()
            ).hexdigest() != item.get("sha256"):
                raise ValueError("HKEX cached notice integrity mismatch")
            return item["text"]
        content = self._download_notice(url)
        text = (
            self._notice_text(content, prospectus=True)
            if prospectus
            else self._notice_text(content)
        )
        self.cache_root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "url": url,
                    "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "pdf_sha256": hashlib.sha256(content).hexdigest(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return text

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
            records.append(
                {
                    "published_at": published,
                    "url": url,
                    "listed_code": code,
                    "title": link.get_text(" ", strip=True),
                }
            )
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
        failures: dict[str, str] = {}
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
            for title in ("dividend", "poll results", "results"):
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
                        if url not in self._text_cache and url not in failures:
                            if downloaded >= self.max_documents:
                                diagnostic = (
                                    "hkex_cash_evidence_document_budget_exhausted"
                                )
                                if diagnostic not in action.diagnostics:
                                    action.diagnostics.append(diagnostic)
                                break
                            for attempt in range(2):
                                downloaded += 1
                                try:
                                    if not urlsplit(url).path.lower().endswith(".pdf"):
                                        raise ValueError("HKEX non-PDF evidence not parsed")
                                    self._text_cache[url] = self._cached_notice(url)
                                    while len(self._text_cache) > self.max_documents:
                                        self._text_cache.popitem(last=False)
                                    break
                                except Exception as exc:  # noqa: BLE001 -- isolate PDF/HTTP failures
                                    delay = _notice_retry_delay(exc)
                                    if (
                                        attempt == 0
                                        and delay is not None
                                        and downloaded < self.max_documents
                                    ):
                                        # Retries consume the SAME request budget;
                                        # never multiply the maximum network work.
                                        diagnostic = "hkex_cash_evidence_retried=" + url
                                        if diagnostic not in action.diagnostics:
                                            action.diagnostics.append(diagnostic)
                                        time.sleep(delay)
                                        continue
                                    failures[url] = (
                                        "hkex_cash_evidence_unavailable="
                                        + url
                                        + "; "
                                        + type(exc).__name__
                                        + ": "
                                        + str(exc)[:240]
                                    )
                                    break
                        if url in failures:
                            if failures[url] not in action.diagnostics:
                                action.diagnostics.append(failures[url])
                            continue
                        self._text_cache.move_to_end(url)
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
            for action in targets[:]:
                if action.currency != "USD":
                    continue
                announced = next(
                    _day(item.split("=", 1)[1])
                    for item in action.diagnostics
                    if item.startswith("etnet_announcement_date=")
                )
                records = self._announcements(code, announced, action.ex_date, "")
                for record in records:
                    title = record.get("title", "").lower()
                    if not (
                        "prospectus" in title or title == "global offering"
                    ) or not urlsplit(record["url"]).path.lower().endswith(".pdf"):
                        continue
                    if downloaded >= self.max_documents:
                        break
                    downloaded += 1
                    url = record["url"]
                    if url in failures:
                        if failures[url] not in action.diagnostics:
                            action.diagnostics.append(failures[url])
                        continue
                    try:
                        text = self._cached_notice(url, prospectus=True)
                    except Exception as exc:
                        failures[url] = (
                            "hkex_cash_evidence_unavailable=" + url + "; "
                            + type(exc).__name__ + ": " + str(exc)[:240]
                        )
                        action.diagnostics.append(failures[url])
                        continue
                    matched = match_hkex_cash_notice(
                        text,
                        action,
                        published_at=record["published_at"],
                        source_url=record["url"],
                        listed_code=code,
                    )
                    if matched:
                        matched.diagnostics.append(
                            "prospectus_evidence_scope=opening_64_pages"
                        )
                        result[result.index(action)] = matched
                        break
        return result


class HkCorporateActionProvider:
    """Read dated HK history and resolve cash terms against official filings."""

    def __init__(self, config: dict | None = None, http=None):
        self.history = EtnetCorporateActionProvider(config, http=http)
        self.evidence = HkexDividendEvidenceProvider(config, http=http)
        from .corporate_action_currency import FixedCashValuation

        self.fx = FixedCashValuation(config)

    def fetch(self, code: str, start: date, end: date) -> list[CorporateAction]:
        actions = self.evidence.refine(self.history.fetch(code, start, end))
        for index, action in enumerate(actions):
            if action.currency == "USD" and action.published_at is not None:
                actions[index] = self.fx.convert(action)
        return actions
