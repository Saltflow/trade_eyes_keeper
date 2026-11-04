"""Resumable, catalog-owned acquisition of public financial source documents.

Only registered catalog jobs may be downloaded. This worker never changes price
or financial-statement facts and never participates in the Baostock breaker.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 1000
_DEFAULT_SIZE = object()
PUBLIC_SOURCE_DOMAINS = (
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "szse.com.cn",
    "hkexnews.hk",
    "sec.gov",
    "vanguard.com",
    "sgx.com",
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; trade-eyes-keeper document archive)",
    "Accept": "application/pdf,text/html,text/plain;q=0.8",
    "Accept-Encoding": "identity",
}


@contextmanager
def document_worker_lock(path):
    """Standalone nonblocking OS lock, compatible with existing project locks."""
    handle = Path(path).open("a+b")  # noqa: SIM115 - lock context owns handle lifetime
    acquired = False
    try:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        if acquired:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} started_at={time.time()}\n".encode())
            handle.flush()
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class DocumentBlocked(ValueError):
    """A gap requiring source discovery or an operator, not blind retries."""


class DocumentFailure(RuntimeError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def safe_public_url(url: str) -> tuple[str, str, int, str]:
    """Validate every redirect and pin the subsequent connection to public DNS."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise DocumentBlocked("needs_source_lookup")
    parsed = urlsplit(url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise DocumentBlocked("invalid_source_url") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.username is not None
        or parsed.password is not None
        or port != (443 if parsed.scheme == "https" else 80)
        or any(ord(char) < 32 for char in url)
        or not any(
            host == domain or host.endswith("." + domain)
            for domain in PUBLIC_SOURCE_DOMAINS
        )
    ):
        raise DocumentBlocked("source_domain_not_approved")
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DocumentFailure("source_dns_unavailable") from exc
    ips = list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise DocumentBlocked("source_resolves_to_non_public_address")
    normalized = urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    return normalized, host, port, ips[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, address, timeout, deadline=None):
        super().__init__(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
        self.address = address
        self.deadline = deadline

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("connection deadline exceeded")
                raw.settimeout(min(self.timeout, remaining))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return min(86400, max(0, float(value)))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return min(86400, max(0, when.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return 0


def download_document(
    url,
    destination,
    deadline,
    stop,
    wait_request,
    *,
    max_bytes=_DEFAULT_SIZE,
    minimum_free_bytes=0,
):
    """Stream to disk; explicit None removes only the per-document size cap."""
    max_bytes = MAX_DOCUMENT_BYTES if max_bytes is _DEFAULT_SIZE else max_bytes
    current = url
    for redirect in range(4):
        if stop.is_set() or time.monotonic() >= deadline:
            raise DocumentFailure("download_interrupted_or_timed_out")
        current, host, port, address = safe_public_url(current)
        wait_request(deadline)
        parsed = urlsplit(current)
        timeout = max(0.1, min(30, deadline - time.monotonic()))
        connection = (
            _PinnedHTTPSConnection(host, port, address, timeout, deadline=deadline)
            if parsed.scheme == "https"
            else http.client.HTTPConnection(address, port, timeout=timeout)
        )
        try:
            request_path = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection.request("GET", request_path, headers={**HEADERS, "Host": host})
            if connection.sock is not None:
                connection.sock.settimeout(
                    max(0.1, min(30, deadline - time.monotonic()))
                )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise DocumentBlocked("source_redirect_limit")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                if response.status in {400, 401, 403, 404, 410}:
                    raise DocumentBlocked(f"source_http_{response.status}")
                raise DocumentFailure(
                    f"source_http_{response.status}",
                    _retry_after(response.getheader("Retry-After")),
                )
            encoding = (response.getheader("Content-Encoding") or "identity").lower()
            if encoding != "identity":
                raise DocumentBlocked("compressed_http_body_not_supported")
            length = response.getheader("Content-Length")
            if max_bytes is not None and length and int(length) > max_bytes:
                raise DocumentBlocked("document_exceeds_size_limit")
            size = 0
            with Path(destination).open("wb") as output:
                while True:
                    remaining = deadline - time.monotonic()
                    if stop.is_set() or remaining <= 0:
                        raise DocumentFailure("download_interrupted_or_timed_out")
                    if connection.sock is not None:
                        connection.sock.settimeout(min(30, max(0.1, remaining)))
                    block = response.read1(
                        65536 if max_bytes is None else min(65536, max_bytes + 1 - size)
                    )
                    if not block:
                        break
                    size += len(block)
                    if max_bytes is not None and size > max_bytes:
                        raise DocumentBlocked("document_exceeds_size_limit")
                    if minimum_free_bytes and shutil.disk_usage(
                        Path(destination).parent
                    ).free < minimum_free_bytes + len(block):
                        raise DocumentBlocked("disk_space_reserve_reached")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if length and size != int(length):
                raise DocumentFailure("incomplete_response_body")
            return {
                "url": current,
                "content_type": response.getheader("Content-Type") or "",
                "bytes": size,
            }
        except (OSError, http.client.HTTPException) as exc:
            raise DocumentFailure("source_network_error") from exc
        finally:
            connection.close()
    raise DocumentBlocked("source_redirect_limit")


def _legacy_extract_pdf_text(pdf_path, text_path, max_pages=MAX_PDF_PAGES):
    """Preserve missing-input/backend diagnostics; never parse a real original.

    Existing originals take the single data-hub backend below. This compatibility
    branch is useful to callers probing extractor availability before acquisition.
    """
    if any(importlib.util.find_spec(name) for name in ("fitz", "pypdf", "pdfplumber")):
        raise FileNotFoundError(pdf_path)
    binary = shutil.which("pdftotext")
    if not binary:
        return {"status": "needs_extractor", "text_available": False}
    completed = subprocess.run(
        [
            binary,
            "-enc",
            "UTF-8",
            "-f",
            "1",
            "-l",
            str(max_pages),
            str(pdf_path),
            str(text_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=45,
        check=False,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    if completed.returncode != 0:
        raise DocumentFailure("pdf_text_parser_failed")
    if not text_path.is_file() or text_path.stat().st_size > MAX_TEXT_BYTES:
        text_path.unlink(missing_ok=True)
        raise DocumentBlocked("extracted_text_exceeds_size_limit")
    pages = text_path.read_text(encoding="utf-8").split("\f")
    count = len(pages) - (1 if pages and not pages[-1].strip() else 0)
    text = "\n\n".join(
        f"--- PAGE {n} ---\n{page}"
        for n, page in enumerate(pages[:max_pages], 1)
        if page.strip()
    ).strip()
    if not text:
        text_path.unlink(missing_ok=True)
        return {
            "status": "needs_ocr",
            "parser": "pdftotext",
            "pages": count,
            "text_available": False,
        }
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise DocumentBlocked("extracted_text_exceeds_size_limit")
    text_path.write_bytes(encoded)
    return {
        "status": "text_truncated" if count >= MAX_PDF_PAGES else "text_extracted",
        "parser": "pdftotext",
        "pages": count,
        "text_available": True,
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def extract_pdf_text(pdf_path, text_path, max_pages=MAX_PDF_PAGES, *, authority=None):
    """Project text projection of the shared complete neutral material."""
    pdf_path, text_path = Path(pdf_path), Path(text_path)
    if not pdf_path.is_file():
        # Preserve the no-extractor/missing-input diagnostic contract. Existing
        # real originals always pass through the content-addressed authority.
        return _legacy_extract_pdf_text(pdf_path, text_path, max_pages)
    from src.data_hub import DataHub
    from src.platform_composition import authority_for

    hub = DataHub(authority or authority_for(Path(__file__).resolve().parents[2]))
    parsed = hub.ensure_parsed(pdf_path, "application/pdf")
    view = hub.text_view(parsed, max_pages=max_pages)
    if not view["text"].strip():
        text_path.unlink(missing_ok=True)
        return {
            "status": "needs_ocr",
            "parser": parsed["parser"],
            "pages": parsed["pages"],
            "text_available": False,
            "parse_ref": parsed["cache_path"],
        }
    encoded = view["text"].encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise DocumentBlocked("extracted_text_exceeds_size_limit")
    text_path.write_bytes(encoded)
    return {
        "status": "text_truncated" if view["truncated"] else "text_extracted",
        "parser": parsed["parser"],
        "pages": parsed["pages"],
        "text_available": True,
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
        "parse_ref": parsed["cache_path"],
        "neutral_source_sha256": parsed["sha256"],
    }


def render_pdf_page_images(
    pdf_path: Path,
    *,
    max_pages=300,
    dpi=120,
    start_page=1,
    page_count=12,
    authority=None,
) -> list[dict]:
    """Compatibility image view backed by the same neutral material authority."""
    if (
        type(start_page) is not int
        or type(page_count) is not int
        or start_page < 1
        or not 1 <= page_count <= 20
    ):
        raise ValueError("invalid page image range")
    from src.data_hub import DataHub, MaterialError
    from src.platform_composition import authority_for

    try:
        hub = DataHub(authority or authority_for(Path(__file__).resolve().parents[2]))
        images = hub.page_images(
            pdf_path,
            max_pages=max_pages,
            dpi=dpi,
            start_page=start_page,
            page_count=page_count,
        )
        if any(len(image["image"]) > 2 * 1024 * 1024 for image in images):
            raise DocumentBlocked("rendered_page_exceeds_size_limit")
        return images
    except MaterialError as error:
        raise DocumentBlocked(str(error)) from error


class DatasetDocumentWorker:
    def __init__(
        self,
        catalog,
        *,
        stop=None,
        min_interval=3.0,
        max_document_bytes=MAX_DOCUMENT_BYTES,
        max_pdf_pages=MAX_PDF_PAGES,
        minimum_free_bytes=0,
        document_timeout_seconds=120,
    ):
        if not 3 <= min_interval <= 3600:
            raise ValueError("min_interval must be between 3 and 3600 seconds")
        self.catalog = catalog
        self.root = Path(catalog.project_root).resolve()
        self.output = self.root / "data" / "dataset_documents"
        self.db_path = Path(catalog.db_path)
        if self.db_path != self.root / "data/dataset_catalog/catalog.sqlite3":
            raise ValueError("document worker requires the project's fixed catalog DB")
        self.stop = stop or threading.Event()
        self.min_interval = float(min_interval)
        if max_document_bytes is not None and (
            type(max_document_bytes) is not int or max_document_bytes < 1024
        ):
            raise ValueError("max_document_bytes must be None or an integer >= 1 KiB")
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes must be a nonnegative integer")
        if not 1 <= document_timeout_seconds <= 86400:
            raise ValueError("document_timeout_seconds must be 1..86400")
        if type(max_pdf_pages) is not int or not 1 <= max_pdf_pages <= MAX_PDF_PAGES:
            raise ValueError("max_pdf_pages is out of range")
        self.max_document_bytes = max_document_bytes
        self.max_pdf_pages = max_pdf_pages
        self.minimum_free_bytes = minimum_free_bytes
        self.document_timeout_seconds = document_timeout_seconds
        self._archive_locks = {}
        self._archive_locks_guard = threading.Lock()
        self.lock_path = self.output / ".worker.lock"
        from src.data_hub import DataHub
        from src.platform_composition import authority_for

        self.authority = getattr(catalog, "material_authority", None) or authority_for(
            self.root
        )
        self.hub = DataHub(self.authority)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        self._safe_path(self.db_path)
        self._safe_path(self.output)
        self.output.mkdir(parents=True, exist_ok=True)
        self._safe_path(self.lock_path)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS document_downloads (
                    job_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    updated REAL NOT NULL, error TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '{}',
                    input_fingerprint TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS document_download_rate (
                    id INTEGER PRIMARY KEY CHECK(id=1), next_request_at REAL NOT NULL
                );
            """)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(document_downloads)")
            }
            if "input_fingerprint" not in columns:
                db.execute(
                    "ALTER TABLE document_downloads ADD COLUMN input_fingerprint TEXT NOT NULL DEFAULT ''"
                )

    def _jobs(self, **filters):
        offset = 0
        while True:
            page = self.catalog.document_jobs(limit=100, offset=offset, **filters)
            items = page["items"]
            if not items:
                break
            yield from items
            offset += len(items)
            if offset >= page["total"]:
                break

    def _has_cached_pdf(self, job):
        for value in job.get("existing_paths", []):
            if not str(value).lower().endswith((".pdf", ".material-ref.json")):
                continue
            try:
                path = self._resolve_pdf(value)
                if path.is_file() and (
                    self.max_document_bytes is None
                    or path.stat().st_size <= self.max_document_bytes
                ):
                    with path.open("rb") as handle:
                        if handle.read(8).startswith(b"%PDF-"):
                            return True
            except (ValueError, OSError):
                continue
        return False

    def plan(self, max_documents=20):
        if type(max_documents) is not int or not 1 <= max_documents <= 10000:
            raise ValueError("max_documents must be 1..10000")
        total, counts, items, missing_source_urls = 0, {}, [], 0
        for job in self._jobs():
            total += 1
            cached_pdf = self._has_cached_pdf(job)
            missing_source_urls += not bool(job.get("source_url"))
            if cached_pdf and job.get("text_path"):
                state = "cached_document_and_text"
            elif cached_pdf:
                state = "eligible_for_text_extraction"
            elif not job.get("source_url"):
                state = "needs_source_lookup"
            else:
                state = "eligible_for_document_fetch"
            counts[state] = counts.get(state, 0) + 1
            if len(items) < max_documents:
                items.append(
                    {
                        "job_id": job["job_id"],
                        "code": job.get("code"),
                        "source_url": job.get("source_url"),
                        "source_url_missing": not bool(job.get("source_url")),
                        "state": state,
                        "existing_paths": job.get("existing_paths", []),
                    }
                )
        return {
            "mode": "plan",
            "total_sources": total,
            "counts": counts,
            "missing_source_urls": missing_source_urls,
            "items": items,
            "listed": len(items),
            "truncated": total > len(items),
            "network_requests": 0,
        }

    def _safe_path(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_relative_to(self.root) or path.resolve() != path:
            raise DocumentBlocked("document_path_outside_project_or_redirected")
        for part in (path, *path.parents):
            if part == self.root:
                break
            if part.is_symlink() or (
                hasattr(part, "is_junction") and part.is_junction()
            ):
                raise DocumentBlocked("document_path_redirected")
        if path.is_file() and path.stat().st_nlink != 1:
            raise DocumentBlocked("document_hardlink_not_allowed")
        return path

    def _wait_request(self, deadline, source="document-archive"):
        from src.task_runtime.request_policy import RequestPolicy

        policy = RequestPolicy(self.authority)
        # Read the old ledger as a compatibility floor; it is not erased.
        while True:
            with self._db() as db:
                row = db.execute(
                    "SELECT next_request_at FROM document_download_rate WHERE id=1"
                ).fetchone()
            delay = max(0, (row[0] if row else 0) - time.time())
            if not delay:
                break
            if delay >= deadline - time.monotonic() or self.stop.wait(delay):
                raise DocumentFailure("rate_wait_interrupted_or_timed_out")
        try:
            policy.wait(
                source,
                min_interval=self.min_interval,
                deadline=deadline,
                cancel=self.stop,
            )
        except (ValueError, TimeoutError) as error:
            raise DocumentFailure("rate_wait_interrupted_or_timed_out") from error
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO document_download_rate VALUES (1,?)",
                (time.time() + self.min_interval,),
            )

    def _set(self, job_id, status, *, error="", result=None, next_attempt_at=0):
        with self._db() as db:
            db.execute(
                "UPDATE document_downloads SET status=?,error=?,result=COALESCE(?,result),next_attempt_at=?,updated=? WHERE job_id=?",
                (
                    status,
                    error[:500],
                    json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else None,
                    next_attempt_at,
                    time.time(),
                    job_id,
                ),
            )

    def _result_intact(self, row):
        try:
            result = json.loads(row["result"])
            for prefix in ("pdf", "text"):
                path = (
                    self._resolve_pdf(result[f"{prefix}_path"])
                    if prefix == "pdf"
                    else self._safe_path(result[f"{prefix}_path"])
                )
                maximum = self.max_document_bytes if prefix == "pdf" else MAX_TEXT_BYTES
                if not path.is_file() or (
                    maximum is not None and path.stat().st_size > maximum
                ):
                    return False
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != result[f"{prefix}_sha256"]
                ):
                    return False
            return True
        except (ValueError, OSError, KeyError, TypeError):
            return False

    def _extract(self, pdf, text, deadline):
        remaining = min(60, deadline - time.monotonic())
        if remaining <= 0:
            raise DocumentFailure("extraction_time_budget_exhausted")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}
        }
        environment["PYTHONUTF8"] = "1"
        environment["DATA_HUB_AUTHORITY_SCOPE"] = self.authority.scope
        environment["DATA_HUB_AUTHORITY_ISOLATED"] = (
            "1" if self.authority.isolated else "0"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(pdf),
                str(text),
                str(self.max_pdf_pages),
                str(self.authority.root),
            ],
            cwd=self.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **(
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if os.name == "nt"
                else {"start_new_session": True}
            ),
        )
        expires = time.monotonic() + remaining
        try:
            while process.poll() is None:
                if self.stop.wait(0.1) or time.monotonic() >= expires:
                    raise DocumentFailure("extraction_interrupted_or_timed_out")
            output = process.stdout.read(8193)
            if process.returncode != 0 or len(output) > 8192:
                raise DocumentFailure("pdf_text_parser_failed")
            return json.loads(output)
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if process.poll() is None:
                        process.kill()
                else:
                    import signal

                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=3)
            process.stdout.close()

    def _resolve_pdf(self, value):
        if str(value).endswith(".material-ref.json") and hasattr(
            self.catalog, "resolve_artifact"
        ):
            self._safe_path(value)
            return self.catalog.resolve_artifact(value)
        return self._safe_path(value)

    def _archive(self, job, deadline):
        job_id = job["job_id"]
        source = urlsplit(job.get("source_url") or "")
        source_identity = (
            urlunsplit(
                (
                    source.scheme.lower(),
                    source.netloc.lower(),
                    source.path,
                    source.query,
                    "",
                )
            )
            or job_id
        )
        directory = self._safe_path(
            self.output / hashlib.sha256(source_identity.encode()).hexdigest()
        )
        directory.mkdir(exist_ok=True)
        legacy_pdf, text = directory / "original.pdf", directory / "document.txt"
        receipt = self._safe_path(directory / "extraction.json")
        original = None
        for value in job.get("existing_paths", []):
            if str(value).lower().endswith((".pdf", ".material-ref.json")):
                candidate = self._resolve_pdf(value)
                if candidate.is_file():
                    original = candidate
                    break
        if legacy_pdf.is_file():
            original = self._safe_path(legacy_pdf)
        if (
            original
            and self.max_document_bytes is not None
            and original.stat().st_size > self.max_document_bytes
        ):
            raise DocumentBlocked("cached_document_exceeds_size_limit")
        result = {
            "job_id": job_id,
            "source_url_missing": not bool(job.get("source_url")),
            "original_pdf_available": False,
            "text_available": False,
        }
        if not original and not job.get("source_url"):
            raise DocumentBlocked("needs_source_lookup")
        download_result = {}

        def download(target):
            kwargs = (
                {"max_bytes": self.max_document_bytes}
                if self.max_document_bytes != MAX_DOCUMENT_BYTES
                else {}
            )
            if self.minimum_free_bytes:
                kwargs["minimum_free_bytes"] = self.minimum_free_bytes
            result = download_document(
                job["source_url"],
                target,
                deadline,
                self.stop,
                lambda bound: self._wait_request(bound, job["source_url"]),
                **kwargs,
            )
            download_result.update(result or {})

        try:
            if job.get("source_url"):
                stored = self.hub.ensure_original(
                    job["source_url"],
                    download,
                    content_type="application/pdf",
                    local_path=original,
                )
            else:
                stored = {**self.hub.register_original(original), "cache_hit": True}
        except Exception as error:
            if "not_pdf" in str(error):
                from src.data_hub.materials import canonical_url
                from src.data_hub.storage import fingerprint

                capture = (
                    self.hub.root
                    / "attempts"
                    / (fingerprint([canonical_url(job["source_url"]), ""]) + ".json")
                )
                if capture.is_file():
                    evidence = json.loads(capture.read_text(encoding="utf-8"))
                    raw_path = Path(evidence.get("raw_path", ""))
                    if raw_path.is_file():
                        # A retained failure attachment, not an original-PDF
                        # cache or a successful source artifact.
                        shutil.copyfile(raw_path, directory / "source_response.html")
                self.catalog.record_document(
                    job_id, status="blocked", error="source_not_pdf_needs_source_lookup"
                )
                raise DocumentBlocked("source_not_pdf_needs_source_lookup") from error
            if "not_retried" in str(error):
                raise DocumentBlocked(str(error)) from error
            raise
        original = Path(stored["path"])
        with original.open("rb") as handle:
            if not handle.read(8).startswith(b"%PDF-"):
                raise DocumentBlocked("cached_document_is_not_pdf")
        if hasattr(self.catalog, "record_material_reference"):
            reference = directory / "original.material-ref.json"
            self.hub.write_reference(reference, stored)
            pdf_path = reference.relative_to(self.root).as_posix()
            self.catalog.record_material_reference(job_id, pdf_path)
        else:
            # Compatibility export for third-party catalogs without reference
            # capability. The production DatasetCatalog uses no PDF copy.
            if original != legacy_pdf:
                shutil.copyfile(original, legacy_pdf)
            pdf_path = legacy_pdf.relative_to(self.root).as_posix()
            self.catalog.record_document(job_id, pdf_path, status="available")
        result.update(
            original_pdf_available=True,
            pdf_path=pdf_path,
            pdf_sha256=stored["sha256"],
            material_authority=self.authority.to_dict(),
            download="reused_cached_pdf"
            if stored.get("cache_hit")
            else download_result,
        )
        saved = None
        if receipt.is_file() and receipt.stat().st_size < 16384:
            try:
                candidate = json.loads(receipt.read_text(encoding="utf-8"))
                if candidate.get("status") == "text_extracted" and self._result_intact(
                    {"result": json.dumps(candidate)}
                ):
                    saved = candidate
            except (OSError, ValueError):
                pass
        if saved:
            result.update(saved)
            result["job_id"] = job_id
            result["download"] = "reused_url_archive"
        else:
            result.update(self._extract(original, self._safe_path(text), deadline))
        if result.get("text_available"):
            result["text_path"] = text.relative_to(self.root).as_posix()
            self.catalog.record_document(
                job_id, result["text_path"], status="available"
            )
        receipt.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    def _claim_job(self, job):
        job_id = job["job_id"]
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO document_downloads(job_id,status,updated) VALUES (?,'pending',?)",
                (job_id, time.time()),
            )
            row = db.execute(
                "SELECT * FROM document_downloads WHERE job_id=?", (job_id,)
            ).fetchone()
            if row["status"] == "succeeded" and not self._result_intact(row):
                db.execute(
                    "UPDATE document_downloads SET status='pending',error='archived_document_missing_or_changed',next_attempt_at=0 WHERE job_id=?",
                    (job_id,),
                )
                row = db.execute(
                    "SELECT * FROM document_downloads WHERE job_id=?", (job_id,)
                ).fetchone()
            fingerprint = hashlib.sha256(
                json.dumps(
                    [job.get("source_url"), job.get("existing_paths", [])],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if row["status"] == "blocked" and (
                row["input_fingerprint"] != fingerprint
                or (
                    self.max_document_bytes is None
                    and row["error"]
                    in {
                        "document_exceeds_size_limit",
                        "cached_document_exceeds_size_limit",
                    }
                )
            ):
                db.execute(
                    "UPDATE document_downloads SET status='pending',error='',next_attempt_at=0 WHERE job_id=?",
                    (job_id,),
                )
                row = db.execute(
                    "SELECT * FROM document_downloads WHERE job_id=?", (job_id,)
                ).fetchone()
            if (
                row["status"] in {"succeeded", "blocked"}
                or row["next_attempt_at"] > time.time()
            ):
                return None
            db.execute(
                "UPDATE document_downloads SET status='running',attempts=attempts+1,updated=?,input_fingerprint=? WHERE job_id=?",
                (time.time(), fingerprint, job_id),
            )
        return dict(row)

    def _execute_job(self, job, row, deadline):
        # One archive per URL even when catalog aliases refer to the same PDF.
        with self._archive_locks_guard:
            archive_lock = self._archive_locks.setdefault(
                job.get("source_url") or job["job_id"], threading.Lock()
            )
        with archive_lock:
            return self._execute_archive(job, row, deadline)

    def _execute_archive(self, job, row, deadline):
        job_id = job["job_id"]
        try:
            result = self._archive(
                job, min(deadline, time.monotonic() + self.document_timeout_seconds)
            )
            status = (
                "succeeded" if result.get("status") == "text_extracted" else "blocked"
            )
            self._set(
                job_id,
                status,
                result=result,
                error=""
                if status == "succeeded"
                else result.get("status", "incomplete_document"),
            )
        except DocumentBlocked as exc:
            status = "blocked"
            self._set(job_id, status, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - persist failure of one source
            status = "failed"
            # Exceptions may contain URLs or provider response bodies.
            error = str(exc) if isinstance(exc, DocumentFailure) else type(exc).__name__
            retry = max(
                60 * 2 ** min(row["attempts"], 8),
                getattr(exc, "retry_after", 0),
            )
            self._set(job_id, status, error=error, next_attempt_at=time.time() + retry)
            if getattr(exc, "retry_after", 0):
                with self._db() as db:
                    db.execute(
                        "INSERT INTO document_download_rate VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_request_at=MAX(next_request_at,excluded.next_request_at)",
                        (time.time() + exc.retry_after,),
                    )
        return {"job_id": job_id, "status": status}

    def run(self, *, max_documents=20, max_seconds=300, job_ids=None, workers=1):
        if type(max_documents) is not int or not 1 <= max_documents <= 10000:
            raise ValueError("max_documents must be 1..10000")
        if type(max_seconds) not in {int, float} or not 1 <= max_seconds <= 86400:
            raise ValueError("max_seconds must be 1..86400")
        if job_ids is not None and (
            not isinstance(job_ids, (list, tuple, set))
            or not 1 <= len(job_ids) <= 100
            or any(not isinstance(value, str) for value in job_ids)
        ):
            raise ValueError("job_ids must contain 1..100 strings")
        if type(workers) is not int or workers not in {1, 2, 3}:
            raise ValueError("workers must be 1, 2 or 3")
        filters = {"job_ids": job_ids} if job_ids is not None else {}
        self._initialize()
        deadline = time.monotonic() + max_seconds
        processed, selected = [], []
        with document_worker_lock(self.lock_path) as acquired:
            if not acquired:
                raise RuntimeError("A document backfill worker already holds the lock")
            with self._db() as db:
                # A prior owner died. Preserve the failure and retry only in a
                # new explicitly requested execution, never mark partial success.
                db.execute(
                    "UPDATE document_downloads SET status='failed',error='interrupted_previous_worker',next_attempt_at=?,updated=? WHERE status='running'",
                    (time.time() + 60, time.time()),
                )
                # Index all gaps first. Missing URLs never consume the bounded
                # network-work budget or hide eligible jobs behind the first page.
                total_sources = 0
                indexing_interrupted = False
                for job in self._jobs(**filters):
                    if self.stop.is_set() or time.monotonic() >= deadline:
                        indexing_interrupted = True
                        break
                    total_sources += 1
                    db.execute(
                        "INSERT OR IGNORE INTO document_downloads(job_id,status,updated) VALUES (?,'pending',?)",
                        (job["job_id"], time.time()),
                    )
                    if not job.get("source_url") and not self._has_cached_pdf(job):
                        fingerprint = hashlib.sha256(
                            json.dumps(
                                [None, job.get("existing_paths", [])], sort_keys=True
                            ).encode()
                        ).hexdigest()
                        db.execute(
                            "UPDATE document_downloads SET status='blocked',error='needs_source_lookup',input_fingerprint=?,updated=? WHERE job_id=?",
                            (fingerprint, time.time(), job["job_id"]),
                        )
            for job in self._jobs(**filters):
                if (
                    self.stop.is_set()
                    or time.monotonic() >= deadline
                    or len(processed) + len(selected) >= max_documents
                ):
                    break
                if not job.get("source_url") and not self._has_cached_pdf(job):
                    continue
                row = self._claim_job(job)
                if row is None:
                    continue
                if workers == 1:
                    processed.append(self._execute_job(job, row, deadline))
                else:
                    selected.append((job, row))
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(self._execute_job, job, row, deadline)
                        for job, row in selected
                    ]
                    processed.extend(
                        future.result() for future in as_completed(futures)
                    )
        with self._db() as db:
            counts = {
                row["status"]: row["count"]
                for row in db.execute(
                    "SELECT status,COUNT(*) AS count FROM document_downloads GROUP BY status"
                )
            }
        return {
            "mode": "execute",
            "total_sources": total_sources,
            "source_sync_incomplete": indexing_interrupted,
            "processed": processed,
            "counts": counts,
            "remaining_download_jobs": counts.get("pending", 0)
            + counts.get("failed", 0),
            "blocked_jobs": counts.get("blocked", 0),
            "stopped": self.stop.is_set(),
            "time_budget_exhausted": time.monotonic() >= deadline,
        }


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if len(sys.argv) not in {3, 4, 5}:
        raise SystemExit("Internal PDF extractor requires input and output paths")
    if os.name != "nt":
        import resource

        # Parser children cannot consume the host's entire memory or produce an
        # unbounded decompressed text file. No model/user code is executed here.
        for resource_kind, limit in (
            (resource.RLIMIT_AS, 768 * 1024 * 1024),
            # The public text view remains capped at MAX_TEXT_BYTES. Complete
            # neutral sparse-page images may legitimately exceed that view cap.
            (resource.RLIMIT_FSIZE, 512 * 1024 * 1024),
            (resource.RLIMIT_CPU, 55),
        ):
            _soft, hard = resource.getrlimit(resource_kind)
            bound = limit if hard == resource.RLIM_INFINITY else min(hard, limit)
            resource.setrlimit(resource_kind, (bound, bound))
    print(
        json.dumps(
            extract_pdf_text(
                Path(sys.argv[1]),
                Path(sys.argv[2]),
                int(sys.argv[3]) if len(sys.argv) >= 4 else MAX_PDF_PAGES,
                authority=(
                    __import__(
                        "src.shared_contracts", fromlist=["AuthorityRef"]
                    ).AuthorityRef(
                        Path(sys.argv[4]),
                        scope=os.environ.get("DATA_HUB_AUTHORITY_SCOPE", "public"),
                        isolated=os.environ.get("DATA_HUB_AUTHORITY_ISOLATED") == "1",
                    )
                    if len(sys.argv) == 5
                    else None
                ),
            ),
            ensure_ascii=False,
        )
    )
