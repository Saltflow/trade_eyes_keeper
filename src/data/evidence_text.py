"""Shared offline HTML projection and bounded existing material parse reads."""

import json
import re
from io import StringIO
from pathlib import Path

from bs4 import BeautifulSoup, Comment


def physical_text_lines(text):
    """Match file iteration: CR/LF/CRLF delimit rows; form feeds do not."""
    return [line.removesuffix("\n") for line in StringIO(text, newline=None)]


def html_rows(text):
    soup = BeautifulSoup(text, "html.parser")
    for item in soup.find_all(string=lambda value: isinstance(value, Comment)):
        item.extract()
    for item in soup(["script", "style", "head", "img", "iframe", "form"]):
        item.decompose()
    # Keep report links in a text-only projection; never fetch external assets.
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.startswith(("https://", "http://")) and not re.search(
            r"(token|secret|password|api_key)=", href, re.IGNORECASE
        ):
            link.append(f" ({href})")
    for row in soup.find_all("tr"):
        if row.find("tr") is not None:
            continue
        cells = row.find_all(["td", "th"], recursive=False)
        if cells:
            row.replace_with(
                " | ".join(cell.get_text(" ", strip=True) for cell in cells)
            )
    return (
        {"text": line.strip()}
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    )


def cached_parse(hub, digest, *, max_bytes=8 * 1024 * 1024):
    """Use verified existing SDK parses without loading oversized image JSON."""
    caches = [hub.parsed]
    quality_cache = getattr(hub.parsed, "_quality_cache", None)
    if callable(quality_cache):
        caches.insert(0, quality_cache())
    for cache in caches:
        path = cache._path(digest)
        if not path.exists():
            reference = path.with_name("parsed.ref.json")
            if reference.exists():
                if reference.stat().st_size > max_bytes:
                    raise ValueError(
                        "material parse reference exceeds reader bound; not scanned"
                    )
                record = json.loads(reference.read_text(encoding="utf-8"))
                path = Path(record["path"])
            else:
                legacy = cache.root / "legacy_roots.json"
                if legacy.exists():
                    if legacy.stat().st_size > max_bytes:
                        raise ValueError(
                            "material legacy index exceeds reader bound; not scanned"
                        )
                    roots = json.loads(legacy.read_text(encoding="utf-8"))
                    path = next(
                        (
                            candidate
                            for root in roots
                            if (
                                candidate := Path(root) / digest / "parsed.json"
                            ).is_file()
                        ),
                        path,
                    )
        if not path.exists():
            continue
        if path.stat().st_size > max_bytes:
            raise ValueError(
                "existing material parse exceeds bounded reader size; not scanned"
            )
        return cache.get(digest)
    return None


def parsed_projection(parsed, *, max_chars=8 * 1024 * 1024):
    """One text coordinate system for reading and searching an existing parse."""
    from src.data_hub import DataHub

    view = DataHub.text_view(parsed, max_chars=max_chars)
    text, basis, gaps = view["text"], "existing_parsed_text", []
    if "html" in str(parsed.get("content_type", "")):
        text = "\n".join(
            item["text"]
            for part in parsed.get("content", [])
            if part.get("type") == "text"
            for item in html_rows(part.get("text", ""))
        )
        basis = "rendered_html_text"
    if len(text) > max_chars:
        text = text[:max_chars]
        gaps.append("existing_parse_incomplete")
    if not view["source_complete"] or view["truncated"]:
        gaps.append("existing_parse_incomplete")
    if not parsed.get("page_texts") and "pdf" in str(parsed.get("content_type", "")):
        gaps.append("page_mapping_unavailable")
    images = parsed.get("images") or []
    image_parts = any(
        part.get("type") == "image_url" for part in parsed.get("content", [])
    )
    if images or image_parts:
        pages = sorted(
            {
                image["page"]
                for image in images
                if isinstance(image, dict) and type(image.get("page")) is int
            }
        )
        gaps.append(
            "visual_text_unavailable: existing images were not OCR-read; pages="
            + (",".join(map(str, pages)) if pages else "unknown")
        )
    return text, basis, list(dict.fromkeys(gaps))


def numbered_lines(lines, *, line_basis="source_text"):
    """Yield stable one-based text lines, using only explicit page markers."""
    page = None
    for number, line in enumerate(lines, 1):
        marker = re.fullmatch(
            r"\s*(?:---\s*|#+\s*|\[|【)?(?:PAGE|页)\s*[:#：]?\s*(\d+)\s*(?:---|\]|】)?\s*",
            line,
            re.IGNORECASE,
        )
        if marker and line_basis != "rendered_html_text":
            page = int(marker.group(1))
        yield {"line": number, "page": page, "text": line}
