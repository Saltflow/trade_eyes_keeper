#!/usr/bin/env python3
"""Prepare and resumably push immutable, verified business-data snapshots.

prepare is local only. preflight is read-only on the configured deployment host.
push writes exclusively to data/dataset_imports/local_<manifest SHA-256> and
never changes the active dataset, configuration, services, or existing stores.
Transmission deduplicates content; the final dataset uses ordinary files so
existing readers need neither hardlinks nor a virtual filesystem.
"""

from __future__ import annotations

import argparse
import base64
import collections
import gzip
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

CONTRACT = "local-business-dataset-snapshot-1"
RESERVE_BYTES = 512 * 1024**2
BLOCK_BYTES = 1024**2
PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r"[0-9a-f]{64}\Z")
SECRET = re.compile(
    rb"(?i)(?:[\"'](?:api[_-]?key|app[_-]?secret|password|access[_-]?token|"
    rb"refresh[_-]?token|tenant[_-]?access[_-]?token|token|client[_-]?secret)"
    rb"[\"']\s*:\s*[\"'][^\"'\s][^\"']*[\"']|"
    rb"\bsk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class SnapshotError(RuntimeError):
    """A snapshot or transfer failed a consistency or scope guard."""


class SnapshotPaused(SnapshotError):
    """A source may be actively written; no unstable source was omitted."""


def canonical(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK_BYTES), b""):
            result.update(block)
    return result.hexdigest()


