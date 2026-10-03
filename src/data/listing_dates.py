"""Official, code-matched listing dates for IPO-aware market-history checks."""

from __future__ import annotations

import io
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup

SSE_STOCK_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SSE_FUND_URL = "https://etf.sse.com.cn/fundlist/"
SSE_FUND_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_ELIGIBLE_URL = "https://english.sse.com.cn/access/via/eligible/"
SZSE_STOCK_URL = "https://www.szse.cn/api/report/ShowReport"
_OFFICIAL_PATHS = {
    ("query.sse.com.cn", "/sseQuery/commonQuery.do"),
    ("query.sse.com.cn", "/commonQuery.do"),
    ("etf.sse.com.cn", "/fundlist/"),
    ("english.sse.com.cn", "/access/via/eligible/"),
    ("www.szse.cn", "/api/report/ShowReport"),
}
_OFFICIAL_TIMEOUT = (30, 60)
_OFFICIAL_ATTEMPTS = 2


@dataclass(frozen=True)
class ListingDateEvidence:
    code: str
    listing_date: date
    source_url: str
    retrieved_at: str

    def validate(self, code: str) -> ListingDateEvidence:
        if not re.fullmatch(r"\d{6}", str(code)) or self.code != str(code):
            raise ValueError("listing-date evidence has a different/invalid code")
        url = urlsplit(self.source_url)
        if (
            url.scheme != "https"
            or (url.hostname, url.path) not in _OFFICIAL_PATHS
            or url.username
            or url.password
        ):
            raise ValueError("listing-date evidence has a non-official source")
        source = (url.hostname, url.path)
        if self.code.startswith("5") and source not in {
            ("etf.sse.com.cn", "/fundlist/"),
            ("english.sse.com.cn", "/access/via/eligible/"),
            ("query.sse.com.cn", "/commonQuery.do"),
        }:
            raise ValueError("fund listing evidence must come from SSE fund list")
        if self.code.startswith(("6", "9")) and source != (
            "query.sse.com.cn", "/sseQuery/commonQuery.do"
        ):
            raise ValueError("SSE stock listing evidence has the wrong source")
        if self.code.startswith(("0", "2", "3")) and source != (
            "www.szse.cn", "/api/report/ShowReport"
        ):
            raise ValueError("SZSE stock listing evidence has the wrong source")
        if not isinstance(self.listing_date, date):
            raise TypeError("listing-date evidence has an invalid date")
        if self.listing_date > datetime.now(timezone.utc).date():
            raise ValueError("listing-date evidence is in the future")
        datetime.fromisoformat(self.retrieved_at)
        return self

    def as_dict(self) -> dict[str, str]:
        return {**asdict(self), "listing_date": self.listing_date.isoformat()}

    @classmethod
    def from_dict(cls, value: dict) -> ListingDateEvidence:
        return cls(
            code=str(value["code"]),
            listing_date=date.fromisoformat(str(value["listing_date"])),
            source_url=str(value["source_url"]),
            retrieved_at=str(value["retrieved_at"]),
        )


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return pd.Timestamp(text).date()


