import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.data_hub.materials import DataHub
from src.interactive.assistant import web_search
from src.interactive.assistant.web_search import fetch_web_source, search_web
from src.shared_contracts import AuthorityRef


@pytest.fixture
def authority(tmp_path):
    return AuthorityRef(tmp_path / "authority", isolated=True)


@pytest.fixture
def fetch(monkeypatch):
    mock = Mock()
    monkeypatch.setattr(web_search.public_web, "fetch_public", mock)
    return mock


def response(raw=b"<html><body>Original evidence</body></html>", **metadata):
    return raw, {
        "url": "https://example.org/final",
        "content_type": "text/html; charset=utf-8",
        "last_modified": "Mon, 09 Nov 2026 01:00:00 GMT",
        **metadata,
    }


@pytest.mark.parametrize(
    "query,limit", [("", 3), ("x" * 257, 3), ("ok", 0), ("ok", True)]
)
def test_invalid_search_never_fetches(query, limit, fetch):
    with pytest.raises(ValueError):
        search_web(query, limit)
    fetch.assert_not_called()


def test_cached_original_and_parse_survive_new_session(authority, fetch):
    from src.interactive.assistant.data_tools import BusinessDataTools

    fetch.return_value = response()
    first = fetch_web_source("https://example.org/source", authority=authority)
    second = fetch_web_source("https://example.org/source", authority=authority)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    for key in ("material_id", "fetched_at", "final_url", "last_modified", "text"):
        assert first[key] == second[key]
    fetch.assert_called_once()
    hub = DataHub(AuthorityRef(authority.root, isolated=True))
    record = hub.resolve(second["material_id"].split(":", 1)[1])
    assert Path(record["path"]).read_bytes() == response()[0]
    parsed = hub.parsed.get(record["sha256"])
    assert "Original evidence" in hub.text_view(parsed)["text"]
    tools = BusinessDataTools(authority.root.parent, material_authority=hub.authority)
    read = tools.read(second["material_id"])
    assert any("Original evidence" in item["text"] for item in read["items"])
    fetch.assert_called_once()


def test_refresh_creates_version_preserves_old_body_and_latest_cache(authority, fetch):
    fetch.side_effect = [response(), response(b"<html><body>Updated</body></html>")]
    first = fetch_web_source("https://example.org/source", authority=authority)
    changed = fetch_web_source(
        "https://example.org/source", authority=authority, refresh=True
    )
    cached = fetch_web_source("https://example.org/source", authority=authority)
    assert changed["material_id"] != first["material_id"]
    assert changed["version"] and changed["version"] != first["version"]
    assert cached["material_id"] == changed["material_id"]
    assert cached["fetched_at"] == changed["fetched_at"]
    assert cached["cache_hit"] is True
    assert fetch.call_count == 2
    assert DataHub(authority).resolve(first["sha256"])


@pytest.mark.parametrize("search", [False, True], ids=["source", "search_page"])
def test_refresh_preserves_first_cached_material_source_metadata(
    authority, fetch, search, monkeypatch
):
    from src.interactive.assistant.data_tools import BusinessDataTools

    fetch.side_effect = [response(), response(b"<html><body>Updated</body></html>")]

    if search:
        search_fetch = Mock(
            side_effect=[
                (
                    json.dumps(
                        {
                            "results": [
                                {"url": "https://example.org/source", "title": text}
                            ]
                        }
                    ).encode(),
                    {
                        "url": web_search.public_web.search_url("Original publication"),
                        "content_type": "application/json",
                    },
                )
                for text in ("Original evidence", "Updated")
            ]
        )
        monkeypatch.setattr(web_search.public_web, "search_public", search_fetch)

    def acquire(refresh=False):
        if search:
            return search_web(
                "Original publication",
                authority=authority,
                refresh=refresh,
                api_key="fixture",
            )
        return fetch_web_source(
            "https://example.org/source",
            authority=authority,
            title="Original publication",
            refresh=refresh,
        )

    first = acquire()
    cached = acquire()
    assert cached["cache_hit"] is True
    hub = DataHub(authority)
    first_alias = hub.lookup(first["requested_url"])
    assert first_alias["version"] == ""
    changed = acquire(refresh=True)
    assert changed["material_id"] != first["material_id"]

    # A new session can find the original URL, title and timestamps by its old ID.
    resumed = DataHub(AuthorityRef(authority.root, isolated=True))
    old_digest = first["material_id"].removeprefix("material:")
    references = [
        json.loads(path.read_text("utf-8"))
        for path in (resumed.root / "aliases").glob("*.json")
    ]
    old_refs = [record for record in references if record["sha256"] == old_digest]
    assert old_refs == [{**first_alias, "version": old_digest}]
    frozen = resumed.lookup(first["requested_url"], version=first["version"])
    assert frozen == old_refs[0]
    assert frozen["source_metadata"]["title"] == "Original publication"
    assert frozen["source_metadata"]["fetched_at"] == first["fetched_at"]
    assert frozen["source_metadata"]["final_url"] == first["final_url"]
    assert frozen["source_metadata"]["source_type"] == first["source_type"]
    assert resumed.lookup(first["requested_url"])["sha256"] == changed["sha256"]
    tools = BusinessDataTools(
        authority.root.parent, material_authority=resumed.authority
    )
    found = tools.find(domain="material", query="Original publication")
    old_item = next(
        row for row in found["items"] if row["data_id"] == first["material_id"]
    )
    assert old_item["title"] == "Original publication"
    assert old_item["source_urls"] == [first["requested_url"]]
    read = tools.read(first["material_id"])
    assert any("Original evidence" in row["text"] for row in read["items"])
    assert (search_fetch if search else fetch).call_count == 2