def valid_relative(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise SnapshotError("empty or invalid relative path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or "\\" in name
        or ":" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise SnapshotError("noncanonical relative path")


def safe_path(base: Path, relative: str = "") -> Path:
    base = base.absolute()
    if relative:
        valid_relative(relative)
    path = base / relative
    if not path.absolute().is_relative_to(base.absolute()):
        raise SnapshotError("path is outside snapshot root")
    for part in (path, *path.parents):
        if part.is_symlink() or (
            part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise SnapshotError("symlink or junction is not permitted")
    return path


def regular_file(path: Path) -> os.stat_result:
    safe_path(path.parent, path.name)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SnapshotError("only independent regular files are permitted")
    return info


def atomic_bytes(path: Path, data: bytes) -> None:
    safe_path(path.parent, path.name)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def signature(info: os.stat_result) -> tuple:
    return (
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_dev,
        info.st_ino,
    )


def source_entries(root: Path) -> list[dict]:
    """Share the catalog's exact source scope; no alternative uploader policy."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.data.dataset_catalog import iter_catalog_entries

    entries = []
    for item in iter_catalog_entries(root):
        entries.append(item)
        if len(entries) % 5000 == 0:
            print(
                json.dumps({"phase": "scanning_sources", "entries": len(entries)}),
                flush=True,
            )
    failures = [
        {"path": item["relative_path"], "reason": item["reason"]}
        for item in entries
        if not item["eligible"]
        and (
            "error:" in item["reason"]
            or item["reason"] == "suspected_sensitive_content"
        )
    ]
    if failures:
        raise SnapshotPaused(
            "source inventory requires review: " + json.dumps(failures)
        )
    return entries


def manifest_policy(relative: str, logical: str) -> str:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.data.dataset_catalog import eligibility_reason

    return eligibility_reason(relative, logical)


def policy_program() -> str:
    """Embed only the catalog's pure path policy, retaining one source of truth."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.data import dataset_catalog

    visited, pieces = set(), []

    def collect(function):
        if function.__name__ in visited:
            return
        visited.add(function.__name__)
        for name in function.__code__.co_names:
            value = vars(dataset_catalog).get(name)
            if (
                inspect.isfunction(value)
                and value.__module__ == dataset_catalog.__name__
            ):
                collect(value)
            elif name not in visited and isinstance(
                value, (str, int, float, tuple, list, set, frozenset, dict)
            ):
                visited.add(name)
                pieces.append(f"{name}={value!r}")
        pieces.append(inspect.getsource(function))

    collect(dataset_catalog.eligibility_reason)
    pieces.append("manifest_policy=eligibility_reason")
    return "\n\n".join(pieces) + "\n"


def verify_sources(root: Path, names: list[str], signatures: dict) -> None:
    print(json.dumps({"phase": "checking_source_stability"}), flush=True)
    ensure_quiet(root)
    current = source_entries(root)
    if sorted(item["relative_path"] for item in current if item["eligible"]) != names:
        raise SnapshotPaused("eligible file membership changed during snapshot")
    for name, original in signatures.items():
        if signature(regular_file(safe_path(root, name))) != original:
            raise SnapshotPaused("source changed during snapshot: " + name)


def active_writers(root: Path) -> list[dict]:
    """Detect project download/report/analysis workers without exporting argv."""
    try:
        import psutil
    except ImportError as exc:
        raise SnapshotPaused("psutil is required for the local writer check") from exc
    writers = []
    prefixes = ("backfill_", "analyze_", "backtest_", "screen_", "benchmark_", "fetch_")
    for process in psutil.process_iter(["pid", "cmdline", "cwd"]):
        if process.pid == os.getpid():
            continue
        try:
            argv = process.info.get("cmdline") or []
            cwd = Path(process.info.get("cwd") or ".").absolute()
            belongs = cwd == root or any(
                str(root).lower() in arg.lower() for arg in argv
            )
            if not belongs:
                continue
            scripts = [Path(arg).name for arg in argv if arg.endswith(".py")]
            for script in scripts:
                if (
                    script.startswith(prefixes)
                    or script == "run_reference_universe_isolated.py"
                    or (
                        script == "main.py"
                        and any(
                            flag in argv
                            for flag in ("--once", "--brief", "--optimize", "--service")
                        )
                    )
                ):
                    writers.append({"pid": process.pid, "script": script})
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return writers


def ensure_quiet(root: Path) -> None:
    workers = active_writers(root)
    if workers:
        raise SnapshotPaused("active project writers: " + json.dumps(workers))


def freeze_file(root: Path, entry: dict, objects: Path) -> tuple[dict, tuple]:
    relative = entry["relative_path"]
    path = safe_path(root, relative)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SnapshotPaused("source is no longer a regular unlinked file: " + relative)
    temporary = objects / (".freeze-" + uuid.uuid4().hex + ".tmp")
    digest = hashlib.sha256()
    tail = b""
    try:
        with path.open("rb") as source, temporary.open("xb") as destination:
            opened = os.fstat(source.fileno())
            # Windows' path stat and descriptor stat can expose different
            # creation/change times. Compare stable identity across the two,
            # then check each kind's complete signature against itself.
            before_identity = signature(before)[:2] + signature(before)[3:]
            opened_identity = signature(opened)[:2] + signature(opened)[3:]
            if opened_identity != before_identity:
                raise SnapshotPaused("source changed before reading: " + relative)
            while block := source.read(BLOCK_BYTES):
                inspected = tail + block
                if SECRET.search(inspected):
                    raise SnapshotPaused(
                        "nonempty credential content found: " + relative
                    )
                tail = inspected[-4096:]
                digest.update(block)
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
            if signature(os.fstat(source.fileno())) != signature(opened):
                raise SnapshotPaused("source changed while reading: " + relative)
        if signature(path.lstat()) != signature(before):
            raise SnapshotPaused("source changed after reading: " + relative)
        hexdigest = digest.hexdigest()
        target = safe_path(objects, hexdigest)
        if target.exists():
            if target.stat().st_size != before.st_size or sha_file(target) != hexdigest:
                raise SnapshotError("existing frozen object is corrupt")
        else:
            os.replace(temporary, target)
            os.chmod(target, 0o400)
        return {
            "path": relative,
            "logical_path": entry.get("logical_path", relative),
            "bytes": before.st_size,
            "sha256": hexdigest,
            "source_mtime_ns": before.st_mtime_ns,
        }, signature(before)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_snapshot(
    root: Path, snapshot: Path, chunk_bytes: int = 4 * 1024**2
) -> dict:
    safe_path(root)
    root = root.resolve()
    snapshot = snapshot.absolute()
    if not snapshot.is_relative_to(root / "temp_test_cache"):
        raise SnapshotError("local snapshot must be under project temp_test_cache")
    safe_path(root, snapshot.relative_to(root).as_posix())
    if not 65536 <= chunk_bytes <= 32 * 1024**2:
        raise SnapshotError("chunk size must be 64 KiB to 32 MiB")
    if (snapshot / "manifest.json").exists():
        raise SnapshotError(
            "frozen snapshot already exists; push resumes it, use a new directory for new data"
        )
    ensure_quiet(root)
    entries = source_entries(root)
    eligible = sorted(
        (item for item in entries if item["eligible"]),
        key=lambda item: item["relative_path"],
    )
    if not eligible:
        raise SnapshotError("no eligible business files")
    names = [item["relative_path"] for item in eligible]
    if len(names) != len(set(names)):
        raise SnapshotError("duplicate source path")
    for name in names:
        valid_relative(name)
    total = sum(safe_path(root, name).stat().st_size for name in names)
    if shutil.disk_usage(root).free < total * 2 + RESERVE_BYTES:
        raise SnapshotPaused("insufficient local space for a frozen data snapshot")
    snapshot.mkdir(parents=True, exist_ok=True)
    objects = snapshot / "objects"
    objects.mkdir(exist_ok=True)
    files, signatures = [], {}
    for index, entry in enumerate(eligible, 1):
        row, original = freeze_file(root, entry, objects)
        files.append(row)
        signatures[row["path"]] = original
        if index % 1000 == 0:
            print(
                json.dumps(
                    {"phase": "freezing", "files": index, "total": len(eligible)}
                ),
                flush=True,
            )
    verify_sources(root, names, signatures)
    unique = {row["sha256"]: row["bytes"] for row in files}
    print(
        json.dumps({"phase": "compressing_objects", "objects": len(unique)}), flush=True
    )
    archive = snapshot / "snapshot.tar.gz"
    temporary = snapshot / (".archive-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output, gzip.GzipFile(
            filename="", mode="wb", fileobj=output, mtime=0, compresslevel=6
        ) as compressed, tarfile.open(
            fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT
        ) as bundle:
            for digest, size in sorted(unique.items()):
                info = tarfile.TarInfo("objects/" + digest)
                info.size, info.mode, info.mtime = size, 0o400, 0
                with (objects / digest).open("rb") as source:
                    bundle.addfile(info, source)
        if archive.exists():
            if sha_file(archive) != sha_file(temporary):
                raise SnapshotError(
                    "different archive already exists in local snapshot"
                )
        else:
            os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    chunks = []
    with archive.open("rb") as source:
        offset = 0
        while data := source.read(chunk_bytes):
            chunks.append(
                {
                    "index": len(chunks),
                    "offset": offset,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            offset += len(data)
    exclusions = collections.Counter(
        item.get("reason", "excluded") for item in entries if not item["eligible"]
    )
    manifest = {
        "contract": CONTRACT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "excluded_counts": dict(exclusions),
        "objects": [
            {"sha256": digest, "bytes": size} for digest, size in sorted(unique.items())
        ],
        "logical_bytes": sum(row["bytes"] for row in files),
        "unique_bytes": sum(unique.values()),
        "archive": {
            "bytes": archive.stat().st_size,
            "sha256": sha_file(archive),
            "chunks": chunks,
        },
        "source_writer_check": "no known project writers; membership/stat and per-file consistency verified",
    }
    validate_manifest(manifest)
    # Archive compression can be lengthy. A writer starting during that stage
    # must pause preparation, too; frozen data is never silently declared current.
    verify_sources(root, names, signatures)
    atomic_bytes(snapshot / "manifest.json", canonical(manifest))
    return snapshot_summary(manifest)


def validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise SnapshotError("manifest must be an object")
    if manifest.get("contract") != CONTRACT:
        raise SnapshotError("unknown snapshot contract")
    files, objects = manifest.get("files"), manifest.get("objects")
    if (
        not isinstance(files, list)
        or not files
        or len(files) > 200000
        or not isinstance(objects, list)
    ):
        raise SnapshotError("invalid snapshot file inventory")
    object_sizes = {}
    for row in objects:
        if (
            not isinstance(row, dict)
            or not HEX.fullmatch(str(row.get("sha256", "")))
            or type(row.get("bytes")) is not int
            or row["bytes"] < 0
            or row["sha256"] in object_sizes
        ):
            raise SnapshotError("invalid or duplicate content object")
        object_sizes[row["sha256"]] = row["bytes"]
    names = set()
    used = set()
    for row in files:
        if not isinstance(row, dict):
            raise SnapshotError("file entry must be an object")
        valid_relative(row.get("path"))
        valid_relative(row.get("logical_path"))
        if manifest_policy(row["path"], row["logical_path"]):
            raise SnapshotError("manifest contains a disallowed business-data path")
        if (
            row["path"].casefold() in names
            or type(row.get("bytes")) is not int
            or object_sizes.get(row.get("sha256")) != row["bytes"]
        ):
            raise SnapshotError("duplicate path or invalid file/object mapping")
        names.add(row["path"].casefold())
        used.add(row["sha256"])
        if type(row.get("source_mtime_ns")) is not int or row["source_mtime_ns"] < 0:
            raise SnapshotError("invalid source timestamp")
    if (
        used != set(object_sizes)
        or manifest.get("logical_bytes") != sum(row["bytes"] for row in files)
        or manifest.get("unique_bytes") != sum(object_sizes.values())
    ):
        raise SnapshotError("snapshot totals do not match file inventory")
    archive = manifest.get("archive", {})
    if (
        not HEX.fullmatch(str(archive.get("sha256", "")))
        or type(archive.get("bytes")) is not int
        or archive["bytes"] <= 0
    ):
        raise SnapshotError("invalid archive metadata")
    offset = 0
    for index, row in enumerate(archive.get("chunks", [])):
        if (
            row.get("index") != index
            or row.get("offset") != offset
            or type(row.get("bytes")) is not int
            or not 0 < row["bytes"] <= 32 * 1024**2
            or not HEX.fullmatch(str(row.get("sha256", "")))
        ):
            raise SnapshotError("invalid contiguous archive chunks")
        offset += row["bytes"]
    if offset != archive["bytes"]:
        raise SnapshotError("archive chunks do not cover declared length")


def snapshot_summary(manifest: dict) -> dict:
    return {
        "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest(),
        "files": len(manifest["files"]),
        "unique_objects": len(manifest["objects"]),
        "logical_bytes": manifest["logical_bytes"],
        "unique_bytes": manifest["unique_bytes"],
        "archive_bytes": manifest["archive"]["bytes"],
        "chunks": len(manifest["archive"]["chunks"]),
        "remote_peak_bytes_with_reserve": manifest["logical_bytes"]
        + manifest["unique_bytes"]
        + 2 * manifest["archive"]["bytes"]
        + RESERVE_BYTES,
        "excluded_counts": manifest["excluded_counts"],
    }


def import_root(project: Path, digest: str) -> Path:
    if (
        not HEX.fullmatch(digest)
        or not project.is_absolute()
        or project == Path(project.anchor)
    ):
        raise SnapshotError("invalid project or snapshot identity")
    safe_path(project)
    return safe_path(project, "data/dataset_imports/local_" + digest)


def load_import(project: Path, digest: str) -> tuple[Path, dict]:
    destination = import_root(project, digest)
    path = safe_path(destination, "manifest.json")
    if regular_file(path).st_size > 32 * 1024**2:
        raise SnapshotError("snapshot manifest exceeds 32 MiB")
    manifest = json.loads(path.read_bytes())
    validate_manifest(manifest)
    if hashlib.sha256(canonical(manifest)).hexdigest() != digest:
        raise SnapshotError("remote manifest identity mismatch")
    return destination, manifest


@contextmanager
def remote_lock(destination: Path):
    path = safe_path(destination, ".transfer/operation.lock")
    if path.exists():
        regular_file(path)
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def initialize_import(project: Path, digest: str, manifest: dict) -> dict:
    validate_manifest(manifest)
    if hashlib.sha256(canonical(manifest)).hexdigest() != digest:
        raise SnapshotError("provided manifest does not match snapshot id")
    destination = import_root(project, digest)
    if destination.exists():
        load_import(project, digest)
        return {"created": False, "destination": str(destination)}
    required = snapshot_summary(manifest)["remote_peak_bytes_with_reserve"]
    if shutil.disk_usage(project).free < required:
        raise SnapshotPaused(
            "insufficient remote space including duplicate materialization"
        )
    destination.mkdir(parents=True, mode=0o700)
    for relative in (".transfer/chunks", ".transfer/objects", "dataset"):
        safe_path(destination, relative).mkdir(parents=True, mode=0o700)
    atomic_bytes(destination / "manifest.json", canonical(manifest))
    return {"created": True, "destination": str(destination)}


def chunk_status(destination: Path, manifest: dict) -> dict:
    rows = []
    for row in manifest["archive"]["chunks"]:
        name = f".transfer/chunks/{row['index']:08d}"
        complete = safe_path(destination, name)
        partial = safe_path(destination, name + ".part")
        if complete.exists():
            if (
                regular_file(complete).st_size != row["bytes"]
                or sha_file(complete) != row["sha256"]
            ):
                raise SnapshotError("completed remote chunk is corrupt")
            rows.append(
                {"index": row["index"], "complete": True, "offset": row["bytes"]}
            )
        elif partial.exists():
            if regular_file(partial).st_size > row["bytes"]:
                raise SnapshotError("partial chunk exceeds declared size")
            rows.append(
                {
                    "index": row["index"],
                    "complete": False,
                    "offset": partial.stat().st_size,
                    "prefix_sha256": sha_file(partial),
                }
            )
        else:
            rows.append(
                {
                    "index": row["index"],
                    "complete": False,
                    "offset": 0,
                    "prefix_sha256": hashlib.sha256(b"").hexdigest(),
                }
            )
    return {"chunks": rows, "destination": str(destination)}


def receive_chunk(
    destination: Path, manifest: dict, index: int, offset: int, prefix: str, source
) -> dict:
    if type(index) is not int or not 0 <= index < len(manifest["archive"]["chunks"]):
        raise SnapshotError("invalid chunk index")
    row = manifest["archive"]["chunks"][index]
    final = safe_path(destination, f".transfer/chunks/{index:08d}")
    partial = safe_path(destination, f".transfer/chunks/{index:08d}.part")
    if final.exists():
        if (
            regular_file(final).st_size != row["bytes"]
            or sha_file(final) != row["sha256"]
        ):
            raise SnapshotError("completed chunk changed")
        return {"index": index, "complete": True, "already_present": True}
    size = regular_file(partial).st_size if partial.exists() else 0
    actual_prefix = (
        sha_file(partial) if partial.exists() else hashlib.sha256(b"").hexdigest()
    )
    if (
        type(offset) is not int
        or offset != size
        or not 0 <= offset <= row["bytes"]
        or prefix != actual_prefix
    ):
        raise SnapshotError("partial chunk offset or prefix changed")
    remaining = row["bytes"] - offset
    if shutil.disk_usage(destination).free < remaining + RESERVE_BYTES:
        raise SnapshotPaused("low free space before receiving chunk")
    with partial.open("ab") as output:
        while remaining:
            data = source.read(min(BLOCK_BYTES, remaining))
            if not data:
                output.flush()
                os.fsync(output.fileno())
                raise SnapshotPaused("transfer interrupted; partial chunk retained")
            output.write(data)
            remaining -= len(data)
        output.flush()
        os.fsync(output.fileno())
    if source.read(1):
        raise SnapshotError("unexpected trailing chunk bytes")
    if sha_file(partial) != row["sha256"]:
        raise SnapshotError("received chunk SHA-256 mismatch")
    os.replace(partial, final)
    return {"index": index, "complete": True, "bytes": row["bytes"]}


def assemble_archive(destination: Path, manifest: dict) -> Path:
    final = safe_path(destination, ".transfer/snapshot.tar.gz")
    if final.exists():
        if (
            regular_file(final).st_size != manifest["archive"]["bytes"]
            or sha_file(final) != manifest["archive"]["sha256"]
        ):
            raise SnapshotError("assembled archive changed")
        return final
    if not all(
        row["complete"] for row in chunk_status(destination, manifest)["chunks"]
    ):
        raise SnapshotPaused("archive chunks are not complete")
    temporary = safe_path(destination, ".transfer/.assemble-" + uuid.uuid4().hex)
    if (
        shutil.disk_usage(destination).free
        < manifest["archive"]["bytes"] + RESERVE_BYTES
    ):
        raise SnapshotPaused("low free space before archive assembly")
    try:
        with temporary.open("xb") as output:
            for row in manifest["archive"]["chunks"]:
                with safe_path(
                    destination, f".transfer/chunks/{row['index']:08d}"
                ).open("rb") as source:
                    shutil.copyfileobj(source, output, BLOCK_BYTES)
            output.flush()
            os.fsync(output.fileno())
        if sha_file(temporary) != manifest["archive"]["sha256"]:
            raise SnapshotError("assembled archive SHA-256 mismatch")
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return final


def extract_objects(destination: Path, manifest: dict, archive: Path) -> None:
    expected = {"objects/" + row["sha256"]: row for row in manifest["objects"]}
    seen = set()
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            if (
                not member.isfile()
                or member.name not in expected
                or member.name in seen
            ):
                raise SnapshotError(
                    "unexpected, duplicate or nonregular archive member"
                )
            row = expected[member.name]
            if member.size != row["bytes"]:
                raise SnapshotError("archive object size mismatch")
            seen.add(member.name)
            target = safe_path(destination, ".transfer/" + member.name)
            if target.exists():
                if (
                    regular_file(target).st_size != row["bytes"]
                    or sha_file(target) != row["sha256"]
                ):
                    raise SnapshotError("existing imported object differs")
                continue
            if shutil.disk_usage(destination).free < row["bytes"] + RESERVE_BYTES:
                raise SnapshotPaused("low free space before object extraction")
            temporary = target.with_name(".object-" + uuid.uuid4().hex)
            try:
                source = bundle.extractfile(member)
                if source is None:
                    raise SnapshotError("archive object has no content")
                with source, temporary.open("xb") as output:
                    shutil.copyfileobj(source, output, BLOCK_BYTES)
                if sha_file(temporary) != row["sha256"]:
                    raise SnapshotError("extracted object SHA-256 mismatch")
                os.chmod(temporary, 0o400)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    if seen != set(expected):
        raise SnapshotError("archive omitted expected content objects")


def materialize(destination: Path, manifest: dict) -> dict:
    archive = assemble_archive(destination, manifest)
    extract_objects(destination, manifest, archive)
    dataset = safe_path(destination, "dataset")
    for row in manifest["files"]:
        target = safe_path(dataset, row["path"])
        if target.exists():
            if (
                target.stat().st_nlink != 1
                or target.stat().st_size != row["bytes"]
                or sha_file(target) != row["sha256"]
            ):
                raise SnapshotError("existing dataset file differs; refusing overwrite")
            continue
        if shutil.disk_usage(destination).free < row["bytes"] + RESERVE_BYTES:
            raise SnapshotPaused("low free space before data materialization")
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_path(dataset, row["path"])
        source = safe_path(destination, ".transfer/objects/" + row["sha256"])
        temporary = target.with_name(".copy-" + uuid.uuid4().hex)
        try:
            with source.open("rb") as incoming, temporary.open("xb") as output:
                shutil.copyfileobj(incoming, output, BLOCK_BYTES)
            if sha_file(temporary) != row["sha256"]:
                raise SnapshotError("materialized data SHA-256 mismatch")
            os.utime(temporary, ns=(row["source_mtime_ns"], row["source_mtime_ns"]))
            os.chmod(temporary, 0o400)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    result = {
        "status": "complete",
        "destination": str(destination),
        "dataset_root": str(dataset),
        "verified_files": len(manifest["files"]),
        "logical_bytes": manifest["logical_bytes"],
        "unique_bytes": manifest["unique_bytes"],
    }
    atomic_bytes(destination / "verification.json", canonical(result))
    return result


def remote_dispatch(request: dict, incoming) -> dict:
    project = Path(request["project_root"])
    if (
        not project.is_absolute()
        or project.resolve() != project
        or not (project / "data").is_dir()
    ):
        raise SnapshotError("remote project root must be existing and canonical")
    if request["operation"] == "preflight":
        space = shutil.disk_usage(project / "data")
        return {
            "project_root": str(project),
            "free_bytes": space.free,
            "total_bytes": space.total,
            "read_only": True,
        }
    digest = request["manifest_sha256"]
    if request["operation"] == "initialize":
        return initialize_import(project, digest, request["manifest"])
    destination, manifest = load_import(project, digest)
    if request["operation"] == "status":
        return chunk_status(destination, manifest)
    with remote_lock(destination):
        if request["operation"] == "chunk":
            return receive_chunk(
                destination,
                manifest,
                request["index"],
                request["offset"],
                request["prefix_sha256"],
                incoming,
            )
        if request["operation"] == "materialize":
            return materialize(destination, manifest)
    raise SnapshotError("unsupported remote operation")


def remote_program() -> str:
    functions = (
        canonical,
        sha_file,
        valid_relative,
        safe_path,
        regular_file,
        atomic_bytes,
        validate_manifest,
        snapshot_summary,
        import_root,
        load_import,
        remote_lock,
        initialize_import,
        chunk_status,
        receive_chunk,
        assemble_archive,
        extract_objects,
        materialize,
        remote_dispatch,
    )
    program = "from __future__ import annotations\nimport hashlib,json,os,re,shutil,stat,sys,tarfile,uuid\nfrom pathlib import Path,PurePosixPath\nfrom contextlib import contextmanager\n"
    program += f"CONTRACT={CONTRACT!r}\nBLOCK_BYTES={BLOCK_BYTES}\nRESERVE_BYTES={RESERVE_BYTES}\nHEX=re.compile(r'[0-9a-f]{{64}}\\Z')\n"
    program += (
        inspect.getsource(SnapshotError)
        + "\n"
        + inspect.getsource(SnapshotPaused)
        + "\n"
    )
    program += policy_program()
    program += "\n\n".join(inspect.getsource(function) for function in functions)
    program += "\ntry:\n header=sys.stdin.buffer.readline(32*1024*1024+1)\n if not header.endswith(b'\\n'):raise SnapshotError('request header too large')\n request=json.loads(header)\n result=remote_dispatch(request,sys.stdin.buffer)\n print(json.dumps({'ok':True,'result':result}),flush=True)\nexcept Exception as exc:\n print(json.dumps({'ok':False,'error_type':type(exc).__name__,'error':str(exc)}),flush=True)\n sys.exit(1)\n"
    return program


class SSHTransport:
    def __init__(self, timeout: int = 600, ssh_executable: str = "ssh"):
        sys.path.insert(0, str(PROJECT_ROOT))
        import ci_cd_deploy as deploy

        if not deploy.REMOTE_HOST or not Path(deploy._get_ssh_key()).is_file():
            raise SnapshotError("configured deployment host/key is unavailable")
        if not isinstance(ssh_executable, str) or not ssh_executable.strip():
            raise SnapshotError("SSH executable must be a nonempty command or path")
        self.project_root = deploy.REMOTE_DIR
        self.timeout = timeout
        self.arguments = [
            ssh_executable,
            "-C",
            "-T",
            "-i",
            deploy._get_ssh_key(),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
            f"{deploy.REMOTE_SSH_USER}@{deploy.REMOTE_HOST}",
        ]
        # Compress the fixed worker code to stay below Windows argv limits.
        encoded = base64.b64encode(zlib.compress(remote_program().encode())).decode()
        code = (
            "import base64,zlib;exec(zlib.decompress(base64.b64decode("
            + repr(encoded)
            + ")))"
        )
        self.command = "python3 -B -c " + shlex.quote(code)

    def call(self, operation: str, **parameters) -> dict:
        content = parameters.pop("content", b"")
        request = {
            "project_root": self.project_root,
            "operation": operation,
            **parameters,
        }
        result = subprocess.run(
            self.arguments + [self.command],
            input=canonical(request) + b"\n" + content,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        try:
            response = json.loads(result.stdout)
        except ValueError as exc:
            raise SnapshotError("SSH transport failed; no valid response") from exc
        if result.returncode or not response.get("ok"):
            raise SnapshotError(response.get("error", "remote operation failed"))
        return response["result"]


def load_snapshot(snapshot: Path) -> dict:
    safe_path(snapshot)
    path = safe_path(snapshot, "manifest.json")
    if regular_file(path).st_size > 32 * 1024**2:
        raise SnapshotError("snapshot manifest exceeds 32 MiB")
    manifest = json.loads(path.read_bytes())
    validate_manifest(manifest)
    archive = safe_path(snapshot, "snapshot.tar.gz")
    if (
        regular_file(archive).st_size != manifest["archive"]["bytes"]
        or sha_file(archive) != manifest["archive"]["sha256"]
    ):
        raise SnapshotError("local archive changed after preparation")
    # Verify content members before sending any bytes, including on resumes.
    # An archive-level hash alone does not prove its members match the manifest.
    expected = {"objects/" + row["sha256"]: row for row in manifest["objects"]}
    seen = set()
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            if (
                not member.isfile()
                or member.name not in expected
                or member.name in seen
            ):
                raise SnapshotError("local archive has an unexpected content member")
            row = expected[member.name]
            if member.size != row["bytes"]:
                raise SnapshotError("local archive member size mismatch")
            source = bundle.extractfile(member)
            if source is None:
                raise SnapshotError("local archive member has no content")
            digest = hashlib.sha256()
            with source:
                while block := source.read(BLOCK_BYTES):
                    digest.update(block)
            if digest.hexdigest() != row["sha256"]:
                raise SnapshotError("local archive member SHA-256 mismatch")
            seen.add(member.name)
    if seen != set(expected):
        raise SnapshotError("local archive omitted content objects")
    return manifest


def push_snapshot(snapshot: Path, transport, attempts: int = 3) -> dict:
    manifest = load_snapshot(snapshot)
    digest = hashlib.sha256(canonical(manifest)).hexdigest()
    transport.call("initialize", manifest_sha256=digest, manifest=manifest)
    archive = snapshot / "snapshot.tar.gz"
    stable = signature(archive.stat())
    for attempt in range(attempts):
        try:
            status = transport.call("status", manifest_sha256=digest)
            for state in status["chunks"]:
                if state["complete"]:
                    continue
                row = manifest["archive"]["chunks"][state["index"]]
                with archive.open("rb") as source:
                    source.seek(row["offset"])
                    content = source.read(row["bytes"])
                if (
                    signature(archive.stat()) != stable
                    or hashlib.sha256(content).hexdigest() != row["sha256"]
                ):
                    raise SnapshotError("local frozen archive changed during transfer")
                if (
                    hashlib.sha256(content[: state["offset"]]).hexdigest()
                    != state["prefix_sha256"]
                ):
                    raise SnapshotError(
                        "remote partial bytes differ from frozen archive"
                    )
                print(
                    json.dumps(
                        {
                            "phase": "uploading_chunk",
                            "index": row["index"],
                            "chunks": len(manifest["archive"]["chunks"]),
                            "resume_offset": state["offset"],
                        }
                    ),
                    flush=True,
                )
                transport.call(
                    "chunk",
                    manifest_sha256=digest,
                    index=row["index"],
                    offset=state["offset"],
                    prefix_sha256=state["prefix_sha256"],
                    content=content[state["offset"] :],
                )
            break
        except (subprocess.TimeoutExpired, OSError):
            if attempt + 1 == attempts:
                raise
    result = transport.call("materialize", manifest_sha256=digest)
    result["manifest_sha256"] = digest
    atomic_bytes(snapshot / "push_receipt.json", canonical(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "preflight", "push"))
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--chunk-mib", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--ssh-executable", default="ssh")
    args = parser.parse_args()
    try:
        snapshot = args.snapshot_dir.absolute()
        if args.mode == "prepare":
            result = prepare_snapshot(
                args.project_root, snapshot, args.chunk_mib * 1024**2
            )
        else:
            transport = SSHTransport(args.timeout_seconds, args.ssh_executable)
            if args.mode == "preflight":
                manifest = load_snapshot(snapshot)
                result = {
                    **snapshot_summary(manifest),
                    "remote": transport.call("preflight"),
                }
            else:
                ensure_quiet(args.project_root.resolve())
                result = push_snapshot(snapshot, transport)
    except (SnapshotError, subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            message = "SSH operation timed out; resume the same frozen snapshot"
        elif isinstance(exc, OSError):
            message = "Operating-system I/O failed; existing snapshot and checkpoints were retained"
        else:
            message = str(exc)[:1000]
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": message,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
