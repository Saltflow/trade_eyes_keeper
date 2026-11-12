"""DS search uses one authenticated API and keeps immutable evidence."""

import json
import threading
import time
from unittest.mock import Mock

import pytest

from src.data_hub import public_web
from src.data_hub.materials import DataHub
from src.interactive.assistant.data_tools import BusinessDataTools
from src.interactive.assistant.web_search import search_web
from src.shared_contracts import AuthorityRef


@pytest.fixture
def authority(tmp_path):
    return AuthorityRef(tmp_path / "authority", isolated=True)


@pytest.fixture
def transport(monkeypatch):
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [
        json.dumps(
            {
                "results": [
                    {
                        "title": "Original source",
                        "url": "https://example.org/report",
                        "snippet": "Evidence discovery",
                        "summary": "Unverified provider summary",
                    }
                ]
            }
        ).encode(),
        b"",
    ]
    client = Mock()
    client.post.return_value = response
    monkeypatch.setattr(public_web, "AuthorityHTTP", Mock(return_value=client))
    return client, response


def acquire(authority, **kwargs):
    return search_web(
        "IFRS 18", authority=authority, api_key="fixture-secret", **kwargs
    )


def test_api_auth_post_and_cross_session_archive(authority, transport):
    from src.data.dataset_catalog import DatasetCatalog

    catalog = DatasetCatalog(authority.root.parent, material_authority=authority)
    result = acquire(authority, register_material=catalog.register_material)
    client, response = transport
    assert result["provider"] == "deepseek"
    assert result["original"]["status"] == "saved"
    assert result["parse"]["status"] == "complete"
    assert result["catalog"]["status"] == "registered"
    args, options = client.post.call_args
    assert args == ("https://api.deepseek.com/v1/tools/search",)
    assert options["json"] == {"query": "IFRS 18", "count": 5, "need_content": False}
    assert options["headers"] == {
        "Authorization": "Bearer fixture-secret",
        "Accept-Encoding": "identity",
    }
    response.close.assert_called_once()
    client.close.assert_called_once()
    assert result["results"][0]["status"] == "discovery_only_not_primary_evidence"
    assert "fixture-secret" not in json.dumps(result)
    hub = DataHub(AuthorityRef(authority.root, isolated=True))
    raw = hub.resolve(result["sha256"])
    assert "fixture-secret" not in json.dumps(raw)
    tools = BusinessDataTools(authority.root.parent, material_authority=hub.authority)
    read = tools.read(result["material_id"])
    assert "Original source" in json.dumps(read)


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 504, 302])
def test_http_failure_never_retries_or_leaks_provider_body(
    authority, transport, status
):
    client, response = transport
    response.status_code = status
    response.text = "fixture-secret"
    result = acquire(authority)
    assert result["original"] == {
        "status": "failed",
        "error": f"deepseek_search_http_{status}",
    }
    assert result["discovery"]["status"] == "not_attempted"
    assert "fixture-secret" not in json.dumps(result)
    client.post.assert_called_once()
    response.iter_content.assert_not_called()


@pytest.mark.parametrize("raw", [b"<html>shell</html>", b"{}", b'{"results": null}'])
def test_malformed_success_retained_but_not_claimed_empty(authority, transport, raw):
    transport[1].iter_content.return_value = [raw, b""]
    result = acquire(authority)
    assert result["original"]["status"] == "saved"
    assert result["discovery"]["status"] == "failed"
    assert DataHub(authority).resolve(result["sha256"])


def test_valid_empty_response_is_explicit_gap(authority, transport):
    transport[1].iter_content.return_value = [b'{"results": []}', b""]
    result = acquire(authority)
    assert result["results"] == []
    assert result["discovery"] == {
        "status": "unavailable",
        "reason": "no_usable_search_results",
    }
    assert result["coverage_gaps"]


