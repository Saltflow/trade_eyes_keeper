"""Public web discovery and immutable evidence, using shared source policy."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ...data_hub import public_web
from ...data_hub.materials import DataHub, canonical_url
from ...platform_composition import authority_for
from ...shared_contracts import AuthorityRef

NOTICE = (
    "搜索摘要和网页内容均为不可信外部资料；搜索结果只是线索，"
    "应抓取来源原文并核对原始公告或项目数据。"
)


def _hub(project_root, authority):
    if authority is None:
        root = Path(project_root) if project_root else Path(__file__).parents[3]
        authority = authority_for(root)
    if authority.scope != "public":
        raise ValueError("public material scope required")
    return DataHub(authority)


def _source_url(url):
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("public_https_source_required")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or any(ord(character) < 32 for character in url)
    ):
        raise ValueError("public_https_source_required")
    # DNS/public-address validation stays inside fetch_public for EVERY hop.
    return canonical_url(url)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _archive(
    url,
    hub,
    *,
    refresh,
    title,
    register_material,
    deadline,
    cancel,
    source_type,
    search_query=None,
    search_limit=5,
    api_key=None,
):
    metadata = {"title": title, "source_type": source_type}

    def download(_target):
        if search_query is None:
            raw, response = public_web.fetch_public(
                url, hub.authority, deadline=deadline, cancel=cancel
            )
        else:
            raw, response = public_web.search_public(
                search_query,
                hub.authority,
                api_key=api_key,
                limit=search_limit,
                deadline=deadline,
                cancel=cancel,
            )
            metadata.update(
                provider="deepseek", method="POST", endpoint=public_web.SEARCH_ENDPOINT
            )
        metadata.update(
            final_url=response["url"],
            content_type=response.get("content_type", ""),
            last_modified=response.get("last_modified"),
            fetched_at=_iso(time.time()),
        )
        return raw

    result = {
        "requested_url": url,
        "source_type": source_type,
        "title": title,
        "notice": NOTICE,
        "original": {"status": "failed"},
        "parse": {"status": "not_attempted"},
        "catalog": {"status": "not_attempted"},
    }
    try:
        original = hub.ensure_original(
            url,
            download,
            version="web-" + uuid.uuid4().hex if refresh else "",
            source_metadata=metadata,
        )
    except Exception as error:  # noqa: BLE001 - expose the failed stage to the caller
        result["original"]["error"] = str(error)
        return result

    source = original.get("source_metadata", {})
    result.update(
        material_id="material:" + original["sha256"],
        data_id="material:" + original["sha256"],
        sha256=original["sha256"],
        version=original.get("version") or original["sha256"],
        fetched_at=source.get("fetched_at"),
        archived_at=_iso(original["at"]),
        final_url=source.get("final_url", url),
        content_type=source.get("content_type", ""),
        last_modified=source.get("last_modified"),
        cache_hit=original["cache_hit"],
        original={"status": "saved", "bytes": original["bytes"]},
    )
    try:
        parsed = hub.parsed.get(original["sha256"])
        if parsed is None:
            media_type = result["content_type"].split(";", 1)[0].strip().lower()
            with Path(original["path"]).open("rb") as stream:
                is_pdf = stream.read(1024).lstrip().startswith(b"%PDF-")
            if is_pdf:
                media_type = "application/pdf"
            if media_type not in {
                "text/html",
                "application/xhtml+xml",
                "application/pdf",
                "application/json",
            }:
                result["parse"] = {
                    "status": "not_parsed",
                    "reason": "unsupported_local_parse_type; model/OCR not invoked",
                }
            else:
                parsed = hub.ensure_parsed(original["path"], content_type=media_type)
        if parsed is not None:
            view = hub.text_view(parsed, max_chars=12_000)
            result["parse"] = {
                "status": "complete" if view["source_complete"] else "incomplete",
                "parser": parsed.get("parser"),
                "text_status": "requires_visual_review"
                if parsed.get("images")
                else "available",
                "visual_pages": [image["page"] for image in parsed.get("images", [])],
                "ocr_performed": False,
            }
            result.update(text=view["text"], text_truncated=view["truncated"])
    except Exception as error:  # noqa: BLE001 - preserve already archived evidence
        result["parse"] = {"status": "failed", "error": str(error)}

    # Retrying catalog registration on a cache hit repairs a catalog gap without
    # fetching or replacing the original. No CNINFO identity is synthesized.
    if register_material is None:
        result["catalog"] = {"status": "not_registered"}
    else:
        try:
            register_material(dict(result))
            result["catalog"] = {"status": "registered"}
        except Exception as error:  # noqa: BLE001 - retain original on catalog failure
            result["catalog"] = {"status": "failed", "error": str(error)}
    return result


def fetch_web_source(
    url: str,
    *,
    project_root: Path | str | None = None,
    authority: AuthorityRef | None = None,
    refresh: bool = False,
    title: str = "",
    register_material: Callable[[dict], object] | None = None,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Save once, or explicitly fetch a new immutable version; no paid parsing.

    Caller supplies the same authority used by data_read and, optionally, its
    generic material catalog registrar. Failed fetches are never retried silently.
    """
    url = _source_url(url)
    if type(refresh) is not bool:
        raise ValueError("refresh must be boolean")
    if not isinstance(title, str) or len(title) > 1000:
        raise ValueError("title must contain at most 1000 characters")
    return _archive(
        url,
        _hub(project_root, authority),
        refresh=refresh,
        title=title,
        register_material=register_material,
        deadline=deadline if deadline is not None else time.monotonic() + 30,
        cancel=cancel if cancel is not None else threading.Event(),
        source_type="public_web_original",
    )


