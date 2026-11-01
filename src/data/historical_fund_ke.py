"""Dated official fund valuation references, never current-basket backfills.

Index PEs are benchmark references, not verified fund constituent look-throughs.
CSI ``peg`` is the provider's historical rolling-PE field. Its retrospective
history has no archived revision clock and cannot qualify a promotion trial.
Publication-date-only observations become usable the following calendar day.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

from .etf_valuation import ETF_VALUATION_REFERENCES

CSI_FUND_REFERENCES = {
    code: reference.index_code
    for code, reference in ETF_VALUATION_REFERENCES.items()
    if reference.kind == "csi"
}
CSI_HISTORY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
HSI_CATALOG_URL = (
    "https://www.hsi.com.hk/content/wpb/hsil/en_hk/insights-and-reports.model.json"
)
HSI_INDEX_NAME = "Hang Seng SCHK High Dividend Low Volatility Index"


def _day(value: date | str) -> date:
    return (
        value
        if isinstance(value, date) and not isinstance(value, datetime)
        else date.fromisoformat(str(value)[:10])
    )


def _clock(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation clock must be timezone-aware")
    return value.isoformat()


def _official(url: str, hosts: set[str]) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in hosts
        or parts.username
        or parts.password
    ):
        raise ValueError("source must be a permitted official HTTPS URL")


def _positive(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number > 0 else None


@dataclass(frozen=True)
class FundKeObservation:
    valuation_date: date
    available_from: date
    pe_ttm: float | None
    source_url: str
    source_sha256: str
    observed_at: str
    reference_price: float | None = None
    published_at: str | None = None
    catalog_sha256: str | None = None
    availability_basis: str = "index_eod_next_calendar_day_policy"
    source_period_start: date | None = None
    estimated: bool = False


@dataclass(frozen=True)
class FundKeValue:
    status: str
    ke: float | None
    pe_ttm: float | None
    reason: str
    valuation_basis: str
    reference_code: str | None
    valuation_mode: str = "unavailable"
    source_date: date | None = None
    available_from: date | None = None
    source_url: str | None = None
    source_sha256: str | None = None
    observed_at: str | None = None
    availability_basis: str | None = None
    published_at: str | None = None
    catalog_sha256: str | None = None
    source_period_start: date | None = None
    estimated: bool = False
    vintage_basis: str = "unspecified"
    qualified_for_promotion: bool = False

    def as_dict(self) -> dict:
        return {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class FundKeHistory:
    fund_code: str
    reference_code: str | None
    observations: tuple[FundKeObservation, ...] = ()
    status: str = "available"
    reason: str = ""
    valuation_basis: str = "official_index_reference"
    vintage_basis: str = "provider_retrospective_history_no_archived_revision_clock"
    qualified_for_promotion: bool = False

    def __post_init__(self):
        dates = [row.valuation_date for row in self.observations]
        if dates != sorted(set(dates)):
            raise ValueError("valuation dates must be unique and increasing")
        if any(row.available_from <= row.valuation_date for row in self.observations):
            raise ValueError("date-only historical observations require a causal delay")
        if any(
            row.pe_ttm is not None and _positive(row.pe_ttm) is None
            for row in self.observations
        ):
            raise ValueError("available PE must be positive and finite")

    def at(
        self,
        day: date | str,
        *,
        max_age_days: int = 7,
        reference_price: float | None = None,
        reference_price_as_of: date | str | None = None,
    ) -> FundKeValue:
        """Select only known observations; null latest PE is an explicit barrier.

        With no new reference price this is a *lagged* reference PE, not an
        invented daily exact PE. Repricing requires an observed same-day index
        level in the same index units. Never supply the ETF's trading price.
        """
        day = _day(day)
        if (
            not isinstance(max_age_days, int)
            or isinstance(max_age_days, bool)
            or max_age_days < 0
        ):
            raise ValueError("max_age_days must be a non-negative integer")
        common = {
            "valuation_basis": self.valuation_basis,
            "reference_code": self.reference_code,
            "vintage_basis": self.vintage_basis,
            "qualified_for_promotion": False,
        }
        if self.status == "not_applicable":
            return FundKeValue("not_applicable", None, None, self.reason, **common)
        known = [
            row
            for row in self.observations
            if row.valuation_date <= day and row.available_from <= day
        ]
        if not known:
            return FundKeValue(
                "unavailable",
                None,
                None,
                self.reason or "no_known_historical_pe",
                **common,
            )
        row = known[-1]
        provenance = {
            "source_date": row.valuation_date,
            "available_from": row.available_from,
            "source_url": row.source_url,
            "source_sha256": row.source_sha256,
            "observed_at": row.observed_at,
            "availability_basis": row.availability_basis,
            "published_at": row.published_at,
            "catalog_sha256": row.catalog_sha256,
            "source_period_start": row.source_period_start,
            "estimated": row.estimated,
        }
        reason = (
            "stale_reference"
            if (day - row.valuation_date).days > max_age_days
            else "no_positive_historical_pe"
        )
        if reason == "stale_reference" or row.pe_ttm is None:
            return FundKeValue(
                "unavailable", None, None, reason, **common, **provenance
            )
        pe, mode = row.pe_ttm, "lagged_reference"
        if reference_price is not None:
            if reference_price_as_of is None or _day(reference_price_as_of) != day:
                raise ValueError(
                    "repricing requires a dated same-day reference index price"
                )
            current = _positive(reference_price)
            anchor = _positive(row.reference_price)
            if current is None or anchor is None:
                return FundKeValue(
                    "unavailable",
                    None,
                    None,
                    "missing_reference_price_anchor",
                    **common,
                    **provenance,
                )
            pe = current / (anchor / pe)
            mode = "price_updated_reference"
        return FundKeValue(
            "available",
            0.02 + 1.0 / pe,
            pe,
            "",
            **common,
            **provenance,
            valuation_mode=mode,
        )

    def as_dict(self) -> dict:
        result = asdict(self)
        result["observations"] = [
            {
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(row).items()
            }
            for row in self.observations
        ]
        return result


def unavailable_fund_history(code: str) -> FundKeHistory:
    """Explicit missing states do not substitute yield, FFO, NAV or current PE."""
    if str(code) == "508091":
        return FundKeHistory(
            str(code),
            None,
            status="not_applicable",
            reason="reit_enterprise_pe_not_applicable_no_dividend_or_ffo_substitution",
        )
    return FundKeHistory(
        str(code),
        None,
        status="unavailable",
        reason="no_verified_dated_historical_fund_or_reference_pe",
    )


def parse_csi_index_history(
    body: bytes,
    fund_code: str,
    *,
    source_url: str,
    observed_at: datetime,
    start: date | str,
    end: date | str,
) -> FundKeHistory:
    """Import original official JSON without guessing anonymous column order."""
    fund_code = str(fund_code)
    expected = CSI_FUND_REFERENCES.get(fund_code)
    _official(source_url, {"www.csindex.com.cn"})
    if expected is None or parse_qs(urlsplit(source_url).query).get("indexCode") != [
        expected
    ]:
        raise ValueError("fund/reference identity mismatch")
    if urlsplit(source_url).path != "/csindex-home/perf/index-perf":
        raise ValueError("unexpected CSI historical endpoint")
    start, end = _day(start), _day(end)
    if end < start:
        raise ValueError("historical interval is reversed")
    payload = json.loads(body)
    if str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list):
        raise ValueError("CSI history response is unavailable or malformed")
    digest, clock = hashlib.sha256(body).hexdigest(), _clock(observed_at)
    rows = {}
    for item in payload["data"]:
        if str(item.get("indexCode")) != expected or "peg" not in item:
            raise ValueError("CSI row identity or rolling-PE schema mismatch")
        stamp = str(item.get("tradeDate", ""))
        if not re.fullmatch(r"\d{8}", stamp):
            raise ValueError("CSI valuation date is malformed")
        day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
        if not start <= day <= end:
            continue
        row = FundKeObservation(
            day,
            day + timedelta(days=1),
            _positive(item["peg"]),
            source_url,
            digest,
            clock,
            reference_price=_positive(item.get("close")),
        )
        if day in rows and rows[day] != row:
            raise ValueError("conflicting duplicate valuation date")
        rows[day] = row
    observations = tuple(rows[day] for day in sorted(rows))
    return FundKeHistory(
        fund_code,
        expected,
        observations,
        status="available" if observations else "unavailable",
        reason="" if observations else "empty_official_history",
    )


def _fetch(url, archive_dir, *, http_get, observed_at):
    response = http_get(
        url,
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (TradeEyesKeeper historical valuation)"},
    )
    response.raise_for_status()
    _official(getattr(response, "url", url), {"www.csindex.com.cn", "www.hsi.com.hk"})
    body = response.content
    if archive_dir is not None:
        directory = Path(archive_dir)
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(body).hexdigest()
        (directory / f"{digest}.raw").write_bytes(body)
        (directory / f"{digest}.source.json").write_text(
            json.dumps(
                {
                    "url": url,
                    "observed_at": _clock(observed_at),
                    "sha256": digest,
                    "bytes": len(body),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return body


def fetch_csi_index_history(
    fund_code, start, end, *, archive_dir=None, http_get=requests.get, observed_at=None
):
    expected = CSI_FUND_REFERENCES.get(str(fund_code))
    if expected is None:
        return unavailable_fund_history(str(fund_code))
    observed_at = observed_at or datetime.now(timezone.utc)
    url = (
        CSI_HISTORY_URL
        + "?"
        + urlencode(
            {
                "indexCode": expected,
                "startDate": _day(start).strftime("%Y%m%d"),
                "endDate": _day(end).strftime("%Y%m%d"),
            }
        )
    )
    body = _fetch(url, archive_dir, http_get=http_get, observed_at=observed_at)
    return parse_csi_index_history(
        body,
        str(fund_code),
        source_url=url,
        observed_at=observed_at,
        start=start,
        end=end,
    )


@dataclass(frozen=True)
class MonthlyReport:
    title: str
    period_end: date
    published_at: datetime
    source_url: str
    catalog_sha256: str


def parse_hsi_monthly_catalog(body: bytes) -> tuple[MonthlyReport, ...]:
    """Read publication clocks from HSIL's actual public page model, not filenames."""
    result = {}
    digest = hashlib.sha256(body).hexdigest()

    def visit(item):
        if isinstance(item, dict):
            elements = item.get("elements", {})
            title = elements.get("title", {}).get("value", "")
            match = re.fullmatch(r"Monthly Roundup \(([A-Za-z]+) (\d{4})\)", title)
            if match:
                month = list(calendar.month_name).index(match[1])
                year = int(match[2])
                end = date(year, month, calendar.monthrange(year, month)[1])
                published = datetime.fromisoformat(
                    elements["publishDate"]["value"].replace("Z", "+00:00")
                )
                _clock(published)
                url = urljoin("https://www.hsi.com.hk", elements["url"]["value"])
                _official(url, {"www.hsi.com.hk"})
                if (
                    published.date() <= end
                    or "monthly_roundup/" not in urlsplit(url).path
                ):
                    raise ValueError("monthly report publication or identity mismatch")
                report = MonthlyReport(title, end, published, url, digest)
                if end in result and result[end] != report:
                    raise ValueError("ambiguous monthly report revisions")
                result[end] = report
            for value in item.values():
                visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)

    visit(json.loads(body))
    return tuple(result[day] for day in sorted(result))


