"""Bounded public evidence retrieval with pinned DNS and shared source policy."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from ..task_runtime.request_policy import RequestPolicy
from .http import access_verification


def public_address(url):
    parsed = urlsplit(url)
    if (
        len(url) > 4096
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or any(ord(c) < 32 for c in url)
    ):
        raise ValueError("public_https_source_required")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("nonpublic_source_address")
    return parsed, ips[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_public(url, authority, *, deadline, cancel, max_bytes=20 * 1024 * 1024):
    """Each redirect is independently resolved, pinned and admitted. No retries."""
    policy = RequestPolicy(authority)
    current = url
    for hop in range(5):
        if cancel.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("evidence_fetch_stopped")
        parsed, address = public_address(current)
        policy.wait(current, deadline=deadline, cancel=cancel)
        connection = _PinnedHTTPS(
            parsed.hostname, address, max(0.1, min(20, deadline - time.monotonic()))
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "TradeEyesResearch/1.0",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or hop == 4:
                    raise ValueError("evidence_redirect_limit")
                current = urljoin(current, location)
                continue
            if response.status in {401, 403, 429}:
                retry = response.getheader("Retry-After") or "60"
                policy.block(
                    current,
                    f"source_http_{response.status}",
                    retry_after=max(60, int(retry))
                    if response.status == 429 and retry.isdigit()
                    else 60
                    if response.status == 429
                    else None,
                )
            if response.status != 200:
                raise ValueError(f"source_http_{response.status}")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("evidence_encoded_body_not_supported")
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError("evidence_size_limit")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if cancel.is_set() or remaining <= 0:
                    raise TimeoutError("evidence_fetch_stopped")
                if connection.sock:
                    connection.sock.settimeout(min(20, remaining))
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("evidence_size_limit")
                chunks.append(chunk)
            if length and size != int(length):
                raise ValueError("evidence_incomplete_body")
            raw = b"".join(chunks)
            if access_verification(raw):
                policy.block(current, "source_access_verification")
                raise ValueError("source_access_verification")
            return raw, {
                "url": current,
                "content_type": response.getheader("Content-Type", ""),
                "last_modified": response.getheader("Last-Modified"),
                "bytes": size,
            }
        finally:
            connection.close()
    raise ValueError("evidence_redirect_limit")


def search_url(query, provider="bing"):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
        raise ValueError("bounded_search_query_required")
    endpoints = {
        "bing": "https://cn.bing.com/search?",
        "duckduckgo": "https://html.duckduckgo.com/html/?",
    }
    if provider not in endpoints:
        raise ValueError("unsupported_search_provider")
    return endpoints[provider] + urlencode({"q": query.strip()})


def search_results(raw):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    results = []
    for item in soup.select("li.b_algo, .result"):
        bing = "b_algo" in item.get("class", [])
        link = item.select_one("h2 a" if bing else ".result__a")
        if not link:
            continue
        base = "https://cn.bing.com" if bing else "https://html.duckduckgo.com"
        url = urljoin(base, link.get("href", ""))
        parsed = urlsplit(url)
        if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"}:
            url = parse_qs(parsed.query).get("uddg", [url])[0]
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname in {
            "bing.com",
            "www.bing.com",
            "cn.bing.com",
            "duckduckgo.com",
            "html.duckduckgo.com",
        }:
            continue
        if any(row["url"] == url for row in results):
            continue
        snippet = item.select_one(".b_caption p, p" if bing else ".result__snippet")
        results.append(
            {
                "title": link.get_text(" ", strip=True)[:300],
                "url": url,
                "snippet": snippet.get_text(" ", strip=True)[:1000] if snippet else "",
                "status": "discovery_only_not_primary_evidence",
            }
        )
        if len(results) == 8:
            break
    return results
