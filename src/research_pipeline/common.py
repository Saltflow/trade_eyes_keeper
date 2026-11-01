"""Small deterministic storage and identity primitives, without network access."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_sha(path: Path) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def load(path: Path) -> Any:
    return json.loads(Path(path).read_text("utf-8"))


def atomic_write(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        value
        if isinstance(value, bytes)
        else value.encode("utf-8")
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    )
    if exclusive:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return
    from src.core.runtime_files import atomic_write_json

    if not isinstance(value, (bytes, str)):
        atomic_write_json(path, value)
        return
    import tempfile

    handle, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def artifact_key(stage: str, issuer: str | list[str], versions: Any) -> str:
    subjects = sorted(set(issuer)) if isinstance(issuer, list) else issuer
    return fingerprint({"stage": stage, "subject": subjects, "versions": versions})


def annual_versions(documents: list) -> dict[str, list[str]]:
    """Source content and fiscal window, never paths, prompt versions or prices."""
    years: dict[str, list[str]] = {}
    for document in documents:
        value = document.to_dict() if hasattr(document, "to_dict") else document
        digest = value.get("sha256") or (
            file_sha(Path(value["local_path"]))
            if value.get("local_path") and Path(value["local_path"]).is_file()
            else value["document_id"]
        )
        years.setdefault(str(value["fiscal_year"]), []).append(str(digest))
    return {year: sorted(set(values)) for year, values in sorted(years.items())}