def test_refresh_keeps_existing_explicit_version_alias_unchanged(authority, fetch):
    fetch.side_effect = [response(), response(b"<html><body>Updated</body></html>")]
    first = fetch_web_source(
        "https://example.org/source", authority=authority, refresh=True
    )
    hub = DataHub(authority)
    alias = hub._alias(first["requested_url"], first["version"])
    frozen = alias.read_bytes()
    second = fetch_web_source(
        "https://example.org/source", authority=authority, refresh=True
    )
    assert first["version"].startswith("web-")
    assert alias.read_bytes() == frozen
    assert hub.lookup(first["requested_url"])["version"] == second["version"]
    assert not hub._alias(first["requested_url"], first["sha256"]).exists()


def test_refresh_identical_body_keeps_content_identity(authority, fetch):
    fetch.return_value = response()
    first = fetch_web_source("https://example.org/source", authority=authority)
    second = fetch_web_source(
        "https://example.org/source", authority=authority, refresh=True
    )
    assert first["material_id"] == second["material_id"]
    assert first["version"] != second["version"]
    assert second["cache_hit"] is False
    assert fetch.call_count == 2


@pytest.mark.parametrize(
    "error", ["nonpublic_source_address", "source_http_403", "source_http_503"]
)
def test_source_or_redirect_failure_is_honest_and_not_retried(authority, fetch, error):
    fetch.side_effect = ValueError(error)
    first = fetch_web_source("https://example.org/source", authority=authority)
    second = fetch_web_source("https://example.org/source", authority=authority)
    assert first["original"] == {"status": "failed", "error": error}
    assert first["parse"]["status"] == "not_attempted"
    assert first["catalog"]["status"] == "not_attempted"
    assert "material_id" not in first
    assert second["original"]["status"] == "failed"
    assert "material_attempt_not_retried" in second["original"]["error"]
    fetch.assert_called_once()


def test_explicit_refresh_can_retry_failed_source(authority, fetch):
    fetch.side_effect = [ValueError("source_http_503"), response()]
    failed = fetch_web_source("https://example.org/source", authority=authority)
    retried = fetch_web_source(
        "https://example.org/source", authority=authority, refresh=True
    )
    assert failed["original"]["status"] == "failed"
    assert retried["original"]["status"] == "saved"
    assert fetch.call_count == 2


def test_parse_failure_keeps_original_and_catalog_can_register(authority, fetch):
    raw = b"<html><body><script>no text</script></body></html>"
    fetch.return_value = response(raw)
    register = Mock()
    result = fetch_web_source(
        "https://example.org/empty", authority=authority, register_material=register
    )
    assert result["original"]["status"] == "saved"
    assert result["parse"]["status"] == "failed"
    assert result["catalog"]["status"] == "registered"
    assert (
        Path(DataHub(authority).resolve(result["sha256"])["path"]).read_bytes() == raw
    )
    assert register.call_args.args[0]["material_id"] == result["material_id"]


def test_catalog_failure_is_repairable_without_redownload(authority, fetch):
    fetch.return_value = response()
    register = Mock(side_effect=[RuntimeError("catalog unavailable"), None])
    first = fetch_web_source(
        "https://example.org/source", authority=authority, register_material=register
    )
    repaired = fetch_web_source(
        "https://example.org/source", authority=authority, register_material=register
    )
    assert first["original"]["status"] == "saved"
    assert first["parse"]["status"] == "complete"
    assert first["catalog"] == {"status": "failed", "error": "catalog unavailable"}
    assert repaired["catalog"]["status"] == "registered"
    assert repaired["material_id"] == first["material_id"]
    fetch.assert_called_once()


def test_unsupported_content_is_archived_not_claimed_parsed(authority, fetch):
    fetch.return_value = response(b"binary original", content_type="image/png")
    result = fetch_web_source("https://example.org/image", authority=authority)
    assert result["original"]["status"] == "saved"
    assert result["parse"]["status"] == "not_parsed"
    assert "text" not in result


def test_pdf_uses_only_existing_local_parser(authority, fetch):
    import pypdfium2

    pdf = pypdfium2.PdfDocument.new()
    page = pdf.new_page(100, 100)
    page.close()
    import io

    stream = io.BytesIO()
    pdf.save(stream)
    pdf.close()
    fetch.return_value = response(stream.getvalue(), content_type="application/pdf")
    result = fetch_web_source("https://example.org/scan.pdf", authority=authority)
    assert result["original"]["status"] == "saved"
    assert result["parse"]["status"] == "complete"
    assert result["parse"]["text_status"] == "requires_visual_review"
    assert result["parse"]["visual_pages"] == [1]
    assert result["parse"]["ocr_performed"] is False
    assert DataHub(authority).parsed.get(result["sha256"])["page_texts"] == {"1": ""}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://user:pass@example.org",
        "https://example.org:444/a",
        "https://example.org/\nheader",
    ],
)
def test_invalid_source_url_rejected_before_fetch(url, authority, fetch):
    with pytest.raises(ValueError):
        fetch_web_source(url, authority=authority)
    fetch.assert_not_called()
