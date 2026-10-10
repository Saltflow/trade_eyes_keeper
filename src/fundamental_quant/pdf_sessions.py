"""Original PDF pages and scoped Files API references for persistent research.

No extraction, selection, model generation or implicit external call occurs here.
Files upload is explicit. Page numbers always refer to the original PDF (1 based).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

BATCH_PAGES = 50
MIB = 1024 * 1024
RENDER_VERSION = "pdf-original-pages/2"


class PDFSessionError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".page-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class PageDocument:
    document_id: str
    issuer_id: str
    path: str
    sha256: str
    period_end: str
    published_at: str
    kind: str
    title: str = ""
    url: str = ""

    @classmethod
    def from_source(cls, source: dict, project_root: Path) -> PageDocument:
        path = source.get("document_path") or source.get("pdf_path")
        digest = source.get("document_sha256")
        if not path or not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
            raise PDFSessionError("pdf_source_identity_missing")
        for field in ("issuer_id", "period_end", "published_at"):
            if not source.get(field):
                raise PDFSessionError("pdf_source_metadata_missing_" + field)
        original = Path(path)
        if not original.is_absolute():
            original = Path(project_root) / original
        return cls(
            document_id=str(source.get("document_id") or digest),
            issuer_id=str(source["issuer_id"]),
            path=str(original.resolve()),
            sha256=digest,
            period_end=str(source["period_end"]),
            published_at=str(source["published_at"]),
            kind=str(source.get("document_kind") or source.get("kind") or "unknown"),
            title=str(source.get("document_title") or ""),
            url=str(source.get("url") or ""),
        )


class PDFBatchReader:
    def __init__(self, store, root: Path, allowed_roots=None, *, project_root=None):
        self.store = store
        self.root = Path(root).resolve()
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.allowed_roots = tuple(
            Path(p).resolve() for p in (allowed_roots or [self.project_root])
        )
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pdf_session_pages (
                page_key TEXT PRIMARY KEY, document_sha256 TEXT NOT NULL,
                page INTEGER NOT NULL, payload TEXT NOT NULL)""")

    def discover(self, packet: dict) -> list[PageDocument]:
        """Discover original PDFs without depending on extracted text completeness."""
        sources = list(packet.get("sources") or [])
        sources += list(packet.get("documents") or [])
        for peer in packet.get("peers") or []:
            if isinstance(peer, dict):
                sources.extend(peer.get("sources") or [])
                sources.extend(peer.get("documents") or [])
        documents = {}
        for source in sources:
            if not isinstance(source, dict):
                continue
            if not (source.get("document_path") or source.get("pdf_path")):
                continue
            document = PageDocument.from_source(source, self.project_root)
            key = (document.issuer_id, document.sha256)
            previous = documents.get(key)
            if previous and (
                previous.period_end != document.period_end
                or previous.published_at != document.published_at
            ):
                raise PDFSessionError("pdf_source_metadata_conflict")
            documents[key] = document
        return sorted(
            documents.values(),
            key=lambda d: (d.period_end, d.kind == "annual"),
            reverse=True,
        )

    def discover_catalog(self, company: dict, as_of: date) -> list[PageDocument]:
        """Read archived originals directly, including pages the text parser missed.

        Periods are mapped only from explicit report titles, never publication year.
        Ambiguous/unidentified documents are left for metadata repair, not relabelled.
        """
        catalog = self.project_root / "data/dataset_catalog/catalog.sqlite3"
        if not catalog.exists():
            return []
        documents = {}
        with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT DISTINCT d.document_id,d.title,d.publication_date,d.source_url,"
                "f.relative_path,f.sha256 FROM documents d "
                "JOIN document_sources s ON s.document_id=d.document_id "
                "JOIN document_artifacts a ON a.document_id=d.document_id "
                "JOIN files f ON f.file_id=a.file_id "
                "WHERE s.code=? AND a.kind='pdf' AND f.present=1",
                (company["code"],),
            ).fetchall()
        for row in rows:
            title = str(row["title"] or "")
            year = re.search(r"(?<!\d)(20\d{2})\s*年", title)
            if not year or "摘要" in title:
                continue
            year = int(year[1])
            if not as_of.year - 7 <= year <= as_of.year:
                continue
            published = str(row["publication_date"] or "")[:10]
            try:
                if date.fromisoformat(published) > as_of:
                    continue
            except ValueError:
                raise PDFSessionError("catalog_publication_date_invalid") from None
            periods = (
                (r"(?:第三|三|第3|3)季度", "09-30", "quarterly"),
                (r"(?:第一|一|第1|1)季度", "03-31", "quarterly"),
                (r"半年度|中期|(?:第二|二|第2|2)季度", "06-30", "interim"),
                (r"年度报告|年报|(?:第四|四|第4|4)季度", "12-31", "annual"),
            )
            identity = next(
                (
                    (end, kind)
                    for pattern, end, kind in periods
                    if re.search(pattern, title)
                ),
                None,
            )
            if identity is None:
                continue
            end, kind = identity
            doc = PageDocument.from_source(
                {
                    "document_id": row["document_id"],
                    "document_path": row["relative_path"],
                    "document_sha256": row["sha256"],
                    "issuer_id": company["id"],
                    "period_end": f"{year}-{end}",
                    "published_at": published,
                    "document_kind": kind,
                    "document_title": title,
                    "url": row["source_url"],
                },
                self.project_root,
            )
            documents[(doc.issuer_id, doc.sha256)] = doc
        return sorted(documents.values(), key=lambda doc: doc.period_end, reverse=True)

    def _bytes(self, document: PageDocument) -> bytes:
        path = Path(document.path).resolve()
        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise PDFSessionError("pdf_outside_allowed_roots")
        try:
            if path.stat().st_size > 512 * MIB:
                raise PDFSessionError("pdf_size_limit")
            data = path.read_bytes()
        except OSError:
            raise PDFSessionError("pdf_unavailable", retryable=True) from None
        if _sha(data) != document.sha256:
            raise PDFSessionError("pdf_hash_mismatch")
        return data

    def page_count(self, document: PageDocument) -> int:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(self._bytes(document))) as pdf:
            return len(pdf.pages)

    def render_batch(self, document: PageDocument, start_page: int = 1) -> dict:
        import pdfplumber

        if type(start_page) is not int or start_page < 1:
            raise PDFSessionError("invalid_page_range")
        with pdfplumber.open(io.BytesIO(self._bytes(document))) as pdf:
            total = len(pdf.pages)
            if start_page > total:
                raise PDFSessionError("invalid_page_range")
            end = min(total, start_page + BATCH_PAGES - 1)
            pages = [
                self._render(document, pdf.pages[n - 1], n, total)
                for n in range(start_page, end + 1)
            ]
        validate_request([], pages)
        return {
            "document": asdict(document),
            "start_page": start_page,
            "end_page": end,
            "total_pages": total,
            "pages": pages,
            "next_page": end + 1 if end < total else None,
            "estimated_image_tokens": len(pages) * 1024,
        }

    def render_zoom(self, document: PageDocument, page: int, box: list) -> dict:
        """Explicit normalized full-page region [x0,y0,x1,y1]; never replace original."""
        import pdfplumber

        if (
            not isinstance(box, (list, tuple))
            or len(box) != 4
            or any(type(x) not in (float, int) or not math.isfinite(x) for x in box)
            or not 0 <= box[0] < box[2] <= 1
            or not 0 <= box[1] < box[3] <= 1
        ):
            raise PDFSessionError("invalid_zoom_box")
        with pdfplumber.open(io.BytesIO(self._bytes(document))) as pdf:
            if type(page) is not int or not 1 <= page <= len(pdf.pages):
                raise PDFSessionError("invalid_page_range")
            return self._render(
                document, pdf.pages[page - 1], page, len(pdf.pages), box
            )

    def _render(self, document, original, page, total, zoom=None):
        from PIL import Image

        key = _sha(
            json.dumps(
                [
                    document.sha256,
                    document.issuer_id,
                    document.period_end,
                    document.published_at,
                    page,
                    RENDER_VERSION,
                    zoom,
                ],
                sort_keys=True,
            ).encode()
        )
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT payload FROM pdf_session_pages WHERE page_key=?", (key,)
            ).fetchone()
        if row:
            cached = json.loads(row[0])
            path = Path(cached["image_path"]).resolve()
            if not path.is_relative_to(self.root):
                raise PDFSessionError("page_cache_outside_root")
            if path.exists() and _sha(path.read_bytes()) == cached["image_sha256"]:
                return cached
        box = original.mediabox
        width, height = abs(float(box[2] - box[0])), abs(float(box[3] - box[1]))
        if not min(width, height) > 0:
            raise PDFSessionError("invalid_pdf_geometry")
        # Render all mediabox content; reduce resolution, never crop original pages.
        resolution = min(144, 4000 * 72 / max(width, height))
        if zoom:
            resolution = min(288, 8000 * 72 / max(width, height))
        try:
            image = original.to_image(
                resolution=resolution, antialias=True, force_mediabox=True
            ).original.convert("RGB")
        except Exception:  # noqa: BLE001 -- isolate third-party renderer failures
            raise PDFSessionError("pdf_render_failed", retryable=True) from None
        if zoom:
            image = image.crop(
                tuple(
                    round(v * (image.width if i % 2 == 0 else image.height))
                    for i, v in enumerate(zoom)
                )
            )
            image.thumbnail((4000, 4000), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        rendered = buffer.getvalue()
        if len(rendered) > 64 * MIB:
            raise PDFSessionError("page_image_size_limit")
        image_hash = _sha(rendered)
        path = self.root / "pages" / (image_hash + ".png")
        _atomic(path, rendered)
        metadata = {
            "page_key": key,
            "source_ref": f"pdf:{document.sha256}:p{page}",
            "document_id": document.document_id,
            "document_sha256": document.sha256,
            "document_path": document.path,
            "url": document.url,
            "kind": document.kind,
            "issuer_id": document.issuer_id,
            "period_end": document.period_end,
            "published_at": document.published_at,
            "page": page,
            "total_pages": total,
            "image_path": str(path),
            "image_sha256": image_hash,
            "bytes": len(rendered),
            "width": image.width,
            "height": image.height,
            "renderer_version": RENDER_VERSION,
            "zoom_box": zoom,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO pdf_session_pages VALUES(?,?,?,?)",
                (key, document.sha256, page, json.dumps(metadata, ensure_ascii=False)),
            )
        return metadata


def validate_request(messages: list, pages: list[dict]) -> dict:
    """Validate all active page references, including repeats, before paid calls."""
    count = len(pages)
    if count > 600:
        raise PDFSessionError("vision_image_count_limit")
    total = 0
    for page in pages:
        size = page.get("bytes")
        if type(size) is not int or not 0 < size <= 64 * MIB:
            raise PDFSessionError("vision_image_size_limit")
        dimension = 4096 if count >= 15 else 8192
        if any(
            type(page.get(k)) is not int or not 0 < page[k] <= dimension
            for k in ("width", "height")
        ):
            raise PDFSessionError("vision_image_dimension_limit")
        total += size
    if total > 200 * MIB:
        raise PDFSessionError("vision_total_image_size_limit")
    body_bytes = len(json.dumps(messages, ensure_ascii=False, allow_nan=False).encode())
    if body_bytes > 48 * MIB:
        raise PDFSessionError("vision_request_body_limit")
    return {
        "images": count,
        "image_bytes": total,
        "body_bytes": body_bytes,
        "estimated_image_tokens": count * 1024,
    }


class DeepSeekFiles:
    """Single-attempt upload and scoped reusable references; no generation calls."""

    def __init__(self, store, config: dict, *, http=None, clock=time.time):
        llm = config.get("llm") or {}
        self.api_key = str(llm.get("api_key") or os.getenv("DEEPSEEK_API_KEY", ""))
        self.base_url = str(
            llm.get("base_url") or "https://api.deepseek.com/v1"
        ).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PDFSessionError("invalid_files_base_url")
        self.scope = _sha((self.base_url + "\0" + self.api_key).encode())
        self.store, self.http, self.clock = store, http or requests, clock
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pdf_session_files (
                scope TEXT NOT NULL, image_sha256 TEXT NOT NULL, file_id TEXT NOT NULL,
                expires_at REAL, checked_at REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(scope,image_sha256))""")

    def invalidate(self, file_id: str) -> None:
        with self.store.transaction() as db:
            db.execute(
                "DELETE FROM pdf_session_files WHERE scope=? AND file_id=?",
                (self.scope, file_id),
            )

    def _response(self, response):
        try:
            if response.status_code != 200:
                raise PDFSessionError(
                    "files_http_" + str(int(response.status_code)),
                    retryable=response.status_code == 429
                    or response.status_code >= 500,
                )
            body = response.json()
            if not isinstance(body, dict) or not re.fullmatch(
                r"file-api-[A-Za-z0-9_-]+", str(body.get("id", ""))
            ):
                raise PDFSessionError("files_invalid_response")
            expiry = body.get("expires_at")
            if expiry is not None and (
                type(expiry) not in (float, int) or not math.isfinite(expiry)
            ):
                raise PDFSessionError("files_invalid_expiry")
            return body
        except (ValueError, TypeError):
            raise PDFSessionError("files_invalid_response") from None
        finally:
            response.close()

    def ensure_uploaded(self, page: dict) -> dict:
        if not self.api_key.strip():
            raise PDFSessionError("files_credentials_missing")
        validate_request([], [page])
        digest = page["image_sha256"]
        now = self.clock()
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM pdf_session_files WHERE scope=? AND image_sha256=?",
                (self.scope, digest),
            ).fetchone()
        headers = {"Authorization": "Bearer " + self.api_key}
        body = None
        try:
            if row and (row["expires_at"] is None or row["expires_at"] > now + 300):
                if now - row["checked_at"] < 300:
                    return {"type": "file", "file_id": row["file_id"]}
                response = self.http.get(
                    self.base_url + "/files/" + quote(row["file_id"], safe=""),
                    headers=headers,
                    timeout=(5, 60),
                    allow_redirects=False,
                )
                if response.status_code in (404, 410):
                    response.close()
                    self.invalidate(row["file_id"])
                else:
                    body = self._response(response)
                    if body["id"] != row["file_id"]:
                        raise PDFSessionError("files_identity_mismatch")
                    if (
                        body.get("expires_at") is not None
                        and body["expires_at"] <= now + 300
                    ):
                        body = None
            if body is None:
                try:
                    image = Path(page["image_path"]).read_bytes()
                except OSError:
                    raise PDFSessionError("page_image_unavailable") from None
                if _sha(image) != digest or len(image) != page["bytes"]:
                    raise PDFSessionError("page_image_hash_mismatch")
                response = self.http.post(
                    self.base_url + "/files",
                    headers=headers,
                    data={
                        "purpose": "user_data",
                        "expires_after[anchor]": "created_at",
                        "expires_after[seconds]": "2592000",
                    },
                    files={"file": (digest + ".png", image, "image/png")},
                    timeout=(5, 120),
                    allow_redirects=False,
                )
                body = self._response(response)
            if body.get("bytes") != page["bytes"]:
                raise PDFSessionError("files_size_mismatch")
            if body.get("expires_at") is not None and body["expires_at"] <= now + 300:
                raise PDFSessionError("files_expired_response")
        except requests.RequestException:
            raise PDFSessionError("files_transport_failed", retryable=True) from None
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO pdf_session_files VALUES(?,?,?,?,?,?)",
                (
                    self.scope,
                    digest,
                    body["id"],
                    body.get("expires_at"),
                    now,
                    json.dumps(body, ensure_ascii=False),
                ),
            )
        return {"type": "file", "file_id": body["id"]}
