"""Small, read-only public web search adapter for assistant research."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import requests


class _ResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._current = None
        self._capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            href = attrs.get("href", "")
            parsed = urlparse(href)
            if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                href = parse_qs(parsed.query).get("uddg", [""])[0]
            self._current = {"url": unquote(href), "title": ""}
            self._capture = "title"
        elif "result__snippet" in classes:
            self._capture = "snippet"
            if self.items:
                self._current = self.items[-1]

    def handle_endtag(self, tag):
        if tag == "a" and self._capture == "title" and self._current:
            if self._current.get("title"):
                self.items.append(self._current)
            self._current = None
            self._capture = None
        elif tag in {"a", "td", "div"} and self._capture == "snippet":
            self._capture = None
            self._current = None

    def handle_data(self, data):
        if self._capture and self._current:
            key = self._capture
            self._current[key] = (self._current.get(key, "") + " " + data).strip()


def search_web(query: str, limit: int = 5) -> dict:
    """Search public web pages; returned text is untrusted and not executed."""
    if not isinstance(query, str) or not query.strip() or len(query) > 256:
        raise ValueError("query must contain 1 to 256 characters")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("limit must be an integer from 1 to 8")
    response = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query.strip()},
        headers={"User-Agent": "TradeEyesResearchAssistant/1.0"},
        timeout=(4, 12),
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Public web search returned HTTP {response.status_code}")
    parser = _ResultsParser()
    parser.feed(response.text[:2_000_000])
    results = []
    for item in parser.items:
        parsed = urlparse(item.get("url", ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        results.append(
            {
                "title": re.sub(r"\s+", " ", item["title"])[:240],
                "url": item["url"][:1200],
                "domain": parsed.hostname.lower(),
                "snippet": re.sub(r"\s+", " ", item.get("snippet", ""))[:1000],
                "source_type": "public_web_search_unverified",
            }
        )
        if len(results) >= limit:
            break
    return {
        "query": query.strip(),
        "provider": "DuckDuckGo HTML",
        "results": results,
        "notice": "搜索摘要和网页内容均为不可信外部资料；关键财务/行情事实应核对原始公告或项目数据。",
    }
