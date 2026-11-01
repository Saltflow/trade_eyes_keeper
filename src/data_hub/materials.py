"""One immutable original index per authority; business outputs are references."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..shared_contracts import AuthorityRef
from .parsing import ParsedCache, hash_lock
from .storage import atomic_write, file_sha, fingerprint, load


class MaterialError(ValueError):
    """A local material gap; callers may continue unrelated evidence tasks."""


def canonical_url(url: str) -> str:
    if not url:
        return ""
    value = urlsplit(url)
    if value.scheme not in {"https", "http"} or not value.hostname:
        raise MaterialError("material_source_url_invalid")
    if value.username or value.password:
        raise MaterialError("material_source_url_credentials_forbidden")
    return urlunsplit(
        (value.scheme.lower(), value.netloc.lower(), value.path or "/", value.query, "")
    )


class DataHub:
    """Trusted SDK for a deployment's explicit material authority.

    A callback is the only external I/O boundary. Same-URL/version consumers
    share its producer and failure record; unknown/new URLs cannot be deduped
    before their bytes are known. Private scopes have independent namespaces.
    """

    def __init__(self, authority: AuthorityRef, *, pdf_reader=None):
        if not isinstance(authority, AuthorityRef):
            raise TypeError("explicit_authority_required")
        from ..task_runtime.authority import bind_authority

        bind_authority(authority)
        self.authority = authority
        self.root = authority.materials_root
        self.parsed = ParsedCache(
            self.root / "parsed", pdf_reader=pdf_reader, authority=authority
        )

    def _alias(self, url: str, version: str = "") -> Path:
        return (
            self.root
            / "aliases"
            / (fingerprint([canonical_url(url), version]) + ".json")
        )

    def _verified(self, record: dict) -> dict:
        path = Path(record["path"])
        if (
            record.get("scope") != self.authority.scope
            or record.get("authority_id") != self.authority.authority_id
        ):
            raise MaterialError("material_scope_mismatch")
        if not path.is_file() or file_sha(path) != record["sha256"]:
            current = self.root / "objects" / record["sha256"] / "original.json"
            replacement = load(current) if current.exists() else {}
            candidate = Path(replacement.get("path", ""))
            if (
                replacement.get("scope") != self.authority.scope
                or candidate == path
                or not candidate.is_file()
                or file_sha(candidate) != record["sha256"]
            ):
                raise MaterialError("material_original_unavailable_or_corrupt")
            path = candidate
        return {**record, "path": str(path.resolve())}

    def lookup(self, url: str, *, version: str = "") -> dict | None:
        path = self._alias(url, version)
        if path.is_file():
            record = load(path)
            if record.get("url") != canonical_url(url) or (
                version and record.get("version") != version
            ):
                raise MaterialError("material_alias_identity_changed")
            return self._verified(record)
        return None

    def _register(self, path, *, url="", version="", expected_sha=""):
        path = Path(path).resolve()
        digest = file_sha(path)
        if expected_sha and digest != expected_sha:
            raise MaterialError("material_original_hash_mismatch")
        blob_index = self.root / "objects" / digest / "original.json"
        if blob_index.exists():
            previous = load(blob_index)
            try:
                record = self._verified(previous)
            except MaterialError:
                # Equivalent verified bytes supplied by a new location can
                # restore an unavailable pointer; old declaration stays audited.
                history = blob_index.with_name(f"recovery_{fingerprint(previous)}.json")
                if not history.exists():
                    atomic_write(history, previous, exclusive=True)
                record = {
                    **previous,
                    "path": str(path),
                    "recovered_at": time.time(),
                }
                atomic_write(blob_index, record)
        else:
            record = {
                "sha256": digest,
                "path": str(path),
                "bytes": path.stat().st_size,
                "scope": self.authority.scope,
                "authority_id": self.authority.authority_id,
                "at": time.time(),
            }
            atomic_write(blob_index, record, exclusive=True)
        result = {**record, "url": canonical_url(url), "version": str(version)}
        if url:
            exact = self._alias(url, str(version))
            if exact.exists() and load(exact)["sha256"] != digest:
                raise MaterialError("immutable_material_alias_changed")
            if not exact.exists():
                atomic_write(exact, result, exclusive=True)
            if version:
                # A discovery-less consumer reads the latest known official
                # version. Earlier version aliases and bytes remain immutable.
                atomic_write(self._alias(url), result)
        return result

    def register_original(self, path, *, url="", version="", expected_sha=""):
        key = fingerprint(canonical_url(url)) if url else file_sha(Path(path))
        with hash_lock(self.root / "locks" / (key + ".lock")):
            # Separate URLs may arrive with the same bytes concurrently.
            digest = file_sha(Path(path))
            with hash_lock(self.root / "blob_locks" / (digest + ".lock")):
                return self._register(
                    path, url=url, version=version, expected_sha=expected_sha
                )

    def ensure_original(
        self,
        url,
        downloader,
        *,
        version="",
        expected_sha="",
        content_type=None,
        local_path=None,
    ):
        """Download once under a durable URL/version attempt, or reuse a ref.

        The trusted adapter validates public URLs and performs shared request
        admission for each actual HTTP/redirect. Callback retries are not hidden
        here; an attempted failure is not automatically retried by another app.
        """
        url = canonical_url(url)
        key = fingerprint(url)
        with hash_lock(self.root / "locks" / (key + ".lock")):
            existing = self.lookup(url, version=str(version))
            if existing is None and version and expected_sha:
                known = self.lookup(url)
                if known and known["sha256"] == expected_sha:
                    existing = known
            if existing:
                if expected_sha and expected_sha != existing["sha256"]:
                    raise MaterialError("material_cached_version_mismatch")
                if version and not self._alias(url, str(version)).exists():
                    self._register(
                        existing["path"],
                        url=url,
                        version=version,
                        expected_sha=expected_sha,
                    )
                return {**existing, "cache_hit": True}
            if local_path and Path(local_path).is_file():
                digest = file_sha(Path(local_path))
                with hash_lock(self.root / "blob_locks" / (digest + ".lock")):
                    result = self._register(
                        local_path, url=url, version=version, expected_sha=expected_sha
                    )
                return {**result, "cache_hit": True}
            url_state = self.root / "source_states" / (key + ".json")
            if url_state.exists():
                previous = load(url_state)
                if previous.get("status") in {"sending", "failed", "unknown"} and (
                    not version or previous.get("version") == str(version)
                ):
                    raise MaterialError(
                        "material_attempt_not_retried:" + previous["status"]
                    )
            attempt_path = (
                self.root / "attempts" / (fingerprint([url, str(version)]) + ".json")
            )
            if attempt_path.exists():
                status = load(attempt_path).get("status", "unknown")
                raise MaterialError("material_attempt_not_retried:" + status)
            attempt = {
                "url": url,
                "version": str(version),
                "status": "sending",
                "pid": os.getpid(),
                "at": time.time(),
            }
            atomic_write(attempt_path, attempt, exclusive=True)
            atomic_write(url_state, attempt)
            target = attempt_path.with_suffix(".part")
            try:
                from .runtime import run_material_task

                returned = run_material_task(
                    self.authority, "material_download", lambda: downloader(target)
                )
                if isinstance(returned, bytes):
                    with target.open("xb") as stream:
                        stream.write(returned)
                if not target.is_file() or target.stat().st_size == 0:
                    raise MaterialError("material_download_empty")
                with target.open("rb") as stream:
                    magic = stream.read(16384).lstrip()
                from .http import access_verification

                if access_verification(magic):
                    raise MaterialError("source_http_403")
                if content_type == "application/pdf" and not magic.startswith(b"%PDF-"):
                    raise MaterialError("material_response_is_not_pdf")
                if (
                    content_type
                    and "html" in content_type
                    and not magic.startswith((b"<", b"\xef\xbb\xbf<"))
                ):
                    raise MaterialError("material_response_is_not_html")
                digest = file_sha(target)
                if expected_sha and expected_sha != digest:
                    raise MaterialError("material_download_hash_mismatch")
                with hash_lock(self.root / "blob_locks" / (digest + ".lock")):
                    record_path = self.root / "objects" / digest / "original.json"
                    if record_path.exists():
                        stored = self._verified(load(record_path))
                        target.unlink()
                        path = Path(stored["path"])
                    else:
                        path = record_path.with_name("original.blob")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        target.replace(path)
                    result = self._register(
                        path, url=url, version=version, expected_sha=expected_sha
                    )
                atomic_write(attempt_path, {**attempt, "status": "complete", **result})
                atomic_write(url_state, {**attempt, "status": "complete", **result})
                return {**result, "cache_hit": False}
            except Exception as error:
                from .runtime import MaterialTaskUnknown

                self._record_access_failure(url, error)

                status = (
                    "unknown" if isinstance(error, MaterialTaskUnknown) else "failed"
                )
                evidence = {}
                if target.exists():
                    evidence = {
                        "raw_path": str(target),
                        "raw_sha256": file_sha(target),
                        "raw_bytes": target.stat().st_size,
                    }
                atomic_write(
                    attempt_path,
                    {**attempt, "status": status, "error": str(error), **evidence},
                )
                atomic_write(
                    url_state,
                    {**attempt, "status": status, "error": str(error), **evidence},
                )
                raise

    def _record_access_failure(self, url, error):
        """A failed document cannot let the next URL bypass an access block."""
        import re

        from ..task_runtime.request_policy import RequestPolicy

        status = getattr(getattr(error, "response", None), "status_code", None)
        known = re.fullmatch(r"source_http_(401|403|429)", str(error))
        if known:
            status = int(known[1])
        if status in {401, 403, 429}:
            retry = max(60, float(getattr(error, "retry_after", 0)))
            RequestPolicy(self.authority).block(
                url,
                f"source_http_{status}",
                retry_after=retry if status == 429 else None,
            )

    def import_bytes(self, raw: bytes) -> dict:
        if not raw:
            raise MaterialError("material_original_empty")
        import hashlib

        digest = hashlib.sha256(raw).hexdigest()
        with hash_lock(self.root / "blob_locks" / (digest + ".lock")):
            record_path = self.root / "objects" / digest / "original.json"
            if record_path.exists():
                return self._verified(load(record_path))
            path = record_path.with_name("original.blob")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(raw)
            return self._register(path)

    def ensure_parsed(self, path, content_type=None):
        self.register_original(path)
        return self.parsed.read(Path(path), content_type)

    def parse_bytes(self, raw, content_type):
        reference = self.import_bytes(raw)
        return self.parsed.read(Path(reference["path"]), content_type)

    def resolve(self, digest: str) -> dict:
        import re

        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise MaterialError("invalid_material_sha256")
        path = self.root / "objects" / digest / "original.json"
        if not path.is_file():
            raise MaterialError("material_not_registered_in_scope")
        record = load(path)
        if record.get("sha256") != digest:
            raise MaterialError("material_object_identity_changed")
        return self._verified(record)

    def write_reference(self, path, record, *, kind="pdf") -> None:
        verified = self.resolve(record["sha256"])
        marker = {
            "material_reference": 1,
            "authority_id": self.authority.authority_id,
            "scope": self.authority.scope,
            "sha256": verified["sha256"],
            "bytes": verified["bytes"],
            "kind": kind,
        }
        path = Path(path)
        if path.exists():
            if load(path) != marker:
                raise MaterialError("immutable_material_reference_changed")
        else:
            atomic_write(path, marker, exclusive=True)

    def resolve_reference(self, path) -> dict:
        marker = load(Path(path))
        if (
            marker.get("material_reference") != 1
            or marker.get("authority_id") != self.authority.authority_id
            or marker.get("scope") != self.authority.scope
        ):
            raise MaterialError("material_reference_scope_or_authority_mismatch")
        return {**self.resolve(marker["sha256"]), "kind": marker["kind"]}

    def page_images(self, path, *, start_page=1, page_count=12, max_pages=300, dpi=120):
        """Read cached neutral images or create an explicit cached rendition."""
        import base64

        if (
            type(dpi) is not int
            or not 36 <= dpi <= 300
            or type(start_page) is not int
            or start_page < 1
            or type(page_count) is not int
            or not 1 <= page_count <= 20
        ):
            raise MaterialError("invalid_page_image_rendition")
        parsed = self.ensure_parsed(path, "application/pdf")
        if parsed["pages"] > max_pages:
            raise MaterialError("pdf_page_limit_exceeded")
        numbers = list(
            range(start_page, min(parsed["pages"] + 1, start_page + page_count))
        )
        known = {row["page"]: row for row in parsed.get("images", [])}
        missing = [number for number in numbers if number not in known]
        if missing:
            from .pdf import render_pages

            root = self.root / "renditions" / parsed["sha256"] / f"jpeg-{dpi}-1600-85"
            with hash_lock(root / "render.lock"):
                for number in list(missing):
                    record = root / f"{number}.json"
                    if record.is_file():
                        envelope = load(record)
                        if fingerprint(envelope["image"]) != envelope["payload_sha256"]:
                            raise MaterialError("page_image_cache_corrupt")
                        known[number] = envelope["image"]
                        missing.remove(number)
                for image in render_pages(path, missing, dpi=dpi) if missing else []:
                    known[image["page"]] = image
                    atomic_write(
                        root / f"{image['page']}.json",
                        {"image": image, "payload_sha256": fingerprint(image)},
                        exclusive=True,
                    )
        return [
            {"page": n, "image": base64.b64decode(known[n]["base64"], validate=True)}
            for n in numbers
        ]

    def _rendition_path(self, digest, profile):
        import re

        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise MaterialError("invalid_material_sha256")
        if not isinstance(profile, str) or not profile or len(profile) > 256:
            raise MaterialError("explicit_rendition_profile_required")
        return (
            self.root
            / "renditions"
            / digest
            / "contracts"
            / fingerprint(profile)
            / "result.json"
        )

    def get_rendition(self, digest, profile):
        path = self._rendition_path(digest, profile)
        if not path.is_file():
            return None
        value = load(path)
        if (
            value.get("source_sha256") != digest
            or value.get("profile") != profile
            or value.get("scope") != self.authority.scope
            or value.get("payload_sha256") != fingerprint(value.get("value"))
        ):
            raise MaterialError("material_rendition_corrupt")
        return {**value, "cache_hit": True, "cache_path": str(path)}

    def import_rendition(self, digest, profile, value, *, provenance=None):
        """Import a declared old projection, never invent the missing original."""
        path = self._rendition_path(digest, profile)
        with hash_lock(path.with_suffix(".lock")):
            previous = self.get_rendition(digest, profile)
            if previous:
                if fingerprint(previous["value"]) != fingerprint(value):
                    raise MaterialError("immutable_material_rendition_changed")
                return previous
            record = {
                "source_sha256": digest,
                "profile": profile,
                "scope": self.authority.scope,
                "value": value,
                "payload_sha256": fingerprint(value),
                "contract_limited_projection": True,
                "provenance": provenance or {},
            }
            atomic_write(path, record, exclusive=True)
            return {**record, "cache_hit": False, "cache_path": str(path)}

    def ensure_rendition(self, digest, profile, builder):
        """Explicit compatibility algorithms run once, separate from neutral parse."""
        from .runtime import MaterialTaskUnknown, run_material_task

        path = self._rendition_path(digest, profile)
        with hash_lock(path.with_suffix(".lock")):
            previous = self.get_rendition(digest, profile)
            if previous:
                return previous
            self.resolve(digest)
            attempt = path.with_name("attempt.json")
            if attempt.exists():
                raise MaterialError(
                    "rendition_attempt_not_retried:"
                    + load(attempt).get("status", "unknown")
                )
            atomic_write(
                attempt,
                {"source_sha256": digest, "profile": profile, "status": "sending"},
                exclusive=True,
            )
            try:
                value = run_material_task(self.authority, "material_parse", builder)
                record = {
                    "source_sha256": digest,
                    "profile": profile,
                    "scope": self.authority.scope,
                    "value": value,
                    "payload_sha256": fingerprint(value),
                    "contract_limited_projection": True,
                    "provenance": {"original_available": True},
                }
                atomic_write(path, record, exclusive=True)
                atomic_write(
                    attempt,
                    {"status": "complete", "source_sha256": digest, "profile": profile},
                )
                return {**record, "cache_hit": False, "cache_path": str(path)}
            except Exception as error:
                atomic_write(
                    attempt,
                    {
                        "status": "unknown"
                        if isinstance(error, MaterialTaskUnknown)
                        else "failed",
                        "error": str(error),
                    },
                )
                raise

    def alias_rendition(self, url, digest, profile, *, version=""):
        if self.get_rendition(digest, profile) is None:
            raise MaterialError("rendition_missing")
        path = (
            self.root
            / "rendition_aliases"
            / (fingerprint([canonical_url(url), profile, version]) + ".json")
        )
        marker = {
            "source_url": canonical_url(url),
            "source_sha256": digest,
            "profile": profile,
            "version": version,
        }
        with hash_lock(path.with_suffix(".lock")):
            if path.exists() and load(path) != marker:
                raise MaterialError("immutable_rendition_alias_changed")
            if not path.exists():
                atomic_write(path, marker, exclusive=True)

    def rendition_for_url(self, url, profile, *, version=""):
        original = self.lookup(url, version=version)
        if original:
            return self.get_rendition(original["sha256"], profile)
        path = (
            self.root
            / "rendition_aliases"
            / (fingerprint([canonical_url(url), profile, version]) + ".json")
        )
        if path.exists():
            marker = load(path)
            if (
                marker.get("source_url") != canonical_url(url)
                or marker.get("profile") != profile
                or marker.get("version") != version
            ):
                raise MaterialError("rendition_alias_identity_changed")
            return self.get_rendition(marker["source_sha256"], profile)
        return None

    @staticmethod
    def text_view(parsed, *, max_chars=None, max_pages=None):
        if parsed.get("page_texts"):
            pages = list(parsed["page_texts"].items())
            selected = pages if max_pages is None else pages[:max_pages]
            text = "\n\n".join(f"--- PAGE {n} ---\n{v}" for n, v in selected)
            truncated = len(selected) < len(pages)
        else:
            text = "\n\n".join(
                part.get("text", "")
                for part in parsed["content"]
                if part.get("type") == "text"
            )
            if "html" in parsed["content_type"]:
                from bs4 import BeautifulSoup

                text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
            truncated = False
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        return {
            "text": text,
            "truncated": truncated,
            "source_sha256": parsed["sha256"],
            "parse_ref": parsed.get("cache_path"),
            "source_complete": bool(parsed.get("complete")),
        }