def _pdf_text(body: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(body)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def parse_hsi_roundup(
    body: bytes, report: MonthlyReport, *, observed_at: datetime
) -> FundKeObservation:
    """Import the uniquely named HSHYLV row and keep monthly frequency explicit."""
    _official(report.source_url, {"www.hsi.com.hk"})
    if not body.startswith(b"%PDF"):
        raise ValueError("monthly report is not a PDF")
    text = _pdf_text(body)
    # Four price-return columns, then PE, then dividend yield. Never use yield.
    number = r"[-+]?\d+(?:\.\d+)?"
    pattern = (
        re.escape(HSI_INDEX_NAME)
        + r"\s+"
        + (number + r"%\s+") * 4
        + "("
        + number
        + ")"
        + r"\s+"
        + number
        + "%"
    )
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise ValueError("missing or ambiguous HSHYLV PE row")
    # Require the report's declared month in the actual document as well.
    month = report.period_end.strftime("%b")
    if not re.search(
        r"\b" + month + r"(?:[a-z]+)?\s+" + str(report.period_end.year) + r"\b",
        text,
        re.IGNORECASE,
    ):
        raise ValueError("PDF report period does not match the catalog")
    return FundKeObservation(
        report.period_end,
        report.published_at.date() + timedelta(days=1),
        _positive(matches[0]),
        report.source_url,
        hashlib.sha256(body).hexdigest(),
        _clock(observed_at),
        published_at=report.published_at.isoformat(),
        catalog_sha256=report.catalog_sha256,
        availability_basis="official_catalog_publication_next_calendar_day_policy",
    )


def fetch_hsi_monthly_history(
    start, end, *, archive_dir=None, http_get=requests.get, observed_at=None
) -> FundKeHistory:
    observed_at = observed_at or datetime.now(timezone.utc)
    start, end = _day(start), _day(end)
    catalog = _fetch(
        HSI_CATALOG_URL, archive_dir, http_get=http_get, observed_at=observed_at
    )
    observations = []
    for report in parse_hsi_monthly_catalog(catalog):
        if (
            start - timedelta(days=45) <= report.period_end <= end
            and report.published_at.date() <= end
        ):
            body = _fetch(
                report.source_url,
                archive_dir,
                http_get=http_get,
                observed_at=observed_at,
            )
            observations.append(
                parse_hsi_roundup(body, report, observed_at=observed_at)
            )
    return FundKeHistory(
        "520550",
        "HSHYLV",
        tuple(observations),
        status="available" if observations else "unavailable",
        reason="monthly_only_no_daily_exact_pe_or_index_price_anchor",
        vintage_basis="dated_official_monthly_documents_with_catalog_publication_clock",
    )


MULTPL_HISTORY_URL = "https://www.multpl.com/s-p-500-pe-ratio/table/by-month"


def parse_multpl_reference_history(
    body: bytes,
    *,
    source_url: str = MULTPL_HISTORY_URL,
    observed_at: datetime,
    start: date | str,
    end: date | str,
) -> FundKeHistory:
    """Diagnostic third-party S&P monthly PE; never a strict PIT fund valuation.

    A source month-start label is delayed until that entire month has finished.
    This avoids intramonth future values but does not eliminate retrospective
    earnings revisions or prove the original publication date. Both limitations
    remain explicit. The additional current intramonth quote is not imported.
    """
    from bs4 import BeautifulSoup

    parts = urlsplit(source_url)
    if (
        parts.scheme != "https"
        or parts.hostname != "www.multpl.com"
        or parts.path != "/s-p-500-pe-ratio/table/by-month"
        or parts.username
        or parts.password
        or parts.query
    ):
        raise ValueError("unexpected third-party monthly PE source")
    start, end = _day(start), _day(end)
    if end < start:
        raise ValueError("historical interval is reversed")
    clock, digest = _clock(observed_at), hashlib.sha256(body).hexdigest()
    soup = BeautifulSoup(body, "html.parser", from_encoding="utf-8")
    heading = soup.find("h1")
    if (
        heading is None
        or heading.get_text(" ", strip=True).lower() != "s&p 500 pe ratio by month"
    ):
        raise ValueError("not the trailing S&P monthly PE table")
    tables = soup.find_all("table", id="datatable")
    if len(tables) != 1:
        raise ValueError("missing or ambiguous monthly PE table")
    rows = {}
    for tr in tables[0].find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        if len(cells) != 2:
            raise ValueError("monthly PE row shape mismatch")
        match = re.fullmatch(
            r"([A-Za-z]{3}) (\d{1,2}), (\d{4})", cells[0].get_text(" ", strip=True)
        )
        if not match:
            raise ValueError("monthly PE source date is malformed")
        month = list(calendar.month_abbr).index(match[1])
        label = date(int(match[3]), month, int(match[2]))
        if label.day != 1:
            continue  # Current intramonth snapshot is not a completed monthly row.
        period_end = date(label.year, month, calendar.monthrange(label.year, month)[1])
        if not start <= period_end <= end:
            continue
        value_text = cells[1].get_text(" ", strip=True)
        estimated = "†" in value_text or bool(
            cells[1].find("abbr", title=re.compile("estimate", re.IGNORECASE))
        )
        raw_number = value_text.replace("†", "").strip()
        if raw_number not in {"", "--", "N/A"} and not re.fullmatch(
            r"[-+]?\d+(?:\.\d+)?", raw_number
        ):
            raise ValueError("monthly PE value is malformed")
        row = FundKeObservation(
            period_end,
            period_end + timedelta(days=1),
            _positive(raw_number),
            source_url,
            digest,
            clock,
            availability_basis="month_end_plus_one_policy_not_verified_publication",
            source_period_start=label,
            estimated=estimated,
        )
        if period_end in rows and rows[period_end] != row:
            raise ValueError("conflicting monthly PE values")
        rows[period_end] = row
    observations = tuple(rows[day] for day in sorted(rows))
    return FundKeHistory(
        "VOO",
        "SP500",
        observations,
        status="available" if observations else "unavailable",
        reason="diagnostic_monthly_reference_original_publication_and_vintage_unknown",
        valuation_basis="third_party_index_reference",
        vintage_basis="retrospective_uncertain_publication",
    )


def fetch_multpl_reference_history(
    start,
    end,
    *,
    archive_dir=None,
    http_get=requests.get,
    observed_at=None,
) -> FundKeHistory:
    """An independent explicit opt-in; official source allowlists stay unchanged."""
    observed_at = observed_at or datetime.now(timezone.utc)
    response = http_get(
        MULTPL_HISTORY_URL, timeout=25, headers={"User-Agent": "Mozilla/5.0"}
    )
    response.raise_for_status()
    url = getattr(response, "url", MULTPL_HISTORY_URL)
    # Parse and validate the redirect target before accepting or archiving data.
    history = parse_multpl_reference_history(
        response.content, source_url=url, observed_at=observed_at, start=start, end=end
    )
    if archive_dir is not None:
        directory = Path(archive_dir)
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(response.content).hexdigest()
        (directory / f"{digest}.raw").write_bytes(response.content)
        (directory / f"{digest}.source.json").write_text(
            json.dumps(
                {
                    "url": url,
                    "observed_at": _clock(observed_at),
                    "sha256": digest,
                    "bytes": len(response.content),
                    "valuation_basis": history.valuation_basis,
                    "vintage_basis": history.vintage_basis,
                    "qualified_for_promotion": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return history
