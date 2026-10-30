#!/usr/bin/env python3
"""Export the complete existing catalog as metadata, without scanning source data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

EXPORT_NAMES = (
    "datasets.json",
    "files.jsonl",
    "documents.jsonl",
    "document_sources.jsonl",
    "document_artifacts.jsonl",
    "README.md",
)
DATASETS_SQL = """
    SELECT d.dataset_id,d.logical_root,d.title AS physical_root,
        COUNT(f.file_id) AS file_count,COUNT(DISTINCT f.sha256) AS unique_content_count,
        SUM(f.bytes) AS source_bytes,MIN(NULLIF(f.date_start,'')) AS date_start,
        MAX(NULLIF(f.date_end,'')) AS date_end,
        (SELECT COUNT(DISTINCT c.code) FROM file_codes c JOIN files fc
         ON c.file_id=fc.file_id WHERE fc.dataset_id=d.dataset_id AND fc.present=1)
        AS unique_security_count
    FROM datasets d JOIN files f ON f.dataset_id=d.dataset_id AND f.present=1
    GROUP BY d.dataset_id ORDER BY d.logical_root,d.title,d.dataset_id
"""
FILES_SQL = """
    SELECT file_id,dataset_id,relative_path,logical_path,sha256,bytes,mtime_ns,kind,
        date_start,date_end,row_count,parse_status,
        CASE WHEN parse_error IS NULL OR parse_error='' THEN 0 ELSE 1 END
        AS parse_error_present
    FROM files WHERE present=1 ORDER BY relative_path
"""
SOURCES_SQL = """
    SELECT s.document_id,s.file_id,s.dataset_id,s.code FROM document_sources s
    JOIN files f ON s.file_id=f.file_id WHERE f.present=1
    ORDER BY s.document_id,s.file_id,s.code
"""
ARTIFACTS_SQL = """
    SELECT a.document_id,a.file_id,a.kind,f.relative_path,f.sha256,f.bytes
    FROM document_artifacts a JOIN files f ON a.file_id=f.file_id
    WHERE f.present=1 ORDER BY a.document_id,a.file_id
