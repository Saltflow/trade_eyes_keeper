"""Bounded public report observations, independent of trading/account state."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from zoneinfo import ZoneInfo

from .catalog import data_mapping, iter_safe_leaves, safe_path, safe_segment

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_KINDS = {"daily", "brief"}
_MISSING = object()


def _safe_data(value, depth=0):
    if depth > 10:
        return _MISSING
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _MISSING
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _safe_data(value.value, depth + 1)
    mapping = data_mapping(value)
    if mapping or isinstance(value, dict):
        result = {}
        for key, child in list(mapping.items())[:4096]:
            key = str(key).replace(".", "-")
            if not safe_segment(key):
                continue
            cleaned = _safe_data(child, depth + 1)
            if cleaned is not _MISSING:
                result[key] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 4096:
            return _MISSING
        result = [_safe_data(item, depth + 1) for item in value]
        return result if all(item is not _MISSING for item in result) else _MISSING
    return _MISSING


def _aware(value, zone):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


def _directory(config):
    return (
        Path(config.get("storage", {}).get("data_dir", "data"))
        / "runtime"
        / "trigger_snapshots"
    )


def capture_report_snapshot(config: dict, session, kind: str) -> Path:
    """Atomically record a completed public report; callers isolate optional errors."""
    if kind not in _KINDS:
        raise ValueError("report snapshot kind must be daily or brief")
    zone = ZoneInfo(config.get("scheduler", {}).get("timezone", "Asia/Shanghai"))
    now = datetime.now(zone)
    mapping = data_mapping(session)
    from pydantic import BaseModel

    explicitly_set = (
        session.__fields_set__ if isinstance(session, BaseModel) else set(mapping)
    )
    public = {}
    for key, value in mapping.items():
        if not safe_segment(key) or (
            key not in explicitly_set
            and (value is None or isinstance(value, (dict, list, tuple)) and not value)
        ):
            continue
        cleaned = _safe_data(value)
        if cleaned is not _MISSING:
            public[key] = cleaned
    created = _aware(mapping.get("created_at"), zone)
    metadata = {}
    for key, value in public.items():
        if isinstance(value, (dict, list)):
            metadata[key] = {
                "as_of": created.isoformat() if created else None,
                "observed_at": now.isoformat(),
                "source": f"{kind}_report_snapshot",
            }
    for path, value in iter_safe_leaves(public):
        parts = path.split(".")
        source_time = created
        if parts[0] == "stocks_data" and len(parts) > 2:
            row = public.get("stocks_data", {}).get(parts[1], {})
            source_time = _aware(row.get("quote_time", row.get("date")), zone)
            # A current quote clock does not date a financial statement or dividend.
            if parts[-1] in {
                "roe",
                "pe_ratio",
                "pb_ratio",
                "earnings_growth",
                "dividend_per_share",
                "dividend_yield",
            }:
                source_time = _aware(row.get(parts[-1] + "_as_of"), zone)
        metadata[path] = {
            "as_of": source_time.isoformat() if source_time else None,
            "observed_at": now.isoformat(),
            "source": f"{kind}_report_snapshot",
        }
    payload = {
        "version": 1,
        "kind": kind,
        "captured_at": now.isoformat(),
        "created_at": created.isoformat() if created else None,
        "data": public,
        "fact_metadata": metadata,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("public report snapshot exceeds size limit")
    directory = _directory(config)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"last_{kind}.json"
    temporary = directory / f".{kind}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _merge(target, incoming, prefix, metadata, incoming_metadata):
    for key, value in incoming.items():
        if not safe_segment(key) or value is None:
            continue
        path = f"{prefix}.{key}".strip(".")
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value, path, metadata, incoming_metadata)
            continue
        target[key] = value
        for previous in list(metadata):
            if previous == path or previous.startswith(path + "."):
                del metadata[previous]
        for leaf_path, leaf in iter_safe_leaves(value, path):
            if leaf is not None and leaf_path in incoming_metadata:
                metadata[leaf_path] = incoming_metadata[leaf_path]
        if path in incoming_metadata:
            metadata[path] = incoming_metadata[path]


def load_report_snapshot(config: dict) -> dict:
    """Merge last completed daily/brief, preserving each field's original clock.

    New brief fields supplement the daily record. None and absent fields cannot
    erase earlier values. Real-time trigger prices always use the live producer.
    Corrupt, oversized or unsupported snapshots are ignored independently.
    """
    records = []
    for kind in sorted(_KINDS):
        path = _directory(config) / f"last_{kind}.json"
        try:
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("kind") != kind
                or not isinstance(payload.get("data"), dict)
                or not isinstance(payload.get("fact_metadata"), dict)
            ):
                continue
            captured = _aware(payload.get("captured_at"), ZoneInfo("UTC"))
            if captured is None:
                continue
            cleaned = _safe_data(payload["data"])
            if cleaned is _MISSING:
                continue
            payload["data"] = cleaned
            records.append((captured, payload))
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    merged, metadata = {}, {}
    for _, payload in sorted(records, key=lambda item: item[0]):
        safe_metadata = {
            path: info
            for path, info in payload["fact_metadata"].items()
            if safe_path(path) and isinstance(info, dict)
        }
        _merge(merged, payload["data"], "", metadata, safe_metadata)
        merged["created_at"] = payload.get("created_at")
    if records:
        merged["_fact_metadata"] = metadata
    return merged
