"""One neutral, content-addressed document parse shared by all research stages."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .storage import atomic_write, file_sha, fingerprint, load


class ParsedCacheError(ValueError):
    """An existing immutable parse could not be trusted."""


_GUARDS: dict[str, threading.Lock] = {}
_GUARDS_LOCK = threading.Lock()
_PDF_READ_LOCK = threading.Lock()


@contextlib.contextmanager
def hash_lock(path: Path, timeout: float = 120):
    """OS-owned lock: process death releases it; lock inodes are never unlinked."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _GUARDS_LOCK:
        guard = _GUARDS.setdefault(str(path.resolve()), threading.Lock())
    if not guard.acquire(timeout=timeout):
        raise TimeoutError("document_cache_busy")
    stream = None
    locked = False
    try:
        stream = path.open("a+b")
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while not locked:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (BlockingIOError, PermissionError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("document_cache_busy") from None
                time.sleep(0.05)
        yield
    finally:
        if stream is not None:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        guard.release()


def neutral_content(content: list) -> list:
    """Strip known V2 task envelopes, never alter financial text or images."""
    result = list(content)
    if not all(isinstance(part, dict) for part in result):
        raise ParsedCacheError("unsupported_legacy_parse_layout")
    had_header = False
    if result and result[0].get("type") == "text":
        try:
            header = json.loads(result[0]["text"])
        except (ValueError, TypeError, KeyError):
            header = None
        if isinstance(header, dict) and {
            "frozen_job_ownership",
            "expected_company",
            "expected_year",
        }.issubset(header):
            result.pop(0)
            had_header = True
    if (
        had_header
        and result
        and result[-1].get("type") == "text"
        and result[-1].get("text", "").startswith("[block:")
    ):
        result.pop()
    if not result or not all(
        isinstance(x, dict) and x.get("type") in {"text", "image_url"} for x in result
    ):
        raise ParsedCacheError("unsupported_legacy_parse_layout")
    return result


class ParsedCache:
    def __init__(
        self, root: Path, *, pdf_reader: Callable | None = None, authority=None
    ):
        self.root = Path(root).resolve()
        redirect = self.root / "shared_parse_ref.json"
        if redirect.is_file():
            reference = load(redirect)
            if fingerprint(reference["root"]) != reference["root_fingerprint"]:
                raise ParsedCacheError("shared_parse_reference_corrupt")
            self.root = Path(reference["root"]).resolve()
        self.pdf_reader = pdf_reader
        self.authority = authority

    @classmethod
    def bind_root(cls, previous: Path, central: Path) -> Path:
        """Point old host cache roots at one authority, without copying bodies."""
        previous, central = Path(previous).resolve(), Path(central).resolve()
        if previous == central:
            return central
        with hash_lock(central / "locks/legacy_roots.lock"):
            path = central / "legacy_roots.json"
            roots = load(path) if path.is_file() else []
            if str(previous) not in roots:
                atomic_write(path, [*roots, str(previous)])
            marker = previous / "shared_parse_ref.json"
            reference = {
                "root": str(central),
                "root_fingerprint": fingerprint(str(central)),
            }
            if marker.is_file() and load(marker) != reference:
                raise ParsedCacheError("shared_parse_authority_changed")
            if not marker.exists():
                atomic_write(marker, reference, exclusive=True)
        return central

    def _path(self, digest: str) -> Path:
        import re

        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid_document_sha256")
        return self.root / digest / "parsed.json"

    def get(self, digest: str) -> dict | None:
        """Read a verified parse without requiring the original blob to exist."""
        with hash_lock(self.root / "locks" / f"{digest}.lock"):
            return self._existing(digest)

    def _existing(self, digest: str) -> dict | None:
        path = self._path(digest)
        if not path.exists():
            reference = path.with_name("parsed.ref.json")
            if reference.exists():
                data = load(reference)
                path = Path(data["path"])
                if not path.is_file() or file_sha(path) != data["file_sha256"]:
                    raise ParsedCacheError("legacy_parse_reference_corrupt")
            else:
                roots = self.root / "legacy_roots.json"
                for previous in load(roots) if roots.exists() else []:
                    candidate = Path(previous) / digest / "parsed.json"
                    if candidate.is_file():
                        path = candidate
                        break
                else:
                    return None
        try:
            envelope = load(path)
            value = envelope["parsed"]
            if envelope["payload_sha256"] != fingerprint(value):
                raise ParsedCacheError("parsed_cache_payload_changed")
            if value["sha256"] != digest or not value["complete"]:
                raise ParsedCacheError("parsed_cache_identity_or_completeness_changed")
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ParsedCacheError(f"corrupt_parsed_cache:{path}") from error
        # First use and replay must emit byte-identical content envelopes,
        # including caches written by an earlier canonical-key serializer.
        value = {
            **value,
            "content": [
                {"type": part["type"], **{k: v for k, v in part.items() if k != "type"}}
                for part in value["content"]
            ],
        }
        if value.get("page_texts"):
            value["page_texts"] = dict(
                sorted(value["page_texts"].items(), key=lambda pair: int(pair[0]))
            )
        return {**value, "cache_hit": True, "cache_path": str(path)}

    def _save(self, value: dict) -> dict:
        path = self._path(value["sha256"])
        atomic_write(
            path,
            {"payload_sha256": fingerprint(value), "parsed": value},
            exclusive=True,
        )
        return {**value, "cache_hit": False, "cache_path": str(path)}

    def _produce(self, digest, callback):
        attempt = self.root / digest / "parse_attempt.json"
        if attempt.exists():
            previous = load(attempt)
            raise ParsedCacheError(
                "parse_attempt_not_retried:" + previous.get("status", "unknown")
            )
        atomic_write(
            attempt,
            {"sha256": digest, "status": "parsing", "pid": os.getpid()},
            exclusive=True,
        )
        try:
            if self.authority is not None:
                from .runtime import run_material_task

                result = run_material_task(self.authority, "material_parse", callback)
            else:
                result = callback()
            atomic_write(attempt, {"sha256": digest, "status": "complete"})
            return result
        except BaseException as error:
            from .runtime import MaterialTaskUnknown

            status = (
                "unknown"
                if isinstance(
                    error, (MaterialTaskUnknown, KeyboardInterrupt, SystemExit)
                )
                else "failed"
            )
            atomic_write(
                attempt, {"sha256": digest, "status": status, "error": str(error)}
            )
            raise

    def read(self, path: Path, content_type: str | None = None) -> dict:
        path = Path(path)
        digest = file_sha(path)
        with hash_lock(self.root / "locks" / f"{digest}.lock"):
            existing = self._existing(digest)
            if existing is not None:
                return existing
            with path.open("rb") as stream:
                magic = stream.read(1024).lstrip()
            if (
                magic.startswith(b"%PDF-")
                or (content_type or "").split(";", 1)[0] == "application/pdf"
            ):
                reader = self.pdf_reader
                if reader is None:
                    from .pdf import read_pdf_material

                    reader = read_pdf_material

                # The model pool can be concurrent without concurrently entering
                # the native PDF renderer in this process.
                def produce():
                    with _PDF_READ_LOCK:
                        return reader(path)

                material = self._produce(digest, produce)
                if isinstance(material, dict):
                    content, pages = material["content"], material["pages"]
                    extra = {
                        key: material[key]
                        for key in ("page_texts", "images")
                        if key in material
                    }
                else:
                    content, pages = material
                    extra = {
                        key: material.material[key]
                        for key in ("page_texts", "images")
                        if hasattr(material, "material") and key in material.material
                    }
                value = {
                    "sha256": digest,
                    "content_type": "application/pdf",
                    "content": content,
                    "pages": pages,
                    "complete": True,
                    "anchors": [f"page:{p}" for p in range(1, pages + 1)],
                    "parser": "pypdfium2-v2-neutral/1",
                    **extra,
                }
            elif (content_type or "").split(";", 1)[0] in {
                "text/html",
                "application/xhtml+xml",
            } or path.suffix.lower() in {".html", ".htm", ".xhtml"}:
                value = self._produce(digest, lambda: self._html(path, digest))
            elif (content_type or "").split(";", 1)[0] == "application/json":

                def parse_json():
                    text = json.dumps(
                        json.loads(path.read_bytes()), ensure_ascii=False, indent=2
                    )
                    return {
                        "sha256": digest,
                        "content_type": "application/json",
                        "content": [{"type": "text", "text": text}],
                        "pages": 0,
                        "complete": True,
                        "anchors": [],
                        "parser": "json-neutral/1",
                    }

                value = self._produce(digest, parse_json)
            else:
                raise ParsedCacheError("document_is_not_pdf_or_declared_html")
            # Reject a source concurrently replaced while parsing it.
            if file_sha(path) != digest:
                raise ParsedCacheError("document_changed_during_parse")
            return self._save(value)

    def _html(self, path: Path, digest: str) -> dict:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for element in soup.find_all(["script", "style"]):
            element.decompose()
        body = soup.body or soup
        if not body.get_text(" ", strip=True):
            raise ParsedCacheError("empty_html_document")
        # Retain original table structure, inline XBRL and hidden numeric evidence.
        # This is HTML, not synthetic PDF pagination or an inferred financial table.
        anchors = [
            {"tag": element.name, "id": element.get("id") or element.get("name")}
            for element in body.find_all(True)
            if element.get("id") or (element.name == "a" and element.get("name"))
        ]
        return {
            "sha256": digest,
            "content_type": "text/html",
            "content": [{"type": "text", "text": str(body)}],
            "pages": 0,
            "complete": True,
            "anchors": anchors,
            "parser": "beautifulsoup-neutral-html/1",
        }

    def import_prepared(
        self, pdf_sha: str, content: list, pages: int, source: str = ""
    ) -> dict:
        if type(pages) is not int or pages <= 0:
            raise ParsedCacheError("legacy_pdf_page_count_required")
        value = {
            "sha256": pdf_sha,
            "content_type": "application/pdf",
            "content": neutral_content(content),
            "pages": pages,
            "complete": True,
            "anchors": [f"page:{p}" for p in range(1, pages + 1)],
            "parser": "imported-v2-frozen/1",
            "legacy_source": source,
        }
        with hash_lock(self.root / "locks" / f"{pdf_sha}.lock"):
            existing = self._existing(pdf_sha)
            return existing if existing is not None else self._save(value)
