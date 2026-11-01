"""Recover an indexed artifact from exact offline evidence, without regeneration.

The old SHA is the authority. A readable response, valid JSON, or equivalent prose
is insufficient: reconstructed bytes must have exactly the original digest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from .common import atomic_write, file_sha, fingerprint, now
from .parsing import hash_lock

_ACCEPTED = {"complete", "completed", "succeeded", "partial"}
_SUCCESS = {"complete", "completed", "succeeded"}
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _paths(value):
    if isinstance(value, (str, Path)) and str(value):
        yield Path(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _paths(item)


def _copy_candidates(records):
    for record in records:
        if not isinstance(record, dict):
            continue
        yield from _paths(record.get("path"))
        metadata = record.get("metadata")
        if isinstance(metadata, dict):
            yield from _paths(metadata.get("copies", []))
        files = record.get("files")
        if isinstance(files, dict):
            yield from _paths(list(files))


def _matching_file(paths, expected):
    seen = set()
    for path in paths:
        try:
            identity = str(path.resolve())
            if identity in seen:
                continue
            seen.add(identity)
            if path.is_file() and file_sha(path) == expected:
                raw = path.read_bytes()
                # Recheck after reading, including a concurrently changed copy.
                if _digest(raw) == expected:
                    return raw, {"method": "exact_copy", "source": identity}
        except OSError:
            continue
    return None


def _content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item.get("text", "")
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _raw_text(raw: bytes):
    """Read OpenAI JSON or complete SSE content; omit reasoning channels."""
    try:
        text = raw.decode("utf-8-sig")
        value = json.loads(text)
    except (UnicodeError, ValueError):
        value = None
    if isinstance(value, dict):
        for choice in value.get("choices", []):
            if isinstance(choice, dict):
                yield _content(choice.get("message", {}).get("content"))
        return
    try:
        text = raw.decode("utf-8-sig")
        chunks, done = [], False
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                continue
            item = json.loads(data)
            for choice in item.get("choices", []):
                chunks.append(_content(choice.get("delta", {}).get("content")))
        if done:
            yield "".join(chunks)
    except (UnicodeError, ValueError, TypeError, AttributeError):
        return


def _serializations(text, stage):
    # TextIO-written historical files may use CRLF. Every candidate still has
    # to match the authoritative SHA, so this cannot normalize old output.
    yield text.encode("utf-8")
    yield text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    if stage != "extract":
        return
    try:
        from src.fundamental_data.v2_schema import parse

        value = parse(text)
        encoded = json.dumps(value, ensure_ascii=False, indent=2)
        yield encoded.encode("utf-8")
        yield (encoded + "\n").encode("utf-8")
        yield encoded.replace("\n", "\r\n").encode("utf-8")
    except (ValueError, TypeError, AttributeError):
        return


def _captures(store, record):
    metadata = record.get("metadata", {})
    direct = metadata.get("receipt")
    if isinstance(direct, dict):
        yield direct
    attempt = store.get_attempt(record["key"])
    if attempt:
        yield attempt
    # Research final artifacts have a different key from their write attempt;
    # historical imports can also have a producer key unrelated to the cache key.
    yield from store.list_attempts(limit=10000)


def _from_receipts(store, record, expected):
    seen = set()
    for attempt in _captures(store, record):
        if not isinstance(attempt, dict):
            continue
        receipt = attempt.get("receipt") or attempt
        if not isinstance(receipt, dict):
            continue
        if receipt.get("status", attempt.get("status")) not in _SUCCESS:
            continue
        folder = attempt.get("capture_dir") or receipt.get("capture_dir")
        if not folder or str(folder) in seen:
            continue
        seen.add(str(folder))
        folder = Path(folder)
        result = _matching_file(
            (folder / name for name in ("content.md", "content.txt")), expected
        )
        if result:
            return result
        for name in ("content.md", "content.txt", "response.raw", "response.body"):
            path = folder / name
            try:
                if not path.is_file() or path.stat().st_size > _MAX_RESPONSE_BYTES:
                    continue
                raw = path.read_bytes()
                texts = (
                    [raw.decode("utf-8")]
                    if name.startswith("content.")
                    else _raw_text(raw)
                )
                for text in texts:
                    for candidate in _serializations(text, record["stage"]):
                        if _digest(candidate) == expected:
                            return candidate, {
                                "method": "exact_response_reconstruction",
                                "source": str(path.resolve()),
                                "source_sha256": _digest(raw),
                            }
            except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                continue
    return None


def recover_artifact(store, key: str) -> dict | None:
    """Reindex a new exact copy, or return None without any paid/local parsing.

    Directory artifacts and unverifiable context auxiliaries deliberately defer
    to the caller's visible partial-data path. No historical file is changed.
    """
    lock = store.root / "recovered" / (fingerprint(key) + ".lock")
    with hash_lock(lock):
        current = store.get_artifact(key)
        if current:
            return current
        records = store.list_artifacts()
        record = next((row for row in records if row.get("key") == key), None)
        if not record or record.get("status") not in _ACCEPTED:
            return None
        expected = record.get("sha256", "")
        files = record.get("files", {})
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
            or not isinstance(files, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("metadata", {}), dict)
        ):
            return None
        original = Path(record["path"])
        # A directory digest includes absolute path names. Reindexing it at a
        # new location is a different artifact representation, not exact repair.
        if len(files) != 1 or files.get(str(original)) != expected:
            return None
        metadata = copy.deepcopy(record.get("metadata", {}))
        auxiliary = None
        if record["stage"] == "context" and metadata.get("metadata_path"):
            path = Path(metadata["metadata_path"])
            aux_sha = metadata.get("metadata_sha256") or metadata.get(
                "metadata_path_sha256"
            )
            if not aux_sha:
                # Older context registries did not hash this required sibling.
                # Its absence/corruption cannot be repaired by inventing it.
                try:
                    raw = path.read_bytes()
                    if not isinstance(json.loads(raw), (dict, list)):
                        return None
                    auxiliary = raw, file_sha(path)
                except (OSError, ValueError):
                    return None
            else:
                found = _matching_file([path, *_copy_candidates(records)], aux_sha)
                if not found:
                    return None
                try:
                    if not isinstance(json.loads(found[0]), (dict, list)):
                        return None
                except ValueError:
                    return None
                auxiliary = found[0], aux_sha
        found = _matching_file(_copy_candidates([record, *records]), expected)
        if not found:
            found = _from_receipts(store, record, expected)
        if not found:
            return None
        raw, evidence = found
        folder = store.root / "recovered" / fingerprint(key) / uuid.uuid4().hex
        destination = folder / original.name
        atomic_write(destination, raw, exclusive=True)
        if auxiliary:
            aux_path = folder / "元数据.json"
            atomic_write(aux_path, auxiliary[0], exclusive=True)
            metadata.update(metadata_path=str(aux_path), metadata_sha256=auxiliary[1])
        metadata["recovery"] = {
            **evidence,
            "at": now(),
            "original_path": str(original),
            "original_sha256": expected,
            "exact_sha256_verified": True,
            "api_calls": 0,
        }
        restored = store.put_artifact(
            key,
            record["stage"],
            destination,
            status=record["status"],
            metadata=metadata,
        )
        return restored if restored.get("sha256") == expected else None