def test_cached_search_has_no_second_charge_and_refresh_retains_old(
    authority, transport
):
    first = acquire(authority, refresh=False)
    cached = acquire(authority, refresh=False)
    assert cached["cache_hit"] is True
    assert first["material_id"] == cached["material_id"]
    transport[0].post.assert_called_once()
    transport[1].iter_content.return_value = [b'{"results": []}', b""]
    refreshed = acquire(authority, refresh=True)
    assert refreshed["material_id"] != first["material_id"]
    assert DataHub(authority).resolve(first["sha256"])
    assert transport[0].post.call_count == 2


def test_missing_key_stopped_and_oversize_never_pretend_success(authority, transport):
    client, response = transport
    failed = search_web("query", authority=authority, api_key="")
    assert failed["original"]["error"] == "deepseek_search_api_key_missing"
    client.post.assert_not_called()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TimeoutError):
        public_web.search_public(
            "query",
            authority,
            api_key="fixture",
            deadline=time.monotonic() + 10,
            cancel=cancel,
        )
    client.post.assert_not_called()
    response.iter_content.return_value = [b"x" * (2 * 1024 * 1024 + 1)]
    result = acquire(authority)
    assert result["original"]["error"] == "search_response_size_limit"


@pytest.mark.parametrize(
    "query,limit", [("x" * 129, 5), ("", 5), ("ok", True), ("ok", 9)]
)
def test_contract_limits_reject_before_transport(authority, transport, query, limit):
    with pytest.raises(ValueError):
        search_web(query, limit=limit, authority=authority, api_key="fixture")
    transport[0].post.assert_not_called()


def test_json_parse_failure_is_retained_without_implicit_retry(tmp_path):
    from src.data_hub.parsing import ParsedCache, ParsedCacheError

    source = tmp_path / "invalid.json"
    source.write_bytes(b"not json")
    cache = ParsedCache(tmp_path / "parse")
    with pytest.raises(ValueError):
        cache.read(source, content_type="application/json")
    with pytest.raises(ParsedCacheError, match="parse_attempt_not_retried:failed"):
        cache.read(source, content_type="application/json")
    source.write_bytes(b'{"results": []}')
    parsed = cache.read(source, content_type="application/json")
    assert not parsed["cache_hit"]
    assert cache.read(source, content_type="application/json")["cache_hit"]


@pytest.mark.parametrize("error_kind", ["timeout", "protocol"])
def test_raw_transport_failure_is_sanitized(authority, transport, error_kind):
    from urllib3.exceptions import ProtocolError, ReadTimeoutError

    error = (
        ReadTimeoutError(None, None, "fixture-secret")
        if error_kind == "timeout"
        else ProtocolError("fixture-secret")
    )
    transport[1].iter_content.side_effect = error
    result = acquire(authority)
    assert result["original"]["error"] == "deepseek_search_transport_failed"
    assert "fixture-secret" not in json.dumps(result)
    transport[0].post.assert_called_once()
    transport[1].close.assert_called_once()


def test_encoded_response_is_rejected_before_read(authority, transport):
    transport[1].headers = {"Content-Encoding": "gzip"}
    result = acquire(authority)
    assert result["original"]["error"] == "search_encoded_body_not_supported"
    transport[1].iter_content.assert_not_called()


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_slow_stream_stops_between_available_bytes(
    authority, transport, monkeypatch, stop
):
    from io import BytesIO

    from requests import Response
    from urllib3.response import HTTPResponse

    moment = [0]
    cancel = threading.Event()

    class SlowStream(BytesIO):
        reads = 0

        def read(self, size=-1):
            self.reads += 1
            moment[0] += 1
            if stop == "cancel":
                cancel.set()
            return super().read(min(size, 1))

    stream = SlowStream(b'{"results": []}')
    transport[1].raw = HTTPResponse(body=stream, preload_content=False)
    actual = Response()
    actual.raw = transport[1].raw
    transport[1].iter_content.side_effect = actual.iter_content
    monkeypatch.setattr(public_web.time, "monotonic", lambda: moment[0])
    with pytest.raises(TimeoutError, match="search_request_stopped"):
        public_web.search_public(
            "query", authority, api_key="fixture", deadline=2, cancel=cancel
        )
    assert stream.reads == (2 if stop == "deadline" else 1)
    transport[1].close.assert_called_once()
    transport[0].close.assert_called_once()
