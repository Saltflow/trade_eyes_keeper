"""Dated issuer announcements from the official HKEX title search."""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


class HkexAnnouncementProvider:
    """Split truncated search windows rather than treating 100 rows as complete."""

    MAX_SEARCHES = 64

    def __init__(self, config: dict | None = None, http=None):
        from ..instruments.point_in_time import HkexStatementProvider

        self.provider = HkexStatementProvider(config, http=http)

    def fetch(self, code: str, start: date, end: date) -> list[dict]:
        if not re.fullmatch(r"\d{5}", code) or start > end:
            raise ValueError(
                f"Invalid HKEX announcement request: {code}, {start}, {end}"
            )
        provider = self.provider
        stock_id = provider._load_stock_ids().get(code)
        if not stock_id:
            raise ValueError(f"HKEX stock identifier not found for {code}")
        pending = [(start, end)]
        records = {}
        searches = 0
        while pending:
            if searches >= self.MAX_SEARCHES:
                raise ValueError(f"{code}: HKEX announcement search limit exceeded")
            first, last = pending.pop()
            response = provider.http.post(
                provider.TITLE_SEARCH_URL,
                data={
                    "lang": "ZH",
                    "market": "SEHK",
                    "searchType": "1",
                    "stockId": stock_id,
                    "category": "0",
                    "documentType": "-1",
                    "t1code": "-2",
                    "t2Gcode": "-2",
                    "t2code": "-2",
                    "from": first.strftime("%Y%m%d"),
                    "to": last.strftime("%Y%m%d"),
                    "MB-Daterange": "0",
                    "title": "",
                },
                headers={**provider._headers, "Accept-Language": "zh-HK,zh;q=0.9"},
                timeout=max(provider.timeout, 45),
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            searches += 1
            rows, total = self._parse(response.text, code, first, last)
            if total > len(rows):
                if first == last:
                    raise ValueError(f"{code}: truncated single-day HKEX announcements")
                middle = first + (last - first) // 2
                pending.extend([(first, middle), (middle + timedelta(days=1), last)])
                continue
            for row in rows:
                records[row["url"]] = row
        return sorted(
            records.values(),
            key=lambda row: (row["published_at"], row["url"]),
            reverse=True,
        )

    def _parse(
        self, source: str, code: str, start: date, end: date
    ) -> tuple[list[dict], int]:
        soup = BeautifulSoup(source, "html.parser")
        count = soup.select_one(".total-records")
        matched = re.search(r"[\d,]+", count.get_text() if count else "")
        if matched is None:
            raise ValueError(f"{code}: HKEX announcement result count missing")
        total = int(matched.group().replace(",", ""))
        records = []
        for row in soup.select("tr"):
            release = row.select_one(".release-time")
            issuer = row.select_one(".stock-short-code")
            link = row.select_one(".doc-link a[href]")
            if release is None and issuer is None and link is None:
                continue
            if release is None or issuer is None or link is None:
                raise ValueError(f"{code}: incomplete HKEX announcement row")
            if code not in re.findall(r"\b\d{5}\b", issuer.get_text(" ", strip=True)):
                raise ValueError(f"{code}: HKEX announcement issuer mismatch")
            released = release.get_text(" ", strip=True)
            published = self.provider._date(released)
            if published is None or not start <= published <= end:
                raise ValueError(f"{code}: invalid HKEX announcement date: {released}")
            clock = re.search(r"\b\d{2}:\d{2}\b", released)
            timestamp = published.isoformat() + (
                f" {clock.group()}:00" if clock else ""
            )
            url = urljoin(self.provider.ROOT_URL, str(link.get("href") or ""))
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"www.hkexnews.hk", "www1.hkexnews.hk"}
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(f"{code}: nonofficial HKEX announcement URL")
            title = link.get_text(" ", strip=True)
            if not title:
                raise ValueError(f"{code}: empty HKEX announcement title")
            headline = row.select_one(".headline")
            records.append(
                {
                    "stock_code": code,
                    "exchange": "hkex",
                    "title": title,
                    "date": published.isoformat(),
                    "published_at": timestamp,
                    "url": url,
                    "type": headline.get_text(" ", strip=True) if headline else "",
                    "summary": title,
                }
            )
        if total and not records:
            raise ValueError(f"{code}: HKEX announcement result rows missing")
        if total < len(records):
            raise ValueError(f"{code}: inconsistent HKEX announcement result count")
        return records, total
