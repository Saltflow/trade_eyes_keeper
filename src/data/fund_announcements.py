"""Official fund disclosure metadata shared by report and research consumers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

import requests
from zoneinfo import ZoneInfo

SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_ORIGIN = "https://www.sse.com.cn"


class FundAnnouncementError(ValueError):
    """The official fund directory is incomplete or has conflicting identities."""


def is_fund_report(item: dict) -> bool:
    """Only periodic financial reports; prospectuses and notices stay separate."""
    title = str(item.get("title", ""))
    if re.search(r"摘要|提示|披露公告", title):
        return False
    return str(item.get("type", "")).removesuffix("/A") in {"N-CSR", "N-CSRS"} or bool(
        re.search(r"年度报告|中期报告|半年度报告|季度报告", title)
    )


class SseFundAnnouncementProvider:
    def __init__(
        self,
        *,
        http_get: Callable = requests.get,
        timeout: float = 20,
        page_size: int = 100,
        max_pages: int = 20,
        archive: Callable | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not 1 <= page_size <= 100 or max_pages < 1:
            raise ValueError("Invalid announcement pagination limits")
        self.http_get = http_get
        self.timeout = timeout
        self.page_size = page_size
        self.max_pages = max_pages
        self.archive = archive
        self.headers = (
            headers
            if headers is not None
            else {"User-Agent": "Mozilla/5.0", "Referer": SSE_ORIGIN + "/"}
        )

    def fetch_disclosures(self, code: str, start: date, end: date) -> list[dict]:
        """Project the shared exchange index into the ordinary disclosure contract."""
        records = []
        for row in self.fetch(code, end):
            published = date.fromisoformat(row["SSEDATE"])
            if not start <= published <= end:
                continue
            url = urljoin(SSE_ORIGIN + "/", row["URL"])
            parsed = urlsplit(url)
            title = re.sub(r"<[^>]*>", "", str(row.get("TITLE", ""))).strip()
            if (
                not title
                or parsed.scheme != "https"
                or parsed.hostname not in {"www.sse.com.cn", "static.sse.com.cn"}
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or not parsed.path.lower().endswith(".pdf")
            ):
                raise FundAnnouncementError("SSE fund disclosure identity invalid")
            records.append(
                {
                    "stock_code": code,
                    "exchange": "sse",
                    "date": published.isoformat(),
                    "title": title,
                    "url": url,
                    "type": row.get("ORG_BULLETIN_TYPE_DESC", ""),
                    "category": row.get("BULLETIN_TYPE_DESC", ""),
                    "official_directory_row": row,
                }
            )
        return sorted(records, key=lambda r: (r["date"], r["url"]), reverse=True)

    def fetch(self, code: str, end: date) -> list[dict]:
        """Read every page for this fund; never substitute another fund's rows."""
        rows, seen_urls = [], set()
        total = None
        for page in range(1, self.max_pages + 1):
            params = {
                "isPagination": "true",
                "pageHelp.pageSize": self.page_size,
                "pageHelp.pageNo": page,
                "pageHelp.beginPage": page,
                "pageHelp.endPage": page,
                "pageHelp.cacheSize": 1,
                "type": "inParams",
                "sqlId": "COMMON_PL_JJXX_JJGG_NEW_L",
                "TITLE": "",
                "SECURITY_CODE": code,
                "BULLETIN_TYPE": "",
                "START_DATE": "",
                "END_DATE": end.isoformat(),
                "DATE_DESC": 1,
            }
            response = self.http_get(
                SSE_QUERY_URL,
                params=params,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            if self.archive:
                self.archive(
                    json.dumps(
                        {"url": SSE_QUERY_URL, "params": params, "response": data},
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8"),
                    "index.json",
                )
            if not isinstance(data, dict) or any(
                data.get(key) for key in ("actionErrors", "fieldErrors")
            ):
                raise FundAnnouncementError("SSE index returned an error")
            pagination = data.get("pageHelp") or {}
            try:
                reported_total = int(pagination["total"])
                returned_page = int(pagination["pageNo"])
            except (TypeError, ValueError, KeyError) as exc:
                raise FundAnnouncementError("SSE pagination metadata missing") from exc
            if returned_page != page or reported_total < 0:
                raise FundAnnouncementError("SSE pagination metadata invalid")
            if total is None:
                total = reported_total
            elif total != reported_total:
                raise FundAnnouncementError("SSE index changed during pagination")
            batch = data.get("result")
            if not isinstance(batch, list) or len(rows) + len(batch) > total:
                raise FundAnnouncementError("SSE index row count is invalid")
            for row in batch:
                if (
                    not isinstance(row, dict)
                    or str(row.get("SECURITY_CODE", "")) != code
                    or not row.get("URL")
                ):
                    raise FundAnnouncementError("SSE index returned a different fund")
                if row["URL"] in seen_urls:
                    raise FundAnnouncementError("SSE repeated a page or document")
                seen_urls.add(row["URL"])
                rows.append(row)
            if len(rows) == total:
                return rows
            if not batch:
                raise FundAnnouncementError("SSE index ended before the declared total")
        raise FundAnnouncementError("SSE index exceeds the pagination limit")


class CninfoFundAnnouncementProvider:
    """One fund identity/pagination adapter for research and report consumers."""

    DIRECTORY_URL = "https://www.cninfo.com.cn/new/data/fund_stock.json"
    QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    STATIC_ROOT = "https://static.cninfo.com.cn/"

    def __init__(self, *, http, timeout: float = 45, max_pages: int = 100):
        self.http, self.timeout, self.max_pages = http, timeout, max_pages
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/",
        }

    def fetch(
        self, code: str, start: date, end: date, *, category: str = ""
    ) -> list[dict]:
        response = self.http.get(
            self.DIRECTORY_URL, headers=self.headers, timeout=self.timeout
        )
        response.raise_for_status()
        directory = response.json()
        if not isinstance(directory, dict) or not isinstance(
            directory.get("stockList"), list
        ):
            raise FundAnnouncementError("cninfo_fund_directory_invalid")
        identities = {
            str(row["orgId"])
            for row in directory["stockList"]
            if isinstance(row, dict)
            and str(row.get("code")) == code
            and row.get("orgId")
        }
        if len(identities) != 1:
            raise FundAnnouncementError("cninfo_fund_identity_missing_or_ambiguous")
        org = identities.pop()
        records, seen, expected_total = [], set(), None
        for page in range(1, self.max_pages + 1):
            response = self.http.post(
                self.QUERY_URL,
                data={
                    "pageNum": page,
                    "pageSize": 50,
                    "column": "fund",
                    "tabName": "fulltext",
                    "stock": f"{code},{org}",
                    "searchkey": "",
                    "category": category,
                    "seDate": f"{start.isoformat()}~{end.isoformat()}",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "false",
                },
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise FundAnnouncementError("cninfo_fund_directory_invalid")
            total, batch = (
                payload.get("totalAnnouncement"),
                payload.get("announcements"),
            )
            if batch is None and total == 0 and payload.get("hasMore") is False:
                batch = []
            if (
                type(total) is not int
                or total < 0
                or not isinstance(batch, list)
                or (expected_total is not None and total != expected_total)
                or len(records) + len(batch) > total
                or len(batch) > 50
                or (not batch and len(records) < total)
                or payload.get("hasMore") is not (len(records) + len(batch) < total)
            ):
                raise FundAnnouncementError("cninfo_fund_pagination_invalid")
            expected_total = total
            digest = hashlib.sha256(response.content).hexdigest()
            for row in batch:
                if not isinstance(row, dict) or str(row.get("secCode")) != code:
                    raise FundAnnouncementError(
                        "cninfo_fund_directory_identity_mismatch"
                    )
                title = re.sub(
                    r"<[^>]*>", "", str(row.get("announcementTitle", ""))
                ).strip()
                adjunct = row.get("adjunctUrl")
                url = urljoin(self.STATIC_ROOT, str(adjunct or ""))
                parsed = urlsplit(url)
                if (
                    not title
                    or not adjunct
                    or url in seen
                    or parsed.scheme != "https"
                    or parsed.hostname != "static.cninfo.com.cn"
                    or parsed.username
                    or parsed.password
                    or parsed.port not in (None, 443)
                    or not parsed.path.lower().endswith(".pdf")
                ):
                    raise FundAnnouncementError(
                        "cninfo_fund_directory_repeated_or_invalid_url"
                    )
                timestamp = row.get("announcementTime")
                if type(timestamp) not in (int, float):
                    raise FundAnnouncementError("cninfo_fund_publication_time_missing")
                published = datetime.fromtimestamp(
                    timestamp / 1000, ZoneInfo("Asia/Shanghai")
                )
                if not start <= published.date() <= end:
                    raise FundAnnouncementError(
                        "cninfo_fund_publication_outside_window"
                    )
                seen.add(url)
                records.append(
                    {
                        "stock_code": code,
                        "exchange": "cninfo",
                        "title": title,
                        "date": published.date().isoformat(),
                        "published_at": published.isoformat(),
                        "url": url,
                        "type": row.get("announcementType", ""),
                        "announcement_id": str(row.get("announcementId") or url),
                        "source_sha256": digest,
                        "official_directory_row": row,
                    }
                )
            if len(records) == total:
                return sorted(
                    records, key=lambda r: (r["date"], r["url"]), reverse=True
                )
        raise FundAnnouncementError("cninfo_fund_pagination_limit_exceeded")
