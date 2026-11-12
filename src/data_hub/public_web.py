"""Bounded public evidence retrieval with pinned DNS and shared source policy."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlencode, urljoin, urlsplit

from urllib3.exceptions import HTTPError

from ..task_runtime.request_policy import RequestPolicy
from .http import AuthorityHTTP, access_verification

SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"


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


def search_url(query, limit=5):
    """Public provenance/cache identity; the actual API request is POST."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
        raise ValueError("bounded_search_query_required")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("bounded_search_count_required")
    return SEARCH_ENDPOINT + "?" + urlencode({"query": query.strip(), "count": limit})


def search_public(query, authority, *, api_key, limit=5, deadline, cancel):
    """One authenticated DS Search call, using the existing request policy."""
    identity = search_url(query, limit)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("deepseek_search_api_key_missing")
    if cancel.is_set() or time.monotonic() >= deadline:
        raise TimeoutError("search_request_stopped")
    client = AuthorityHTTP(authority)
    response = None
    try:
        response = client.post(
            SEARCH_ENDPOINT,
            json={"query": query.strip(), "count": limit, "need_content": False},
            headers={
                "Authorization": "Bearer " + api_key.strip(),
                "Accept-Encoding": "identity",
            },
            timeout=20,
            deadline=deadline,
            cancel=cancel,
            stream=True,
        )
        if response.status_code != 200:
            # Do not persist provider error text or authenticated request objects.
            raise ValueError(f"deepseek_search_http_{response.status_code}")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("search_encoded_body_not_supported")
        chunks, size = [], 0
        # A one-byte chunk yields even on a drip-fed response. Larger chunks
        # can keep resetting the read idle timeout before returning control.
        # This also supports production urllib3 1.26 without another transport.
        for chunk in response.iter_content(1):
            if cancel.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("search_request_stopped")
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise ValueError("search_response_size_limit")
            chunks.append(chunk)
        return b"".join(chunks), {
            "url": identity,
            "content_type": "application/json",
            "provider": "deepseek",
            "method": "POST",
            "endpoint": SEARCH_ENDPOINT,
        }
    except TimeoutError:
        raise
    except (OSError, HTTPError):
        raise TimeoutError("deepseek_search_transport_failed") from None
    finally:
        if response is not None:
            response.close()
        client.close()


def search_results(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("deepseek_search_invalid_json") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("deepseek_search_invalid_results")  # noqa: TRY004 - invalid provider response
    results = []
    for item in payload["results"]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("deepseek_search_invalid_result")  # noqa: TRY004 - invalid provider response
        url = item["url"]
        target = urlsplit(url)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or len(url) > 4096
            or any(ord(c) < 32 for c in url)
        ):
            continue
        if any(row["url"] == url for row in results):
            continue
        results.append(
            {
                "title": str(item.get("title") or "")[:300],
                "url": url,
                "snippet": str(item.get("snippet") or "")[:1000],
                "summary": str(item.get("summary") or "")[:8000],
                "status": "discovery_only_not_primary_evidence",
            }
        )
        if len(results) == 8:
            break
    return results