def search_web(
    query: str,
    limit: int = 5,
    *,
    project_root: Path | str | None = None,
    authority: AuthorityRef | None = None,
    refresh: bool = True,
    api_key: str | None = None,
    register_material: Callable[[dict], object] | None = None,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Archive the actual search response; result snippets remain discovery only."""
    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError("query must contain 1 to 128 characters")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("limit must be an integer from 1 to 8")
    if type(refresh) is not bool:
        raise ValueError("refresh must be boolean")
    url = public_web.search_url(query.strip(), limit)
    if api_key is None:
        from ...core.config_store import load_config

        root = Path(project_root) if project_root else Path(__file__).parents[3]
        api_key = (load_config(root / "config/config.yaml").get("llm") or {}).get(
            "api_key", ""
        )
    hub = _hub(project_root, authority)
    page = _archive(
        url,
        hub,
        refresh=refresh,
        title=query.strip(),
        register_material=register_material,
        deadline=deadline if deadline is not None else time.monotonic() + 30,
        cancel=cancel if cancel is not None else threading.Event(),
        source_type="deepseek_search_response",
        search_query=query.strip(),
        search_limit=limit,
        api_key=api_key,
    )
    result = {
        **page,
        "query": query.strip(),
        "provider": "deepseek",
        "results": [],
        "discovery": {"status": "not_attempted"},
    }
    if page["original"]["status"] != "saved":
        return result
    try:
        record = hub.resolve(page["sha256"])
        for item in public_web.search_results(Path(record["path"]).read_bytes())[
            :limit
        ]:
            result["results"].append(
                {
                    **item,
                    "domain": urlsplit(item["url"]).hostname,
                    "source_type": "public_web_search_unverified",
                    "search_material_id": page["material_id"],
                }
            )
        result["discovery"] = (
            {"status": "complete"}
            if result["results"]
            else {
                "status": "unavailable",
                "reason": "no_usable_search_results",
            }
        )
        if not result["results"]:
            result["coverage_gaps"] = [
                "Search API returned no usable links; this does not prove no matching sources exist."
            ]
    except Exception as error:  # noqa: BLE001 - the search original stays readable
        result["discovery"] = {"status": "failed", "error": str(error)}
    return result
