#!/usr/bin/env python3
"""Snapshot server market datasets and resumably pull a verified local copy.

The export contains data only. It excludes configuration, messages, optimizer
artifacts and restore staging. A pull always extracts into its own dataset root.
This file intentionally uses only the standard library so it can run on the
server independently of the deployed application version.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

DATA_DIRECTORIES = (
    "data/reference_universe",
    "data/point_in_time",
    "cache/data",
    "cache/announcement_content",
    "cache/announcement_extraction",
)
CONTRACT = "server-market-snapshot-1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    temporary.replace(path)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def source_files(root: Path) -> list[Path]:
    candidates = []
    for relative in DATA_DIRECTORIES:
        folder = root / relative
        if folder.is_dir() and folder.resolve().is_relative_to(root.resolve()):
            candidates.extend(folder.rglob("*"))
    candidates.extend((root / "data").glob("*_history.csv"))
    candidates.extend((root / "data").glob("*_history.csv.meta*"))
    return sorted(
        {
            path
            for path in candidates
            if path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve())
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
            and not path.name.endswith((".tar.gz", ".tgz", ".zip", ".tmp", ".part"))
            and path.suffix.lower() in {".csv", ".json", ".yaml", ".yml", ".meta"}
        }
    )


def export_snapshot(root: Path, job: Path) -> dict:
    root, job = root.resolve(), job.resolve()
    job.mkdir(parents=True, exist_ok=True)
    status_path = job / "status.json"
    if status_path.exists():
        previous = json.loads(status_path.read_text(encoding="utf-8"))
        archive = job / "snapshot.tar.gz"
        manifest_path = job / "manifest.json"
        if (
            previous.get("state") == "complete"
            and archive.exists()
            and digest(archive) == previous.get("sha256")
            and manifest_path.exists()
            and digest(manifest_path) == previous.get("manifest_sha256")
        ):
            return previous
    lock = job / "export.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    os.close(descriptor)
    started = time.monotonic()
    status = {
        "contract": CONTRACT,
        "state": "running",
        "pid": os.getpid(),
        "started_at": utc_now(),
        "completed_files": 0,
    }
    partial = job / "snapshot.tar.gz.part"
    try:
        files = source_files(root)
        source_bytes = sum(path.stat().st_size for path in files)
        if shutil.disk_usage(job).free < source_bytes * 2 + 256 * 1024**2:
            raise RuntimeError("insufficient free disk for a safe data snapshot")
        status.update(total_files=len(files), source_bytes=source_bytes)
        write_json(status_path, status)
        entries = []
        with tarfile.open(partial, "w:gz", compresslevel=1) as archive:
            for index, path in enumerate(files, 1):
                before = path.stat()
                content = path.read_bytes()
                after = path.stat()
                if (before.st_mtime_ns, before.st_size) != (
                    after.st_mtime_ns,
                    after.st_size,
                ):
                    raise RuntimeError(f"source changed while reading: {path}")
                relative = path.relative_to(root).as_posix()
                info = tarfile.TarInfo(relative)
                info.size, info.mtime, info.mode = len(content), before.st_mtime, 0o600
                archive.addfile(info, io.BytesIO(content))
                entries.append(
                    {
                        "path": relative,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "source_mtime_ns": before.st_mtime_ns,
                    }
                )
                if index % 100 == 0:
                    status.update(completed_files=index, updated_at=utc_now())
                    write_json(status_path, status)
        final_archive = job / "snapshot.tar.gz"
        partial.replace(final_archive)
        manifest = {
            "contract": CONTRACT,
            "created_at": utc_now(),
            "source_root": str(root),
            "files": entries,
        }
        write_json(job / "manifest.json", manifest)
        status.update(
            state="complete",
            completed_files=len(entries),
            archive_bytes=final_archive.stat().st_size,
            sha256=digest(final_archive),
            manifest_sha256=digest(job / "manifest.json"),
            elapsed_seconds=round(time.monotonic() - started, 3),
            completed_at=utc_now(),
        )
        write_json(status_path, status)
        return status
    except BaseException as exc:
        status.update(
            state="failed", error=f"{type(exc).__name__}: {exc}", updated_at=utc_now()
        )
        write_json(status_path, status)
        raise
    finally:
        lock.unlink(missing_ok=True)


def ssh_arguments(args: argparse.Namespace) -> list[str]:
    return [
        args.ssh_executable,
        "-n",
        "-i",
        str(args.identity_file),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"UserKnownHostsFile={args.known_hosts.as_posix()}",
        "-o",
        "StrictHostKeyChecking=yes",
        args.host,
    ]


def remote_read(args: argparse.Namespace, filename: str) -> bytes:
    command = "cat -- " + shlex.quote(args.remote_job_dir.rstrip("/") + "/" + filename)
    for attempt in range(1, args.attempts + 1):
        try:
            result = subprocess.run(
                ssh_arguments(args) + [command],
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
            )
            return result.stdout
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            if attempt == args.attempts:
                raise
            time.sleep(min(30, attempt * 5))
    raise RuntimeError("no remote read attempts configured")


def extract_verified(archive_path: Path, manifest: dict, destination: Path) -> None:
    if destination.is_symlink() or destination.resolve() != destination.absolute():
        raise ValueError("snapshot destination must not redirect through a symlink")
    expected = {entry["path"]: entry for entry in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise ValueError("duplicate snapshot manifest path")
    for name in expected:
        relative = PurePosixPath(name)
        if (
            str(relative) != name
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or ":" in name
        ):
            raise ValueError(f"unsafe or noncanonical manifest path: {name}")
    seen = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if (
                not member.isfile()
                or relative.is_absolute()
                or ".." in relative.parts
                or "\\" in member.name
                or ":" in member.name
                or member.name not in expected
                or member.name in seen
            ):
                raise ValueError(f"unsafe or unexpected snapshot member: {member.name}")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("snapshot path escapes destination")
            entry = expected[member.name]
            if member.size != entry["bytes"]:
                raise ValueError(f"snapshot size mismatch: {member.name}")
            if target.exists() and digest(target) == entry["sha256"]:
                seen.add(member.name)
                continue
            source = archive.extractfile(member)
            assert source is not None
            content = source.read()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError(f"snapshot checksum mismatch: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            with temporary.open("xb") as handle:
                handle.write(content)
            temporary.replace(target)
            seen.add(member.name)
    if seen != set(expected):
        raise ValueError("snapshot is missing manifest files")


def pull_snapshot(args: argparse.Namespace) -> dict:
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    state = {
        "contract": CONTRACT,
        "state": "waiting_for_export",
        "pid": os.getpid(),
        "started_at": utc_now(),
        "remote_job_dir": args.remote_job_dir,
    }
    progress = output / "transfer_status.json"
    write_json(progress, state)
    try:
        deadline = time.monotonic() + args.export_timeout_seconds
        while True:
            remote = json.loads(remote_read(args, "status.json"))
            if remote["state"] == "failed":
                raise RuntimeError(f"server export failed: {remote.get('error')}")
            if remote["state"] == "complete":
                break
            if time.monotonic() > deadline:
                raise TimeoutError("server export did not finish in time")
            time.sleep(10)
        manifest_bytes = remote_read(args, "manifest.json")
        if hashlib.sha256(manifest_bytes).hexdigest() != remote["manifest_sha256"]:
            raise ValueError("server manifest checksum mismatch")
        manifest = json.loads(manifest_bytes)
        (output / "manifest.json").write_bytes(manifest_bytes)
        write_json(output / "server_status.json", remote)
        required = remote["archive_bytes"] + remote["source_bytes"] + 256 * 1024**2
        if shutil.disk_usage(output).free < required:
            raise RuntimeError("insufficient local disk for verified extraction")
        state.update(
            state="transferring",
            expected_bytes=remote["archive_bytes"],
            files=remote["total_files"],
            updated_at=utc_now(),
        )
        write_json(progress, state)
        partial = output / "snapshot.tar.gz.part"
        final_archive = output / "snapshot.tar.gz"
        if not (final_archive.exists() and digest(final_archive) == remote["sha256"]):
            identity_path = output / "partial_identity.json"
            identity = {"sha256": remote["sha256"], "bytes": remote["archive_bytes"]}
            try:
                old_identity = json.loads(identity_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old_identity = None
            if partial.exists() and old_identity != identity:
                partial.replace(output / f"snapshot.stale.{time.time_ns()}.part")
            write_json(identity_path, identity)
            if partial.exists() and partial.stat().st_size > remote["archive_bytes"]:
                partial.replace(output / f"snapshot.oversized.{time.time_ns()}.part")
            for attempt in range(1, args.attempts + 1):
                offset = partial.stat().st_size if partial.exists() else 0
                state.update(
                    attempt=attempt, transferred_bytes=offset, updated_at=utc_now()
                )
                write_json(progress, state)
                code = (
                    "import shutil,sys; f=open(sys.argv[1],'rb'); "
                    "f.seek(int(sys.argv[2])); shutil.copyfileobj(f,sys.stdout.buffer,1048576)"
                )
                command = " ".join(
                    shlex.quote(value)
                    for value in [
                        "python3",
                        "-c",
                        code,
                        args.remote_job_dir.rstrip("/") + "/snapshot.tar.gz",
                        str(offset),
                    ]
                )
                with partial.open("ab") as handle, (output / "ssh_transfer.log").open(
                    "ab"
                ) as log:
                    try:
                        result = subprocess.run(
                            ssh_arguments(args) + [command],
                            check=False,
                            stdin=subprocess.DEVNULL,
                            stdout=handle,
                            stderr=log,
                            timeout=args.transfer_timeout_seconds,
                        )
                        return_code = result.returncode
                    except subprocess.TimeoutExpired:
                        return_code = 124
                if (
                    return_code == 0
                    and partial.stat().st_size == remote["archive_bytes"]
                ):
                    if digest(partial) == remote["sha256"]:
                        break
                    partial.replace(output / f"snapshot.corrupt.{time.time_ns()}.part")
                if attempt == args.attempts:
                    raise RuntimeError(
                        "SSH transfer retries exhausted; partial file retained"
                    )
                time.sleep(min(30, attempt * 5))
            if digest(partial) != remote["sha256"]:
                raise ValueError("downloaded archive checksum mismatch")
            partial.replace(final_archive)
        state.update(
            state="verifying",
            transferred_bytes=final_archive.stat().st_size,
            updated_at=utc_now(),
        )
        write_json(progress, state)
        extract_verified(final_archive, manifest, output / "dataset")
        state.update(
            state="complete",
            completed_at=utc_now(),
            elapsed_seconds=round(time.monotonic() - started, 3),
            sha256=remote["sha256"],
            extracted_bytes=remote["source_bytes"],
            dataset=str(output / "dataset"),
        )
        write_json(progress, state)
        return state
    except BaseException as exc:
        state.update(
            state="failed", error=f"{type(exc).__name__}: {exc}", updated_at=utc_now()
        )
        write_json(progress, state)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    export = sub.add_parser("export")
    export.add_argument("--project-root", type=Path, required=True)
    export.add_argument("--job-dir", type=Path, required=True)
    pull = sub.add_parser("pull")
    pull.add_argument("--host", required=True, help="user@host for existing SSH access")
    pull.add_argument("--identity-file", type=Path, required=True)
    pull.add_argument("--known-hosts", type=Path, required=True)
    pull.add_argument("--ssh-executable", default="ssh")
    pull.add_argument("--remote-job-dir", required=True)
    pull.add_argument("--output-dir", type=Path, required=True)
    pull.add_argument("--attempts", type=int, default=3)
    pull.add_argument("--export-timeout-seconds", type=int, default=3600)
    pull.add_argument("--transfer-timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    if (
        args.mode == "pull"
        and min(
            args.attempts, args.export_timeout_seconds, args.transfer_timeout_seconds
        )
        < 1
    ):
        parser.error("retry and timeout settings must be positive")
    result = (
        export_snapshot(args.project_root, args.job_dir)
        if args.mode == "export"
        else pull_snapshot(args)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