"""


def _safe_url(value: str | None) -> str:
    """Do not publish userinfo, query credentials, or URL fragments."""
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return ""


def _documents_sql(db) -> str:
    has_worker = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_downloads'"
    ).fetchone()
    worker_fields = (
        "w.status AS download_status,w.attempts AS download_attempts,"
        "CASE WHEN w.error IS NULL OR w.error='' THEN 0 ELSE 1 END "
        "AS download_error_present"
        if has_worker
        else "NULL AS download_status,NULL AS download_attempts,"
        "0 AS download_error_present"
    )
    join = (
        "LEFT JOIN document_downloads w ON d.document_id=w.job_id "
        if has_worker
        else ""
    )
    return (
        "SELECT d.document_id,d.source_url,d.publication_date,d.status,"
        "d.attempts,d.updated,"
        "CASE WHEN d.error IS NULL OR d.error='' THEN 0 ELSE 1 END AS error_present,"
        + worker_fields
        + " FROM documents d "
        + join
        + "WHERE EXISTS (SELECT 1 FROM document_sources s JOIN files f "
        "ON s.file_id=f.file_id WHERE s.document_id=d.document_id AND f.present=1) "
        "ORDER BY d.document_id"
    )


def _write_rows(db, query: str, destination: Path, metadata: dict) -> int:
    count = 0
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for row in db.execute(query):
            item = dict(row)
            if "source_url" in item:
                original = item["source_url"] or ""
                item["source_url"] = _safe_url(original)
                item["source_url_sanitized"] = item["source_url"] != original
                item["original_source_url_sha256"] = (
                    hashlib.sha256(original.encode("utf-8")).hexdigest()
                    if original
                    else ""
                )
            handle.write(json.dumps({**metadata, **item}, ensure_ascii=False) + "\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def _markdown(value) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _write_exports(db, staging: Path) -> dict:
    latest = db.execute("SELECT * FROM builds ORDER BY started DESC LIMIT 1").fetchone()
    completed = db.execute(
        "SELECT * FROM builds WHERE status IN ('completed','completed_with_gaps') "
        "ORDER BY finished DESC LIMIT 1"
    ).fetchone()
    if completed is None:
        raise ValueError("catalog has no completed snapshot; build it before exporting")
    metadata = {
        "export_id": uuid.uuid4().hex,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "build_id": completed["id"],
    }
    index = {
        "status": latest["status"],
        "latest_build_id": latest["id"],
        "snapshot_build_id": completed["id"],
        "indexed_at": completed["finished"],
        "readiness_status": "not_validated",
        "analysis_ready": None,
    }
    counts = {}
    for name, query in (
        ("files.jsonl", FILES_SQL),
        ("documents.jsonl", _documents_sql(db)),
        ("document_sources.jsonl", SOURCES_SQL),
        ("document_artifacts.jsonl", ARTIFACTS_SQL),
    ):
        counts[name] = _write_rows(db, query, staging / name, metadata)
    total = db.execute(
        "SELECT COUNT(DISTINCT dataset_id) FROM files WHERE present=1"
    ).fetchone()[0]
    # Stream every dataset; bot pagination limits do not apply to this export.
    with (staging / "datasets.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        header = {**metadata, "index": index, "total": total, "exported_rows": counts}
        handle.write(json.dumps(header, ensure_ascii=False)[:-1] + ', "datasets": [\n')
        count = 0
        for row in db.execute(DATASETS_SQL):
            if count:
                handle.write(",\n")
            handle.write(json.dumps(dict(row), ensure_ascii=False))
            count += 1
        if count != total:
            raise ValueError("dataset references are incomplete")
        handle.write("\n]}\n")
        handle.flush()
        os.fsync(handle.fileno())
    with (staging / "README.md").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "# 统一数据集目录\n\n"
            f"生成时间：{metadata['exported_at']}  \n"
            f"导出编号：`{metadata['export_id']}`  \n"
            f"快照 build_id：`{metadata['build_id']}`  \n"
            f"最新构建状态：`{index['status']}`\n\n"
            f"数据集 {total} 个，业务文件 {counts['files.jsonl']:,} 个，"
            f"登记原文 {counts['documents.jsonl']:,} 条，"
            f"来源关联 {counts['document_sources.jsonl']:,} 条，"
            f"资料文件关联 {counts['document_artifacts.jsonl']:,} 条。\n\n"
            "文件存在不代表通过回测就绪校验；运行中的构建尚未刷新此快照。\n\n"
            "- [全部数据集与导出计数](datasets.json)\n"
            "- [全部业务文件](files.jsonl)\n"
            "- [全部已登记原文及补取状态](documents.jsonl)\n"
            "- [原文与数据来源关联](document_sources.jsonl)\n"
            "- [原文 PDF 及文字文件路径与哈希](document_artifacts.jsonl)\n"
            "- SQLite：`catalog.sqlite3`；扫描与排除清单：`manifest_*.jsonl`。\n\n"
            "这是一次只读事务的完整元数据快照，不包含文档正文、下载结果详情"
            "或自由文本错误。URL 已移除用户凭证、查询串和片段；原始 URL "
            "以 SQLite 为准。同名数据集按实际来源快照分开。\n\n"
            "维护：先运行 `python scripts/build_dataset_catalog.py`，再运行 "
            "`python scripts/export_dataset_catalog.py`。补取改变资料状态后也可"
            "单独重新导出。各文件包含同一导出编号；README 最后原子发布。\n\n"
            "| 数据集 | 来源目录/快照 | 文件数 | 证券代码数 | 日期范围 |\n"
            "|---|---|---:|---:|---|\n"
        )
        for row in db.execute(DATASETS_SQL):
            handle.write(
                f"| {_markdown(row['logical_root'])} | "
                f"{_markdown(row['physical_root'])} | {row['file_count']} | "
                f"{row['unique_security_count']} | {_markdown(row['date_start'])} ～ "
                f"{_markdown(row['date_end'])} |\n"
            )
        handle.flush()
        os.fsync(handle.fileno())
    return {**metadata, "index": index, "datasets": total, "exported_rows": counts}


def _publish(staging: Path, target: Path) -> None:
    backup = staging / "previous"
    backup.mkdir()
    for name in EXPORT_NAMES:
        destination = target / name
        if destination.exists() or destination.is_symlink():
            info = destination.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or destination.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_nlink != 1
            ):
                raise ValueError(f"export destination must be a regular file: {name}")
            shutil.copyfile(destination, backup / name)
    published = []
    try:
        for name in EXPORT_NAMES:
            os.replace(staging / name, target / name)
            published.append(name)
    except OSError:
        for name in reversed(published):
            if (backup / name).exists():
                os.replace(backup / name, target / name)
            else:
                (target / name).unlink()
        raise


def export_catalog(project_root: str | Path) -> dict:
    root = Path(project_root).resolve()
    target = root / "data" / "dataset_catalog"
    database = target / "catalog.sqlite3"
    for path in (root / "data", target, database):
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("catalog export paths must not use links")
    lock = target / ".export.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix=".export-", dir=target) as directory:
            staging = Path(directory)
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                result = _write_exports(db, staging)
            _publish(staging, target)
        result["directory"] = str(target)
        result["files"] = []
        for name in EXPORT_NAMES:
            digest = hashlib.sha256()
            with (target / name).open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result["files"].append(
                {
                    "path": name,
                    "bytes": (target / name).stat().st_size,
                    "sha256": digest.hexdigest(),
                }
            )
        return result
    finally:
        lock.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    print(json.dumps(export_catalog(args.project_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
