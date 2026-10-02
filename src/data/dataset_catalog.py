"""Complete, offline inventory of business datasets and original documents.

The catalog describes evidence and gaps. Downloaded or indexed never means that
the project backtest/data-readiness contracts have passed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import stat
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

MAX_JSON_BYTES = 32 * 1024 * 1024
PAGE_LIMIT = 100
DOCUMENT_COUNTS_NOTICE = (
    "统计范围为所选dataset_id或全库，不受code/status/分页筛选影响。"
    "available仅表示有已登记内容，可能只有文字或JSON，绝不代表PDF与文字齐备。"
    "documents_with_pdf/text/pdf_and_text分别按文档去重统计实际存在的PDF、文字、二者配对；"
    "同一文档的多个文件或来源不重复计数。缺口维度可重叠，不可相加。"
    "缺PDF不自动等于不能回测；回测就绪须由实际策略数据合同验证。"
)
DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".meta",
    ".parquet",
    ".feather",
    ".arrow",
    ".pdf",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".xlsx",
    ".xls",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".svg",
}
TEXT_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".meta",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".svg",
}
SECRET_ASSIGNMENT = re.compile(
    rb"[\"']?(?:api[_-]?key|app[_-]?secret|access[_-]?token|refresh[_-]?token|password|passwd|bot[_-]?token|secret[_-]?key)[\"']?\s*[:=]\s*[\"']?([^\"'\s,;}]{4,})",
    re.IGNORECASE,
)
EXCLUDED_PARTS = {
    ".git",
    "__pycache__",
    "node_modules",
    "dataset_catalog",
    "provider_state",
    "runtime",
    "deployments",
    "deployment",
    "logs",
    "log",
    "sessions",
    "private",
    "private_account",
    "private_accounts",
    "accounts",
    "account",
    "holdings",
    "positions",
    "credentials",
    "feishu_research",
    "email_archive",
    "chat",
    "chats",
    "config",
}
EXCLUDED_NAMES = {
    "server_status.json",
    "transfer_status.json",
    "progress.json",
    "supervisor_progress.json",
    "resume_receipt.json",
    "startup_check.json",
}
CODE_KEYS = {"code", "stock_code", "symbol", "ticker", "instrument", "instrument_id"}
DATE_KEYS = {
    "date",
    "trade_date",
    "period_end",
    "published_at",
    "publication_date",
    "announcement_date",
    "as_of",
    "start",
    "end",
}
URL_KEYS = ("source_url", "url", "document_url", "pdf_url", "announcement_url")
DOCUMENT_PATH_KEYS = (
    "pdf_file_path",
    "pdf_path",
    "text_path",
    "document_path",
    "original_path",
)
READINESS_NOTICE = (
    "目录完整性与数据就绪分别记录；已下载、已索引或证券覆盖数量不代表"
    "行情/公司行动/时点财报/基准合同已经通过验证。"
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _identifier(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", "."} for part in value.split("/")):
        raise ValueError("invalid relative path")
    if ":" in value:
        raise ValueError("invalid relative path")
    return path.as_posix()


def _excluded(relative: str) -> str:
    parts = PurePosixPath(relative).parts
    for part in parts:
        lower = part.lower()
        if lower in EXCLUDED_PARTS or lower.startswith("."):
            return "private_or_operational_path"
        if any(word in lower for word in ("credential", "password", "private_key")):
            return "credential_name"
        if re.search(r"(?:^|[_-])(?:accounts?|holdings|positions)(?:[_.-]|$)", lower):
            return "private_account_path"
    name = parts[-1].lower()
    if name.startswith(".") or name in EXCLUDED_NAMES:
        return "operational_metadata"
    if (
        PurePosixPath(name).suffix
        and re.search(
            r"(?:^|[_-])(?:config|configuration|account|accounts|holdings|positions|credentials|secrets|tokens)(?:[_.-]|$)",
            name,
        )
    ) or name.startswith("ref_portfolio"):
        return "private_or_configuration_file"
    if name.endswith((".pid", ".lock", ".log", ".sqlite", ".sqlite3", ".db")):
        return "operational_file"
    return ""


def _business_file_reason(path: Path) -> str:
    if path.suffix.lower() not in DATA_EXTENSIONS:
        return "not_a_business_data_extension"
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return ""
    try:
        with path.open("rb") as handle:
            tail = b""
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                content = (tail + block).replace(b'\\"', b'"')
                if (
                    b"-----BEGIN PRIVATE KEY-----" in content
                    or b"-----BEGIN RSA PRIVATE KEY-----" in content
                ):
                    return "suspected_sensitive_content"
                for match in SECRET_ASSIGNMENT.finditer(content):
                    value = match.group(1).lower()
                    if value not in {
                        b"null",
                        b"none",
                        b"false",
                        b"true",
                        b"redacted",
                        b"<redacted>",
                    } and not value.startswith((b"***", b"your_", b"${")):
                        return "suspected_sensitive_content"
                tail = content[-1024:]
    except OSError as exc:
        return f"content_check_error:{type(exc).__name__}"
    return ""


def eligibility_reason(
    relative_path: str, logical_path: str | None = None, *, is_directory: bool = False
) -> str:
    """Public path-only policy for transfer manifests; empty means permitted.

    Packaging still uses iter_dataset_files for link and content checks.
    """
    try:
        relative = _safe_relative(relative_path)
        logical = (
            _safe_relative(logical_path)
            if logical_path is not None
            else _logical_path(relative)
        )
    except ValueError:
        return "invalid_relative_path"
    if logical != _logical_path(relative):
        return "logical_path_mismatch"
    logical_allowed = _business_root(logical) or (
        is_directory and logical in {"data", "cache"}
    )
    if not _business_root(relative) or not logical_allowed:
        return "outside_business_data_roots"
    reason = _excluded(relative) or _excluded(logical)
    if reason:
        return reason
    if (
        not is_directory
        and PurePosixPath(logical).suffix.lower() not in DATA_EXTENSIONS
    ):
        return "not_a_business_data_extension"
    return ""


def _business_root(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if len(parts) < 2:
        return False
    if parts[0] == "cache":
        return parts[1] in {
            "data",
            "announcement_content",
            "announcement_extraction",
            "pdf_files",
            "historical",
            "analysis_financial",
            "analysis",
        }
    if parts[0] != "data":
        return False
    if len(parts) == 2 and parts[1].endswith("_history.csv"):
        return True
    if parts[1].startswith("point_in_time") or parts[1] in {
        "reference_universe",
        "analysis",
        "dataset_documents",
        "optimizer_recovery",
        "instrument_audit",
        "optimizer",
    }:
        return True
    if parts[1] in {"dataset_imports", "server_imports"}:
        if len(parts) <= 3:
            return True
        nested = parts[3:]
        if nested[0] == "dataset":
            nested = nested[1:]
        if not nested:
            return True
        if len(nested) == 1 and nested[0] in {"data", "cache"}:
            return True
        if nested[0] in {"data", "cache"}:
            return _business_root(PurePosixPath(*nested).as_posix())
        return len(nested) == 1 and nested[0] in {
            "manifest.json",
            "import_manifest.json",
            "source_manifest.json",
        }
    return False


def _logical_path(relative: str) -> str:
    """Restore logical names in nested, materialized transfer snapshots."""
    parts = list(PurePosixPath(relative).parts)
    while len(parts) > 3 and parts[:2] in (
        ["data", "dataset_imports"],
        ["data", "server_imports"],
    ):
        start = 3
        if parts[start : start + 1] == ["dataset"]:
            start += 1
        if parts[start : start + 1] not in (["data"], ["cache"]):
            break
        parts = parts[start:]
    return PurePosixPath(*parts).as_posix()


def _scan_roots(root: Path) -> list[Path]:
    # Inspect top-level names so future roots cannot disappear from the audit.
    # The shared policy below prunes unknown/private directories before traversal.
    paths = []
    for name in ("data", "cache"):
        base = root / name
        try:
            info = base.lstat()
        except FileNotFoundError:
            continue
        if base.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            paths.append(base)
        elif stat.S_ISDIR(info.st_mode):
            paths.extend(sorted(base.iterdir(), key=lambda value: value.name))
        else:
            paths.append(base)
    return paths


def iter_catalog_entries(project_root: str | Path) -> Iterator[dict]:
    """Enumerate every eligible file and every excluded/error entry, without caps.

    A skipped directory is represented once and is never traversed. Hard links
    fail closed because the standard library cannot prove all link locations.
    The same function is used by transfer packaging and catalog construction.
    """
    root = Path(project_root).resolve()
    pending = list(reversed(_scan_roots(root)))
    while pending:
        path = pending.pop()
        relative = path.relative_to(root).as_posix()
        item = {
            "path": path,
            "relative_path": relative,
            "logical_path": _logical_path(relative),
            "eligible": False,
            "reason": "",
        }
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            yield {**item, "reason": f"stat_error:{type(exc).__name__}"}
            continue
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            yield {**item, "reason": "symlink_or_reparse_point"}
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            yield {**item, "reason": "outside_project"}
            continue
        reason = eligibility_reason(
            relative, item["logical_path"], is_directory=stat.S_ISDIR(info.st_mode)
        )
        if reason:
            yield {**item, "reason": reason}
        elif stat.S_ISDIR(info.st_mode):
            try:
                pending.extend(
                    sorted(path.iterdir(), key=lambda value: value.name, reverse=True)
                )
            except OSError as exc:
                yield {**item, "reason": f"directory_error:{type(exc).__name__}"}
        elif not stat.S_ISREG(info.st_mode):
            yield {**item, "reason": "not_regular_file"}
        elif info.st_nlink != 1:
            yield {**item, "reason": "hardlink_not_proven_local"}
        else:
            reason = _business_file_reason(path)
            if reason:
                yield {**item, "reason": reason}
                continue
            yield {
                **item,
                "eligible": True,
                "bytes": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }


def iter_dataset_files(project_root: str | Path) -> Iterator[dict]:
    """Transfer-compatible iterator; preserves every physical source path."""
    for item in iter_catalog_entries(project_root):
        if item["eligible"]:
            yield item


def _checked_file(root: Path, relative: str) -> Path:
    relative = _safe_relative(relative)
    if eligibility_reason(relative):
        raise ValueError("file is outside the business-data policy")
    path = root / relative
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor /= part
        info = cursor.lstat()
        if cursor.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked paths are not allowed")
    path.resolve().relative_to(root)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("only unlinked regular files are allowed")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _code(value) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    value = str(value).strip().upper()
    value = re.sub(r"^(SH|SZ|BJ)[.:]", "", value)
    value = re.sub(r"^(\d{6})\.(?:SH|SZ|BJ)$", r"\1", value)
    if re.fullmatch(r"\d{1,5}\.HK", value):
        return value[:-3].zfill(5)
    if re.fullmatch(r"\d{5,6}|[A-Z]{1,7}(?:[.-][A-Z]{1,2})?", value):
        return value
    return ""


def _date(value) -> str:
    if not isinstance(value, str):
        return ""
    match = re.match(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})(?:$|[T :])", value.strip())
    if not match:
        return ""
    from datetime import date

    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return ""


def _url(value) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            return ""
        if re.search(
            r"(?:^|&)(?:token|api_key|access_token|password|secret)=",
            parts.query,
            re.IGNORECASE,
        ):
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return ""


def _kind(logical: str) -> str:
    lower = logical.lower()
    suffix = PurePosixPath(logical).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".txt", ".md"}:
        return "text"
    if "announcement" in lower:
        return "announcement"
    if "fundamental" in lower or ".statements." in lower:
        return "fundamental"
    if "industry_documents" in lower:
        return "document_metadata"
    if "index_constituents" in lower:
        return "universe"
    if suffix == ".csv" and any(
        x in lower for x in ("/market/", "_history", "/historical/", "cache/data/")
    ):
        return "price"
    if lower.startswith(("data/analysis/", "cache/analysis/", "data/optimizer/")):
        return "analysis"
    if lower.startswith("data/instrument_audit/"):
        return "instrument_audit"
    if lower.startswith("data/optimizer_recovery/"):
        return "provider_response" if "/responses/" in lower else "recovery_metadata"
    return "metadata" if suffix in {".json", ".jsonl", ".csv", ".meta"} else "artifact"


def _dataset_root(logical: str) -> str:
    parts = list(PurePosixPath(logical).parts)
    for index, part in enumerate(parts[:-1]):
        if (
            part in {"market", "fundamentals", "capital_market", "provider_cache"}
            and index >= 2
        ):
            return PurePosixPath(*parts[:index]).as_posix()
    if len(parts) == 2 and parts[0] == "data" and parts[-1].endswith("_history.csv"):
        return "data/history"
    if len(parts) >= 5 and parts[:2] == ["data", "optimizer_recovery"]:
        return PurePosixPath(*parts[:4]).as_posix()
    if len(parts) > 3 and parts[:2] in (
        ["data", "analysis"],
        ["cache", "analysis"],
        ["data", "instrument_audit"],
        ["data", "reference_universe"],
        ["data", "optimizer"],
    ):
        return PurePosixPath(*parts[:3]).as_posix()
    return PurePosixPath(*parts[:2]).as_posix()


def _metadata(path: Path, logical: str, size: int) -> dict:
    kind = _kind(logical)
    result = {
        "kind": kind,
        "codes": set(),
        "dates": set(),
        "schema": {},
        "row_count": None,
        "parse_status": "not_applicable",
        "parse_error": "",
        "documents": [],
    }
    filename = PurePosixPath(logical).name
    match = re.match(r"^([0-9]{5,6}|[A-Z]{1,7}(?:\.[A-Z])?)(?:[_.]|$)", filename)
    filename_code = _code(match.group(1)) if match else ""
    if (
        filename_code
        and not filename_code.isdigit()
        and kind not in {"price", "fundamental"}
    ):
        filename_code = ""
    if filename_code:
        result["codes"].add(filename_code)

    def visit(node, inherited_code=""):
        if not isinstance(node, dict):
            return
        code = next(
            (value for key in CODE_KEYS if (value := _code(node.get(key)))),
            inherited_code,
        )
        if code:
            result["codes"].add(code)
        for key in DATE_KEYS:
            date_value = _date(node.get(key))
            if date_value:
                result["dates"].add(date_value)
        source = next((value for key in URL_KEYS if (value := _url(node.get(key)))), "")
        title = str(node.get("title") or node.get("document_title") or "")[:1000]
        published = next(
            (
                _date(node.get(key))
                for key in ("published_at", "publication_date", "date", "period_end")
                if _date(node.get(key))
            ),
            "",
        )
        text = next(
            (
                node.get(key)
                for key in ("content", "full_text", "document_text", "text")
                if isinstance(node.get(key), str) and node[key].strip()
            ),
            "",
        )
        financial_row = kind == "fundamental" and bool(node.get("period_end"))
        announcement_row = kind == "announcement" and bool(title or source or text)
        document_row = kind == "document_metadata" and bool(
            source or node.get("period_end")
        )
        if financial_row or announcement_row or document_row or (source and title):
            identity = source or "|".join(
                (kind, code, title, published, str(node.get("period_end", "")))
            )
            result["documents"].append(
                {
                    "document_id": _identifier("doc_", identity),
                    "source_url": source,
                    "code": code,
                    "title": title
                    or (
                        f"财报 {node.get('period_end', '')}"
                        if financial_row
                        else filename
                    ),
                    "publication_date": published,
                    "has_text": bool(text and not financial_row),
                    "linked_paths": [
                        node[key]
                        for key in DOCUMENT_PATH_KEYS
                        if isinstance(node.get(key), str)
                    ],
                }
            )
        return code

    suffix = PurePosixPath(logical).suffix.lower()
    try:
        if suffix in {".csv", ".tsv"}:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(
                    handle, delimiter="\t" if suffix == ".tsv" else ","
                )
                result["schema"] = {"columns": reader.fieldnames or []}
                result["row_count"] = 0
                columns = set(reader.fieldnames or [])
                code_columns = columns & CODE_KEYS
                date_columns = columns & DATE_KEYS
                raw_codes, raw_dates = set(), set()
                fast_table = kind not in {
                    "fundamental",
                    "announcement",
                    "document_metadata",
                } and not columns.intersection(URL_KEYS)
                for row in reader:
                    result["row_count"] += 1
                    if fast_table:
                        raw_codes.update(
                            row.get(key) for key in code_columns if row.get(key)
                        )
                        raw_dates.update(
                            row.get(key) for key in date_columns if row.get(key)
                        )
                    else:
                        visit(row, filename_code)
                result["codes"].update(
                    value for raw in raw_codes if (value := _code(raw))
                )
                result["dates"].update(
                    value for raw in raw_dates if (value := _date(raw))
                )
            result["parse_status"] = "parsed"
        elif suffix in {".json", ".meta", ".jsonl", ".ndjson"}:
            if size > MAX_JSON_BYTES:
                result.update(
                    parse_status="skipped_size_limit",
                    parse_error=f"JSON parser limit is {MAX_JSON_BYTES} bytes; file remains fully indexed",
                )
                return result
            with path.open("r", encoding="utf-8-sig") as handle:
                values = (
                    [json.loads(line) for line in handle if line.strip()]
                    if suffix in {".jsonl", ".ndjson"}
                    else json.load(handle)
                )
            result["schema"] = {"type": type(values).__name__}
            if isinstance(values, dict):
                result["schema"].update(
                    keys=list(values), contract=values.get("contract")
                )
            elif isinstance(values, list):
                result["row_count"] = len(values)
            pending = [(values, filename_code)]
            while pending:
                node, inherited = pending.pop()
                if isinstance(node, dict):
                    inherited = visit(node, inherited) or inherited
                    pending.extend(
                        (value, inherited)
                        for value in node.values()
                        if isinstance(value, (dict, list))
                    )
                elif isinstance(node, list):
                    pending.extend(
                        (value, inherited)
                        for value in node
                        if isinstance(value, (dict, list))
                    )
            result["parse_status"] = "parsed"
        elif suffix in {".txt", ".md", ".pdf"}:
            result["parse_status"] = "content_registered" if size else "empty_document"
            if suffix == ".pdf" and size:
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        result["parse_status"] = "parse_failed"
                        result["parse_error"] = (
                            "PDF extension has no PDF header; original document is missing"
                        )
            result["documents"].append(
                {
                    "document_id": _identifier("doc_", "path:" + logical),
                    "source_url": "",
                    "code": filename_code,
                    "title": filename,
                    "publication_date": "",
                    "has_text": suffix != ".pdf" and size > 0,
                }
            )
        else:
            result["parse_status"] = "unsupported_format"
            result["parse_error"] = (
                "Binary/other artifact indexed by hash; content parser unavailable"
            )
    except (OSError, UnicodeError, ValueError, csv.Error, RecursionError) as exc:
        result.update(
            parse_status="parse_failed",
            parse_error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    return result


class DatasetCatalog:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root
        self.db_path = self.root / "data" / "dataset_catalog" / "catalog.sqlite3"
        for path in (self.root / "data", self.db_path.parent, self.db_path):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("catalog storage must not use linked paths")
                if path == self.db_path and info.st_nlink != 1:
                    raise ValueError("catalog storage must not use hard links")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY, started REAL,
                    finished REAL, status TEXT, summary TEXT);
                CREATE TABLE IF NOT EXISTS datasets(dataset_id TEXT PRIMARY KEY,
                    logical_root TEXT, title TEXT);
                CREATE TABLE IF NOT EXISTS files(file_id TEXT PRIMARY KEY,
                    dataset_id TEXT, relative_path TEXT UNIQUE, logical_path TEXT,
                    sha256 TEXT, bytes INTEGER, mtime_ns INTEGER, kind TEXT,
                    date_start TEXT, date_end TEXT, schema_json TEXT, row_count INTEGER,
                    parse_status TEXT, parse_error TEXT, build_id TEXT, present INTEGER);
                CREATE INDEX IF NOT EXISTS files_dataset ON files(dataset_id,present);
                CREATE INDEX IF NOT EXISTS files_hash ON files(sha256);
                CREATE TABLE IF NOT EXISTS file_codes(file_id TEXT,code TEXT,
                    PRIMARY KEY(file_id,code));
                CREATE INDEX IF NOT EXISTS codes_lookup ON file_codes(code,file_id);
                CREATE TABLE IF NOT EXISTS scan_entries(build_id TEXT,relative_path TEXT,
                    logical_path TEXT,eligible INTEGER,status TEXT,reason TEXT,file_id TEXT,
                    PRIMARY KEY(build_id,relative_path,status));
                CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,
                    source_url TEXT,title TEXT,publication_date TEXT,status TEXT,
                    attempts INTEGER DEFAULT 0,error TEXT DEFAULT '',updated REAL);
                CREATE TABLE IF NOT EXISTS document_sources(document_id TEXT,file_id TEXT,
                    dataset_id TEXT,code TEXT,PRIMARY KEY(document_id,file_id,code));
                CREATE INDEX IF NOT EXISTS document_source_files ON document_sources(file_id);
                CREATE INDEX IF NOT EXISTS document_source_datasets ON document_sources(dataset_id,document_id);
                CREATE TABLE IF NOT EXISTS document_artifacts(document_id TEXT,file_id TEXT,
                    kind TEXT,PRIMARY KEY(document_id,file_id));
                CREATE TABLE IF NOT EXISTS document_hints(document_id TEXT,source_file_id TEXT,
                    logical_path TEXT,PRIMARY KEY(document_id,source_file_id,logical_path));
            """)
            # Normal readers must remain lock-free while a WAL build is running.
            if self._has_legacy_dataset_uniqueness(db):
                # Serialize only migration; another constructor may finish first.
                db.execute("BEGIN IMMEDIATE")
                if self._has_legacy_dataset_uniqueness(db):
                    # References are text identifiers; preserve every existing ID.
                    db.execute(
                        "CREATE TABLE datasets_v2(dataset_id TEXT PRIMARY KEY,"
                        "logical_root TEXT,title TEXT)"
                    )
                    db.execute("INSERT INTO datasets_v2 SELECT * FROM datasets")
                    db.execute("DROP TABLE datasets")
                    db.execute("ALTER TABLE datasets_v2 RENAME TO datasets")

    @staticmethod
    def _has_legacy_dataset_uniqueness(db) -> bool:
        return any(
            index["unique"]
            and [
                column["name"]
                for column in db.execute(
                    "SELECT name FROM pragma_index_info(?)", (index["name"],)
                )
            ]
            == ["logical_root"]
            for index in db.execute("PRAGMA index_list(datasets)").fetchall()
        )

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _index(self, db, item: dict, build_id: str, discover_documents=True) -> dict:
        path = _checked_file(self.root, item["relative_path"])
        reason = _business_file_reason(path)
        if reason:
            raise ValueError(reason)
        before = path.stat()
        digest = _sha256(path)
        metadata = _metadata(path, item["logical_path"], before.st_size)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise ValueError("file changed during indexing")
        _checked_file(self.root, item["relative_path"])
        logical_root = _dataset_root(item["logical_path"])
        # Keep the complete import provenance prefix, including nested snapshots.
        # Native paths have no prefix and retain their historical dataset IDs.
        if not item["relative_path"].endswith(item["logical_path"]):
            raise ValueError("physical path does not preserve logical provenance")
        physical_root = (
            item["relative_path"][: -len(item["logical_path"])] + logical_root
        )
        dataset_id = _identifier("ds_", physical_root)
        file_id = _identifier("file_", item["relative_path"])
        dates = sorted(metadata["dates"])
        db.execute(
            "INSERT OR IGNORE INTO datasets VALUES(?,?,?)",
            (dataset_id, logical_root, physical_root),
        )
        db.execute(
            """INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(file_id) DO UPDATE SET dataset_id=excluded.dataset_id,
            logical_path=excluded.logical_path,sha256=excluded.sha256,bytes=excluded.bytes,
            mtime_ns=excluded.mtime_ns,kind=excluded.kind,date_start=excluded.date_start,
            date_end=excluded.date_end,schema_json=excluded.schema_json,row_count=excluded.row_count,
            parse_status=excluded.parse_status,parse_error=excluded.parse_error,
            build_id=excluded.build_id,present=1""",
            (
                file_id,
                dataset_id,
                item["relative_path"],
                item["logical_path"],
                digest,
                before.st_size,
                before.st_mtime_ns,
                metadata["kind"],
                dates[0] if dates else "",
                dates[-1] if dates else "",
                _json(metadata["schema"]),
                metadata["row_count"],
                metadata["parse_status"],
                metadata["parse_error"],
                build_id,
            ),
        )
        db.execute("DELETE FROM file_codes WHERE file_id=?", (file_id,))
        db.executemany(
            "INSERT OR IGNORE INTO file_codes VALUES(?,?)",
            ((file_id, code) for code in metadata["codes"]),
        )
        managed_artifact = (
            item["relative_path"].startswith("data/dataset_documents/")
            and db.execute(
                "SELECT 1 FROM document_artifacts WHERE file_id=?", (file_id,)
            ).fetchone()
        )
        if managed_artifact:
            self._sync_artifact_sources(db, file_id)
        discover_documents = discover_documents and not managed_artifact
        if discover_documents:
            db.execute("DELETE FROM document_sources WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM document_artifacts WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM document_hints WHERE source_file_id=?", (file_id,))
        for doc in metadata["documents"] if discover_documents else []:
            available = doc["has_text"] or (
                metadata["kind"] == "pdf"
                and metadata["parse_status"] == "content_registered"
            )
            status = (
                "available"
                if available
                else "missing_document"
                if doc["source_url"]
                else "missing_url"
            )
            db.execute(
                """INSERT INTO documents VALUES(?,?,?,?,?,0,'',?)
                ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,
                publication_date=excluded.publication_date,updated=excluded.updated,
                status=CASE WHEN excluded.status='available' THEN 'available' ELSE documents.status END""",
                (
                    doc["document_id"],
                    doc["source_url"],
                    doc["title"],
                    doc["publication_date"],
                    status,
                    time.time(),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO document_sources VALUES(?,?,?,?)",
                (doc["document_id"], file_id, dataset_id, doc["code"]),
            )
            for hint in doc.get("linked_paths", []):
                normalized = hint.replace("\\", "/")
                candidates = [normalized]
                for marker in ("/cache/", "/data/"):
                    if marker in normalized:
                        candidates.append(normalized.split(marker, 1)[1])
                        candidates[-1] = marker.strip("/") + "/" + candidates[-1]
                for candidate in candidates:
                    if not eligibility_reason(candidate):
                        db.execute(
                            "INSERT OR IGNORE INTO document_hints VALUES(?,?,?)",
                            (doc["document_id"], file_id, _logical_path(candidate)),
                        )
            if available:
                db.execute(
                    "INSERT OR IGNORE INTO document_artifacts VALUES(?,?,?)",
                    (
                        doc["document_id"],
                        file_id,
                        "pdf" if metadata["kind"] == "pdf" else "text",
                    ),
                )
        return {
            "file_id": file_id,
            "dataset_id": dataset_id,
            "sha256": digest,
            "bytes": before.st_size,
            "parse_status": metadata["parse_status"],
        }

    def build(self) -> dict:
        build_id = uuid.uuid4().hex
        counts = {
            "eligible_files": 0,
            "indexed_files": 0,
            "skipped_entries": 0,
            "index_failures": 0,
            "parse_failures": 0,
            "unparsed_files": 0,
            "manifest_gaps": 0,
            "scan_errors": 0,
        }
        manifests = []
        with self._connect() as db:
            db.execute(
                "INSERT INTO builds VALUES(?,?,NULL,'running','{}')",
                (build_id, time.time()),
            )
        try:
            with self._connect() as db:
                for item in iter_catalog_entries(self.root):
                    result = {}
                    reason = item["reason"]
                    status = "skipped"
                    if item["eligible"]:
                        counts["eligible_files"] += 1
                        try:
                            result = self._index(db, item, build_id)
                            status = "indexed"
                            counts["indexed_files"] += 1
                            counts["parse_failures"] += (
                                result["parse_status"] == "parse_failed"
                            )
                            counts["unparsed_files"] += result["parse_status"] in {
                                "unsupported_format",
                                "skipped_size_limit",
                            }
                            if item["relative_path"].endswith(
                                "/manifest.json"
                            ) and item["relative_path"].startswith(
                                ("data/dataset_imports/", "data/server_imports/")
                            ):
                                manifests.append(item["relative_path"])
                        except (OSError, ValueError) as exc:
                            counts["index_failures"] += 1
                            status, reason = (
                                "index_failed",
                                f"{type(exc).__name__}: {str(exc)[:300]}",
                            )
                    else:
                        counts["skipped_entries"] += 1
                        counts["scan_errors"] += reason.startswith(
                            ("stat_error:", "directory_error:", "content_check_error:")
                        )
                    db.execute(
                        "INSERT OR REPLACE INTO scan_entries VALUES(?,?,?,?,?,?,?)",
                        (
                            build_id,
                            item["relative_path"],
                            item["logical_path"],
                            int(item["eligible"]),
                            status,
                            reason,
                            result.get("file_id", ""),
                        ),
                    )
                counts["manifest_gaps"] = self._verify_manifests(
                    db, manifests, build_id
                )
                db.execute("UPDATE files SET present=0 WHERE build_id<>?", (build_id,))
                db.execute("""INSERT OR IGNORE INTO document_artifacts
                    SELECT h.document_id,f.file_id,f.kind FROM document_hints h
                    JOIN files source ON source.file_id=h.source_file_id AND source.present=1
                    JOIN files f ON f.logical_path=h.logical_path AND f.present=1
                    WHERE f.kind IN ('pdf','text') AND f.parse_status='content_registered'""")
                aliases = db.execute("""SELECT DISTINCT old.document_id old_id,
                    target.document_id target_id,f.logical_path FROM documents old
                    JOIN document_artifacts a ON a.document_id=old.document_id
                    JOIN files f ON f.file_id=a.file_id AND f.present=1
                    JOIN document_artifacts other ON other.file_id=f.file_id
                    JOIN documents target ON target.document_id=other.document_id
                    WHERE old.source_url='' AND target.source_url<>''""").fetchall()
                for alias in aliases:
                    if alias["old_id"] != _identifier(
                        "doc_", "path:" + alias["logical_path"]
                    ):
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO document_sources SELECT ?,file_id,dataset_id,code FROM document_sources WHERE document_id=?",
                        (alias["target_id"], alias["old_id"]),
                    )
                    db.execute(
                        "DELETE FROM document_sources WHERE document_id=?",
                        (alias["old_id"],),
                    )
                    db.execute(
                        "DELETE FROM document_artifacts WHERE document_id=?",
                        (alias["old_id"],),
                    )
                    db.execute(
                        "DELETE FROM documents WHERE document_id=?", (alias["old_id"],)
                    )
                db.execute("""UPDATE documents SET status='available' WHERE EXISTS(
                    SELECT 1 FROM document_artifacts a JOIN files f ON a.file_id=f.file_id
                    WHERE a.document_id=documents.document_id AND f.present=1)""")
                db.execute("""UPDATE documents SET status=CASE WHEN source_url='' THEN 'missing_url' ELSE 'missing_document' END
                    WHERE status='available' AND NOT EXISTS(SELECT 1 FROM document_artifacts a JOIN files f ON a.file_id=f.file_id
                    WHERE a.document_id=documents.document_id AND f.present=1)""")
                summary = {
                    "build_id": build_id,
                    **counts,
                    "enumeration_complete": not (
                        counts["index_failures"] or counts["scan_errors"]
                    ),
                    "metadata_complete": not (
                        counts["parse_failures"] or counts["unparsed_files"]
                    ),
                    "transfer_complete": counts["manifest_gaps"] == 0,
                    "analysis_ready": None,
                    "readiness_status": "not_validated",
                    "notice": READINESS_NOTICE,
                }
                summary["unique_security_count"] = db.execute(
                    "SELECT COUNT(DISTINCT c.code) FROM file_codes c JOIN files f ON c.file_id=f.file_id WHERE f.present=1"
                ).fetchone()[0]
                summary["unique_content_count"], summary["unique_bytes"] = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM (SELECT sha256,MAX(bytes) bytes FROM files WHERE present=1 GROUP BY sha256)"
                ).fetchone()
                state = (
                    "completed"
                    if summary["enumeration_complete"] and summary["transfer_complete"]
                    else "completed_with_gaps"
                )
                db.execute(
                    "UPDATE builds SET finished=?,status=?,summary=? WHERE id=?",
                    (time.time(), state, _json(summary), build_id),
                )
                manifest_path = self.db_path.parent / f"manifest_{build_id}.jsonl"
                with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(_json({"type": "summary", **summary}) + "\n")
                    for row in db.execute(
                        """SELECT s.*,f.sha256,f.bytes,f.mtime_ns,
                        f.parse_status,f.parse_error FROM scan_entries s LEFT JOIN files f
                        ON f.file_id=s.file_id WHERE s.build_id=? ORDER BY s.relative_path,s.status""",
                        (build_id,),
                    ):
                        handle.write(_json(dict(row)) + "\n")
            return {
                **summary,
                "manifest_path": manifest_path.relative_to(self.root).as_posix(),
            }
        except BaseException:
            with self._connect() as db:
                db.execute(
                    "UPDATE builds SET finished=?,status='failed',summary=? WHERE id=?",
                    (time.time(), _json(counts), build_id),
                )
            raise

    def index_paths(self, relative_paths: list[str]) -> dict:
        """Incrementally register trusted, already-present business files.

        This updates only the supplied files and never marks the rest of a
        full-catalog snapshot absent. The incremental build status makes clear
        that a complete enumeration was not performed.
        """
        if (
            not isinstance(relative_paths, list)
            or not relative_paths
            or len(relative_paths) > 100
            or any(not isinstance(value, str) for value in relative_paths)
        ):
            raise ValueError("relative_paths must contain 1 to 100 file paths")
        normalized = list(dict.fromkeys(_safe_relative(value) for value in relative_paths))
        for relative in normalized:
            if eligibility_reason(relative):
                raise ValueError("file is outside the business-data policy")
        build_id = "incremental-" + uuid.uuid4().hex
        counts = {"indexed_files": 0, "index_failures": 0}
        results = []
        started = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO builds VALUES(?,?,NULL,'running',?)",
                (build_id, started, _json({"incremental": True})),
            )
            for relative in normalized:
                item = {
                    "relative_path": relative,
                    "logical_path": _logical_path(relative),
                }
                try:
                    result = self._index(db, item, build_id)
                    db.execute(
                        "INSERT OR REPLACE INTO scan_entries VALUES(?,?,?,?,?,?,?)",
                        (
                            build_id,
                            relative,
                            item["logical_path"],
                            1,
                            "indexed",
                            "",
                            result["file_id"],
                        ),
                    )
                    counts["indexed_files"] += 1
                    results.append({"path": relative, **result})
                except (OSError, ValueError) as exc:
                    counts["index_failures"] += 1
                    db.execute(
                        "INSERT OR REPLACE INTO scan_entries VALUES(?,?,?,?,?,?,?)",
                        (
                            build_id,
                            relative,
                            item["logical_path"],
                            1,
                            "index_failed",
                            f"{type(exc).__name__}: {str(exc)[:300]}",
                            "",
                        ),
                    )
                    results.append(
                        {"path": relative, "status": "failed", "error": type(exc).__name__}
                    )
            summary = {
                "incremental": True,
                **counts,
                "enumeration_complete": False,
                "notice": "仅增量登记指定文件；完整目录状态以最近一次全量构建为准。",
            }
            db.execute(
                "UPDATE builds SET finished=?,status=?,summary=? WHERE id=?",
                (
                    time.time(),
                    "incremental_with_gaps" if counts["index_failures"] else "incremental",
                    _json(summary),
                    build_id,
                ),
            )
        return {"build_id": build_id, **summary, "files": results}

    def _verify_manifests(self, db, paths, build_id):
        gaps = 0
        for relative in paths:
            try:
                path = _checked_file(self.root, relative)
                if path.stat().st_size > MAX_JSON_BYTES:
                    raise ValueError("transfer manifest exceeds parser limit")
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                members = value.get("files")
                if not isinstance(members, list):
                    raise TypeError("transfer manifest files must be a list")
                for ordinal, member in enumerate(members):
                    label = f"{relative}#entry-{ordinal}"
                    source = ""
                    try:
                        source = _safe_relative(
                            member.get("path", member.get("relative_path", ""))
                        )
                        if _excluded(source):
                            raise ValueError(
                                "manifest member is excluded by business-data policy"
                            )
                        candidates = [
                            (path.parent / source).relative_to(self.root).as_posix(),
                            (path.parent / "dataset" / source)
                            .relative_to(self.root)
                            .as_posix(),
                        ]
                        record = next(
                            (
                                row
                                for candidate in candidates
                                if (
                                    row := db.execute(
                                        "SELECT sha256,bytes FROM files WHERE relative_path=? AND build_id=?",
                                        (candidate, build_id),
                                    ).fetchone()
                                )
                            ),
                            None,
                        )
                        if record is None:
                            raise ValueError("manifest member is missing or excluded")
                        if (
                            member.get("sha256")
                            and record["sha256"] != member["sha256"]
                        ):
                            raise ValueError("manifest member SHA256 mismatch")
                        if "bytes" in member and record["bytes"] != member["bytes"]:
                            raise ValueError("manifest member size mismatch")
                    except (TypeError, AttributeError, ValueError) as exc:
                        gaps += 1
                        reason = str(exc)
                        if source:
                            reason += f"; source_path={source}"
                        db.execute(
                            "INSERT OR REPLACE INTO scan_entries VALUES(?,?,?,?,?,?,?)",
                            (build_id, label, "", 0, "manifest_gap", reason, ""),
                        )
            except (
                OSError,
                UnicodeError,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                gaps += 1
                db.execute(
                    "INSERT OR REPLACE INTO scan_entries VALUES(?,?,?,?,?,?,?)",
                    (build_id, relative, "", 0, "manifest_gap", str(exc)[:300], ""),
                )
        return gaps

    @staticmethod
    def _page(limit, offset):
        if (
            type(limit) is not int
            or not 1 <= limit <= PAGE_LIMIT
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("limit must be 1..100 and offset must be non-negative")
        return limit, offset

    def index_status(self) -> dict:
        with self._connect() as db:
            latest = db.execute(
                "SELECT * FROM builds ORDER BY started DESC LIMIT 1"
            ).fetchone()
            completed = db.execute(
                "SELECT finished FROM builds WHERE status IN ('completed','completed_with_gaps') ORDER BY finished DESC LIMIT 1"
            ).fetchone()
            gap_total = 0
            gap_examples = []
            if latest is not None:
                gaps_where = """build_id=? AND (
                    status IN ('index_failed','manifest_gap') OR
                    (status='skipped' AND (
                        instr(reason,'stat_error:')=1 OR
                        instr(reason,'directory_error:')=1 OR
                        instr(reason,'content_check_error:')=1)))"""
                gap_total = db.execute(
                    "SELECT COUNT(*) FROM scan_entries WHERE " + gaps_where,
                    (latest["id"],),
                ).fetchone()[0]
                gap_examples = [
                    dict(row)
                    for row in db.execute(
                        "SELECT relative_path,status,reason FROM scan_entries WHERE "
                        + gaps_where
                        + " ORDER BY relative_path,status LIMIT 10",
                        (latest["id"],),
                    )
                ]
        if latest is None:
            return {
                "index_status": "not_built",
                "indexed_at": None,
                "indexed_at_iso": None,
                "gap_total": 0,
                "gap_examples": [],
                "index_notice": "尚未构建数据目录；空列表不代表主机没有业务数据。",
            }
        return {
            "index_status": latest["status"],
            "indexed_at": completed["finished"] if completed else None,
            "indexed_at_iso": datetime.fromtimestamp(
                completed["finished"], timezone(timedelta(hours=8))
            ).isoformat()
            if completed
            else None,
            "build_started_at": latest["started"],
            "build_id": latest["id"],
            "build_summary": json.loads(latest["summary"]),
            "gap_total": gap_total,
            "gap_examples": gap_examples,
            "index_notice": "索引正在构建，当前结果可能包含旧快照，尚未刷新完成。"
            if latest["status"] == "running"
            else "最近一次为增量登记，完整目录状态以最近一次全量构建为准。"
            if latest["status"].startswith("incremental")
            else READINESS_NOTICE,
        }

    def list_datasets(self, limit=20, offset=0) -> dict:
        limit, offset = self._page(limit, offset)
        with self._connect() as db:
            total = db.execute(
                "SELECT COUNT(DISTINCT dataset_id) FROM files WHERE present=1"
            ).fetchone()[0]
            rows = db.execute(
                """SELECT d.*,COUNT(f.file_id) AS file_count,
                COUNT(DISTINCT f.sha256) AS unique_content_count,SUM(f.bytes) AS source_bytes,
                MIN(NULLIF(f.date_start,'')) AS date_start,MAX(NULLIF(f.date_end,'')) AS date_end
                FROM datasets d JOIN files f ON d.dataset_id=f.dataset_id AND f.present=1
                GROUP BY d.dataset_id ORDER BY d.logical_root,d.title LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["physical_root"] = item["title"]
                item["unique_security_count"] = db.execute(
                    """SELECT COUNT(DISTINCT c.code)
                    FROM file_codes c JOIN files f ON c.file_id=f.file_id
                    WHERE f.dataset_id=? AND f.present=1""",
                    (row["dataset_id"],),
                ).fetchone()[0]
                item.update(readiness_status="not_validated", analysis_ready=None)
                items.append(item)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            **self.index_status(),
            "notice": READINESS_NOTICE,
        }

    def details(self, dataset_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown dataset_id")
            item = dict(row)
            item["physical_root"] = item["title"]
            counts = db.execute(
                """SELECT COUNT(*) AS file_count,COUNT(DISTINCT sha256)
                AS unique_content_count,SUM(bytes) AS source_bytes FROM files
                WHERE dataset_id=? AND present=1""",
                (dataset_id,),
            ).fetchone()
            item.update(dict(counts))
            item["kinds"] = {
                r["kind"]: r["n"]
                for r in db.execute(
                    "SELECT kind,COUNT(*) n FROM files WHERE dataset_id=? AND present=1 GROUP BY kind",
                    (dataset_id,),
                )
            }
            item["parse_statuses"] = {
                r["parse_status"]: r["n"]
                for r in db.execute(
                    "SELECT parse_status,COUNT(*) n FROM files WHERE dataset_id=? AND present=1 GROUP BY parse_status",
                    (dataset_id,),
                )
            }
            item["unique_security_count"] = db.execute(
                "SELECT COUNT(DISTINCT c.code) FROM file_codes c JOIN files f ON c.file_id=f.file_id WHERE f.dataset_id=? AND f.present=1",
                (dataset_id,),
            ).fetchone()[0]
        item.update(
            readiness_status="not_validated",
            analysis_ready=None,
            notice=READINESS_NOTICE,
        )
        try:
            item["relative_root"] = (
                self.resolve_dataset_root(dataset_id).relative_to(self.root).as_posix()
            )
        except ValueError:
            item["relative_root"] = None
        item.update(self.index_status())
        item["document_statuses"] = self._document_status_counts(dataset_id)
        item["document_counts_notice"] = DOCUMENT_COUNTS_NOTICE
        return item

    def files(
        self, dataset_id=None, kind=None, code=None, query=None, limit=20, offset=0
    ):
        limit, offset = self._page(limit, offset)
        clauses, parameters = ["f.present=1"], []
        for column, value in (("dataset_id", dataset_id), ("kind", kind)):
            if value is not None:
                clauses.append(f"f.{column}=?")
                parameters.append(value)
        if code is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM file_codes c WHERE c.file_id=f.file_id AND c.code=?)"
            )
            parameters.append(_code(code))
        if query is not None:
            if not isinstance(query, str) or len(query) > 256:
                raise ValueError("query must be at most 256 characters")
            clauses.append("instr(lower(f.logical_path),lower(?))>0")
            parameters.append(query)
        where = " AND ".join(clauses)
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM files f WHERE {where}", parameters
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT f.* FROM files f WHERE {where} ORDER BY f.relative_path LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["schema"] = json.loads(item.pop("schema_json"))
                item["codes"] = [
                    r[0]
                    for r in db.execute(
                        "SELECT code FROM file_codes WHERE file_id=? ORDER BY code",
                        (row["file_id"],),
                    )
                ]
                item["source_copy_count"] = db.execute(
                    "SELECT COUNT(*) FROM files WHERE sha256=? AND present=1",
                    (row["sha256"],),
                ).fetchone()[0]
                items.append(item)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            **self.index_status(),
        }

    def documents(self, dataset_id=None, code=None, status=None, limit=20, offset=0):
        limit, offset = self._page(limit, offset)
        clauses, args = ["f.present=1"], []
        if dataset_id is not None:
            clauses.append("s.dataset_id=?")
            args.append(dataset_id)
        if code is not None:
            clauses.append("s.code=?")
            args.append(_code(code))
        if status is not None:
            clauses.append("d.status=?")
            args.append(status)
        where = " AND ".join(clauses)
        join = "documents d JOIN document_sources s ON d.document_id=s.document_id JOIN files f ON s.file_id=f.file_id"
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(DISTINCT d.document_id) FROM {join} WHERE {where}", args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT DISTINCT d.* FROM {join} WHERE {where} ORDER BY d.document_id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = [self._document(db, row) for row in rows]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            **self.index_status(),
            "status_counts": self._document_status_counts(dataset_id),
            "document_counts_notice": DOCUMENT_COUNTS_NOTICE,
        }

    def _document_status_counts(self, dataset_id=None):
        condition = "AND s.dataset_id=?" if dataset_id is not None else ""
        args = (dataset_id,) if dataset_id is not None else ()
        with self._connect() as db:
            rows = db.execute(
                f"SELECT d.status,COUNT(DISTINCT d.document_id) n FROM documents d JOIN document_sources s ON d.document_id=s.document_id JOIN files f ON s.file_id=f.file_id WHERE f.present=1 {condition} GROUP BY d.status",
                args,
            ).fetchall()
            missing_url = db.execute(
                f"SELECT COUNT(DISTINCT d.document_id) FROM documents d JOIN document_sources s ON d.document_id=s.document_id JOIN files f ON s.file_id=f.file_id WHERE f.present=1 AND d.source_url='' {condition}",
                args,
            ).fetchone()[0]
            artifacts = db.execute(
                f"""WITH scoped AS (
                    SELECT DISTINCT d.document_id FROM documents d
                    JOIN document_sources s ON d.document_id=s.document_id
                    JOIN files f ON s.file_id=f.file_id
                    WHERE f.present=1 {condition}
                ), present_artifacts AS (
                    SELECT a.document_id, MAX(a.kind='pdf') pdf,
                           MAX(a.kind='text') text
                    FROM document_artifacts a JOIN files af ON af.file_id=a.file_id
                    WHERE af.present=1 GROUP BY a.document_id
                )
                SELECT COUNT(*) total, COALESCE(SUM(a.pdf),0) pdf,
                       COALESCE(SUM(a.text),0) text,
                       COALESCE(SUM(a.pdf=1 AND a.text=1),0) paired
                FROM scoped s LEFT JOIN present_artifacts a
                ON a.document_id=s.document_id""",
                args,
            ).fetchone()
        return {
            **{row["status"]: row["n"] for row in rows},
            "source_url_missing": missing_url,
            "source_pdf_missing": artifacts["total"] - artifacts["pdf"],
            "documents_with_pdf": artifacts["pdf"],
            "documents_with_text": artifacts["text"],
            "documents_with_pdf_and_text": artifacts["paired"],
        }

    def document_jobs(self, status=None, limit=20, offset=0, **filters):
        limit, offset = self._page(limit, offset)
        allowed = {"dataset_id", "code", "job_ids"}
        if set(filters) - allowed:
            raise ValueError("unknown document job filter")
        clauses, args = [], []
        if status is not None:
            clauses.append("d.status=?")
            args.append(status)
        if filters.get("dataset_id") is not None:
            clauses.append("s.dataset_id=?")
            args.append(filters["dataset_id"])
        if filters.get("code") is not None:
            clauses.append("s.code=?")
            args.append(_code(filters["code"]))
        if filters.get("job_ids") is not None:
            job_ids = filters["job_ids"]
            if (
                not isinstance(job_ids, (list, tuple, set))
                or not 1 <= len(job_ids) <= 100
                or any(not isinstance(value, str) for value in job_ids)
            ):
                raise ValueError("job_ids must contain 1..100 strings")
            clauses.append("d.document_id IN (" + ",".join("?" for _ in job_ids) + ")")
            args.extend(sorted(job_ids))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            total = db.execute(
                "SELECT COUNT(DISTINCT d.document_id) FROM documents d "
                "JOIN document_sources s ON s.document_id=d.document_id "
                "LEFT JOIN files f ON f.file_id=s.file_id AND f.present=1"
                + where,
                args,
            ).fetchone()[0]
            rows = db.execute(
                "SELECT DISTINCT d.* FROM documents d "
                "JOIN document_sources s ON s.document_id=d.document_id "
                "LEFT JOIN files f ON f.file_id=s.file_id AND f.present=1"
                + where
                + " ORDER BY d.document_id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = [self._document(db, row) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _document(self, db, row):
        item = dict(row)
        sources = db.execute(
            "SELECT s.* FROM document_sources s WHERE s.document_id=? ORDER BY s.file_id,s.code",
            (row["document_id"],),
        ).fetchall()
        artifacts = db.execute(
            "SELECT f.file_id,f.relative_path,a.kind FROM document_artifacts a JOIN files f ON a.file_id=f.file_id WHERE a.document_id=? AND f.present=1 ORDER BY a.kind,f.relative_path",
            (row["document_id"],),
        ).fetchall()
        pdf = next((dict(value) for value in artifacts if value["kind"] == "pdf"), None)
        text = next(
            (dict(value) for value in artifacts if value["kind"] == "text"), None
        )
        source = next(
            (dict(value) for value in sources if value["file_id"]),
            dict(sources[0]) if sources else {},
        )
        item.update(
            job_id=row["document_id"],
            dataset_id=source.get("dataset_id"),
            source_file_id=source.get("file_id"),
            code=source.get("code", ""),
            codes=sorted({value["code"] for value in sources if value["code"]}),
            dataset_ids=sorted({value["dataset_id"] for value in sources}),
            document_file_id=(pdf or text or {}).get("file_id"),
            existing_paths=[value["relative_path"] for value in artifacts],
            artifact_paths=[dict(value) for value in artifacts],
            original_path=(pdf or text or {}).get("relative_path"),
            text_path=(text or {}).get("relative_path"),
            source_url_missing=not bool(row["source_url"]),
            source_pdf_missing=pdf is None,
        )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_downloads'"
        ).fetchone():
            download = db.execute(
                "SELECT * FROM document_downloads WHERE job_id=?", (row["document_id"],)
            ).fetchone()
            if download:
                record = dict(download)
                item.update(
                    download_status=record.get("status"),
                    download_error=record.get("error"),
                    download_attempts=record.get("attempts"),
                    next_attempt_at=record.get("next_attempt_at"),
                )
                try:
                    item["download_result"] = json.loads(record.get("result") or "{}")
                except (ValueError, TypeError):
                    item["download_result"] = {
                        "error": "stored download result is malformed"
                    }
        return item

    def register_document_source(
        self, document_id, source_url, title, publication_date, code
    ):
        """Persist an official source before archive/download workers execute it."""
        document_id = str(document_id)
        source_url = str(source_url)
        title = str(title).strip()
        code = _code(code)
        parsed = urlsplit(source_url)
        if (
            not re.fullmatch(r"cninfo_[0-9a-f]{64}", document_id)
            or parsed.scheme != "https"
            or parsed.hostname != "static.cninfo.com.cn"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or not parsed.path.lower().endswith(".pdf")
            or not re.fullmatch(r"\d{6}", code)
            or not title
            or len(title) > 500
        ):
            raise ValueError("invalid CNINFO report source")
        try:
            if date.fromisoformat(str(publication_date)).isoformat() != publication_date:
                raise ValueError
        except ValueError as exc:
            raise ValueError("publication_date must be YYYY-MM-DD") from exc
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,0,'',?) "
                "ON CONFLICT(document_id) DO UPDATE SET source_url=excluded.source_url,"
                "title=excluded.title,publication_date=excluded.publication_date,"
                "status=CASE WHEN documents.status='available' THEN documents.status "
                "ELSE 'pending' END,updated=excluded.updated",
                (document_id, source_url, title, publication_date, "pending", now),
            )
            db.execute(
                "INSERT OR IGNORE INTO document_sources VALUES(?,?,?,?)",
                (document_id, "", "", code),
            )
        return document_id

    def report_parse_state(self, document_id):
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS report_parse_jobs ("
                "document_id TEXT PRIMARY KEY,status TEXT NOT NULL,"
                "updated REAL NOT NULL,result_json TEXT NOT NULL DEFAULT '{}')"
            )
            row = db.execute(
                "SELECT * FROM report_parse_jobs WHERE document_id=?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json"))
        except (ValueError, TypeError):
            result["result"] = {"status": "invalid_persisted_result"}
        return result

    def set_report_parse_state(self, document_id, status, result):
        if status not in {"pending", "running", "succeeded", "partial", "failed"}:
            raise ValueError("invalid report parse state")
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS report_parse_jobs ("
                "document_id TEXT PRIMARY KEY,status TEXT NOT NULL,"
                "updated REAL NOT NULL,result_json TEXT NOT NULL DEFAULT '{}')"
            )
            db.execute(
                "INSERT INTO report_parse_jobs VALUES(?,?,?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET status=excluded.status,"
                "updated=excluded.updated,result_json=excluded.result_json",
                (
                    document_id,
                    status,
                    time.time(),
                    json.dumps(result, ensure_ascii=False, allow_nan=False),
                ),
            )

    def claim_document_job(self, job_id):
        with self._connect() as db:
            result = db.execute(
                "UPDATE documents SET status='running',attempts=attempts+1,updated=? WHERE document_id=? AND status IN ('missing_document','failed')",
                (time.time(), job_id),
            )
            if not result.rowcount:
                return None
            return self._document(
                db,
                db.execute(
                    "SELECT * FROM documents WHERE document_id=?", (job_id,)
                ).fetchone(),
            )

    def record_document(self, job_id, relative_path=None, status="available", error=""):
        if status not in {"available", "failed", "blocked", "pending", "running"}:
            raise ValueError("invalid document status")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE document_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown document job")
            if relative_path:
                relative_path = _safe_relative(relative_path)
                if not relative_path.startswith("data/dataset_documents/"):
                    raise ValueError(
                        "worker artifacts must be under data/dataset_documents"
                    )
                path = _checked_file(self.root, relative_path)
                if (
                    path.suffix.lower() not in {".pdf", ".txt", ".md"}
                    or not path.stat().st_size
                ):
                    raise ValueError("artifact must be a nonempty PDF or text document")
                if path.suffix.lower() == ".pdf":
                    with path.open("rb") as handle:
                        if handle.read(5) != b"%PDF-":
                            raise ValueError("artifact has no PDF header")
                info = self._index(
                    db,
                    {"relative_path": relative_path, "logical_path": relative_path},
                    "document-worker",
                    discover_documents=False,
                )
                kind = "pdf" if path.suffix.lower() == ".pdf" else "text"
                db.execute(
                    "INSERT OR IGNORE INTO document_artifacts VALUES(?,?,?)",
                    (job_id, info["file_id"], kind),
                )
                self._sync_artifact_sources(db, info["file_id"])
            elif status == "available":
                raise ValueError("available document requires an artifact")
            db.execute(
                "UPDATE documents SET status=?,error=?,updated=? WHERE document_id=?",
                (status, str(error)[:2000], time.time(), job_id),
            )
            return self._document(
                db,
                db.execute(
                    "SELECT * FROM documents WHERE document_id=?", (job_id,)
                ).fetchone(),
            )

    @staticmethod
    def _sync_artifact_sources(db, file_id: str) -> None:
        """Associate an artifact with its own dataset and source securities."""
        file = db.execute(
            "SELECT dataset_id FROM files WHERE file_id=?", (file_id,)
        ).fetchone()
        if file is None:
            return
        jobs = db.execute(
            "SELECT document_id FROM document_artifacts WHERE file_id=?", (file_id,)
        ).fetchall()
        for job in jobs:
            codes = {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT code FROM document_sources WHERE document_id=? AND code<>''",
                    (job["document_id"],),
                )
            }
            for code in codes or {""}:
                db.execute(
                    """INSERT INTO document_sources VALUES(?,?,?,?)
                    ON CONFLICT(document_id,file_id,code) DO UPDATE SET dataset_id=excluded.dataset_id""",
                    (job["document_id"], file_id, file["dataset_id"], code),
                )
                if code:
                    db.execute(
                        "INSERT OR IGNORE INTO file_codes VALUES(?,?)", (file_id, code)
                    )
            if codes:
                db.execute(
                    "DELETE FROM document_sources WHERE document_id=? AND file_id=? AND code=''",
                    (job["document_id"], file_id),
                )

    def repair_document_links(self) -> dict:
        """Migrate existing managed PDF/text links without changing artifacts."""
        with self._connect() as db:
            files = db.execute("""SELECT DISTINCT f.file_id FROM document_artifacts a
                JOIN files f ON a.file_id=f.file_id WHERE f.present=1
                AND f.relative_path LIKE 'data/dataset_documents/%'""").fetchall()
            for file in files:
                self._sync_artifact_sources(db, file["file_id"])
        return {"managed_files_checked": len(files), "downloads_performed": 0}

    def resolve_dataset_root(self, dataset_id: str) -> Path:
        with self._connect() as db:
            dataset = db.execute(
                "SELECT logical_root FROM datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if dataset is None:
                raise ValueError("unknown dataset_id")
            rows = db.execute(
                "SELECT relative_path,logical_path FROM files WHERE dataset_id=? AND present=1 ORDER BY length(relative_path),relative_path",
                (dataset_id,),
            ).fetchall()
        logical_root = dataset["logical_root"]
        candidates = []
        for row in rows:
            prefix = logical_root + "/"
            if not row["logical_path"].startswith(prefix):
                continue
            suffix = row["logical_path"][len(prefix) :]
            physical = self.root / row["relative_path"]
            for _ in PurePosixPath(suffix).parts:
                physical = physical.parent
            if physical not in candidates:
                candidates.append(physical)
        for candidate in candidates:
            try:
                relative = candidate.relative_to(self.root).as_posix()
                cursor = self.root
                for part in PurePosixPath(relative).parts:
                    cursor /= part
                    info = cursor.lstat()
                    if (
                        cursor.is_symlink()
                        or getattr(info, "st_file_attributes", 0) & 0x400
                    ):
                        raise ValueError("linked dataset root")
                for child in (candidate / "market", candidate / "fundamentals"):
                    info = child.lstat()
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or child.is_symlink()
                        or getattr(info, "st_file_attributes", 0) & 0x400
                    ):
                        raise ValueError("invalid dataset layout")
                return candidate
            except (OSError, ValueError):
                continue
        raise ValueError("dataset has no safe materialized market/fundamentals root")

    def read_text(self, file_id, start_line=1, line_count=100):
        if (
            type(start_line) is not int
            or start_line < 1
            or type(line_count) is not int
            or not 1 <= line_count <= 120
        ):
            raise ValueError("invalid text page")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM files WHERE file_id=? AND present=1", (file_id,)
            ).fetchone()
        if row is None:
            raise ValueError("unknown file_id")
        path = _checked_file(self.root, row["relative_path"])
        if path.suffix.lower() not in {".txt", ".md"}:
            raise ValueError("only indexed TXT/MD content can be read")
        if _sha256(path) != row["sha256"]:
            raise ValueError("document changed; rebuild the catalog")
        lines, length, end_line, truncated = [], 0, start_line - 1, False
        with path.open("r", encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                if number < start_line:
                    continue
                if number >= start_line + line_count or length + len(line) > 16000:
                    if not lines and number < start_line + line_count:
                        lines.append(line[:16000])
                        end_line = number
                    truncated = True
                    break
                lines.append(line)
                length += len(line)
                end_line = number
        return {
            "source_file_id": file_id,
            "path": row["relative_path"],
            "sha256": row["sha256"],
            "start_line": start_line,
            "end_line": end_line,
            "text": "".join(lines),
            "truncated": truncated,
            "next_start_line": end_line + 1 if truncated else None,
        }

    def search_document_text(self, document_id, query, limit=8):
        """Search indexed text for one archived document and keep page evidence."""
        if (
            not isinstance(document_id, str)
            or not re.fullmatch(r"cninfo_[0-9a-f]{64}", document_id)
        ):
            raise ValueError("invalid document_id")
        if not isinstance(query, str) or not query.strip() or len(query) > 120:
            raise ValueError("query must contain 1..120 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be 1..20")
        page = self.document_jobs(job_ids=[document_id], limit=1)
        if not page["items"]:
            raise ValueError("unknown report document")
        artifact = next(
            (
                item
                for item in page["items"][0].get("artifact_paths", [])
                if item.get("kind") == "text"
            ),
            None,
        )
        if artifact is None:
            raise ValueError("report text is not available")
        with self._connect() as db:
            file = db.execute(
                "SELECT * FROM files WHERE file_id=? AND present=1",
                (artifact["file_id"],),
            ).fetchone()
        if file is None or file["bytes"] > 8 * 1024 * 1024:
            raise ValueError("indexed report text is unavailable or too large")
        path = _checked_file(self.root, file["relative_path"])
        if _sha256(path) != file["sha256"]:
            raise ValueError("report text changed; rebuild the catalog")
        terms = [query.strip().casefold()]
        if len(terms[0]) < 2:
            raise ValueError("query must be at least two characters")
        matches, total, current_page = [], 0, None
        with path.open("r", encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                page_match = re.search(
                    r"(?:PAGE|页)\s*[:#：]?\s*(\d+)", line, re.IGNORECASE
                )
                if page_match:
                    current_page = int(page_match.group(1))
                normalized = line.casefold()
                if not any(term in normalized for term in terms):
                    continue
                total += 1
                if len(matches) < limit:
                    matches.append(
                        {
                            "path": file["relative_path"],
                            "start_line": number,
                            "end_line": number,
                            "sha256": file["sha256"],
                            "page": current_page,
                            "text": line.rstrip()[:500],
                        }
                    )
        return {
            "document_id": document_id,
            "file_id": file["file_id"],
            "query": query.strip(),
            "results": matches,
            "total_matches": total,
            "truncated": total > len(matches),
        }