def _sse_stock(code: str, session: requests.Session) -> tuple[date, str]:
    params = {
        "STOCK_TYPE": "8" if code.startswith("688") else "1",
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": code,
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": "2,4,5,7,8",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "100",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    response = session.get(
        SSE_STOCK_URL,
        params=params,
        headers={
            "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=_OFFICIAL_TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json().get("result", [])
    matches = [
        row for row in rows if str(row.get("A_STOCK_CODE", "")).strip() == code
    ]
    if len(matches) != 1 or not matches[0].get("LIST_DATE"):
        raise ValueError(f"SSE has no unique official listing date for {code}")
    return _date(matches[0]["LIST_DATE"]), response.url


def _sse_fund(code: str, session: requests.Session) -> tuple[date, str]:
    last_transport_error: requests.RequestException | None = None
    blocked = getattr(session, "_listing_blocked_sources", set())
    for source in (SSE_FUND_QUERY_URL, SSE_ELIGIBLE_URL):
        if source in blocked:
            continue
        try:
            if source == SSE_FUND_QUERY_URL:
                response = session.get(
                    source,
                    params={
                        "sqlId": "COMMON_JJZWZ_JJLB_L",
                        "FUND_CODE": code,
                        "type": "inParams",
                        "isPagination": "true",
                        "pageHelp.pageSize": "10",
                        "pageHelp.pageNo": "1",
                        "pageHelp.beginPage": "1",
                        "pageHelp.endPage": "1",
                    },
                    headers={
                        "Referer": SSE_FUND_URL,
                        "User-Agent": "Mozilla/5.0",
                    },
                    timeout=_OFFICIAL_TIMEOUT,
                )
            else:
                response = session.get(source, timeout=_OFFICIAL_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            if isinstance(exc, requests.HTTPError) and getattr(
                exc.response, "status_code", None
            ) == 403:
                blocked.add(source)
                session._listing_blocked_sources = blocked
            last_transport_error = exc
            continue
        if source == SSE_FUND_QUERY_URL:
            payload = response.json()
            rows = payload.get("result") if isinstance(payload, dict) else None
            page = payload.get("pageHelp") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not isinstance(page, dict):
                raise ValueError("SSE fund list has an invalid response")
            matches = [
                item
                for item in rows
                if isinstance(item, dict) and str(item.get("FUND_CODE")) == code
            ]
            if len(matches) == 1 and str(page.get("total")) == "1":
                listed = matches[0].get("LISTING_DATE")
                if not listed:
                    raise ValueError(f"SSE fund {code} has no listing date")
                return _date(listed), response.url
            if rows or str(page.get("total")) not in {"0", "None"}:
                raise ValueError(f"SSE fund list has no unique match for {code}")
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        matches = []
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
            if code in cells:
                dates = [
                    cell
                    for cell in cells
                    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", cell)
                ]
                matches.extend(dates)
        if len(matches) == 1:
            return _date(matches[0]), response.url
        if matches:
            raise ValueError(f"SSE has conflicting listing dates for {code}")
    if last_transport_error is not None:
        raise last_transport_error
    raise ValueError(f"SSE has no unique official fund listing date for {code}")


def _szse_stock(code: str, session: requests.Session) -> tuple[date, str]:
    response = session.get(
        SZSE_STOCK_URL,
        params={"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"},
        timeout=_OFFICIAL_TIMEOUT,
    )
    response.raise_for_status()
    if len(response.content) > 10 * 1024 * 1024:
        raise ValueError("SZSE stock list exceeds 10 MiB")
    if not response.content.startswith(b"PK\x03\x04"):
        raise ValueError("SZSE stock list is not an XLSX workbook")
    frame = pd.read_excel(io.BytesIO(response.content), engine="openpyxl")
    if not {"A股代码", "A股上市日期"}.issubset(frame.columns):
        raise ValueError("SZSE stock list lacks code or listing-date columns")
    normalized = frame["A股代码"].map(
        lambda value: str(value).split(".")[0].strip().zfill(6)
    )
    matches = frame.loc[normalized == code, "A股上市日期"]
    if len(matches) != 1 or pd.isna(matches.iloc[0]):
        raise ValueError(f"SZSE has no unique official listing date for {code}")
    return _date(matches.iloc[0]), response.url


class ListingDateStore:
    """Cache exact official listing evidence; never infer it from the first bar."""

    def __init__(self, root: str | Path):
        self.root = Path(root) / "listing_dates"

    def _path(self, code: str) -> Path:
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"no official A-share listing route for {code}")
        return self.root / f"{code}.json"

    def read(self, code: str) -> ListingDateEvidence | None:
        path = self._path(code)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return ListingDateEvidence.from_dict(value).validate(code)

    def resolve(self, code: str) -> ListingDateEvidence:
        cached = self.read(code)
        if cached is not None:
            return cached
        with requests.Session() as session:
            for attempt in range(1, _OFFICIAL_ATTEMPTS + 1):
                try:
                    if code.startswith("5"):
                        listing_date, source_url = _sse_fund(code, session)
                    elif code.startswith(("6", "9")):
                        listing_date, source_url = _sse_stock(code, session)
                    elif code.startswith(("0", "2", "3")):
                        listing_date, source_url = _szse_stock(code, session)
                    else:
                        raise ValueError(
                            f"no official A-share listing route for {code}"
                        )
                    break
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == _OFFICIAL_ATTEMPTS:
                        raise
                    time.sleep(2)
                except requests.HTTPError as exc:
                    status = getattr(exc.response, "status_code", None)
                    if attempt == _OFFICIAL_ATTEMPTS or status not in {
                        429, 500, 502, 503, 504
                    }:
                        raise
                    retry_after = (getattr(exc.response, "headers", {}) or {}).get(
                        "Retry-After"
                    )
                    delay = 2.0
                    if retry_after is not None:
                        try:
                            delay = max(0.0, min(60.0, float(retry_after)))
                        except (TypeError, ValueError):
                            pass
                    time.sleep(delay)
        evidence = ListingDateEvidence(
            code=code,
            listing_date=listing_date,
            source_url=source_url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        ).validate(code)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(code)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(evidence.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return evidence
