#!/usr/bin/env python3
"""Upload a frozen snapshot with at most eight independent SFTP streams.

Only the transport changes. The original uploader validates the archive,
initializes the import, verifies/renames chunks and materializes ordinary files.
A small remote coordinator holds its existing operation.lock during each SFTP
batch. No source scanning, snapshot rebuilding, service changes or cleanup occur.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import shlex
import shutil
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts import push_dataset_snapshot as base

SnapshotError = base.SnapshotError
SnapshotPaused = base.SnapshotPaused
BLOCK_BYTES = 32768
MAX_WORKERS = 8


def expected_destination(project_root: str, digest: str) -> str:
    path = PurePosixPath(project_root)
    if (
        not base.HEX.fullmatch(digest)
        or not path.is_absolute()
        or path == PurePosixPath("/")
        or path.as_posix() != project_root
        or ".." in path.parts
        or "\\" in project_root
        or ":" in project_root
        or any(ord(character) < 32 for character in project_root)
    ):
        raise SnapshotError("invalid canonical remote project or snapshot identity")
    return str(path / "data/dataset_imports" / ("local_" + digest))


def validate_rows(rows: list[dict]) -> None:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_WORKERS:
        raise SnapshotError("one to eight distinct chunks are required per batch")
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or type(row.get("index")) is not int
            or not 0 <= row["index"] < 100000000
            or row["index"] in seen
            or type(row.get("bytes")) is not int
            or not 0 < row["bytes"] <= 32 * 1024**2
            or type(row.get("offset")) is not int
            or not 0 <= row["offset"] <= row["bytes"]
            or not base.HEX.fullmatch(str(row.get("sha256", "")))
            or not base.HEX.fullmatch(str(row.get("prefix_sha256", "")))
        ):
            raise SnapshotError("invalid or duplicated SFTP chunk metadata")
        seen.add(row["index"])


@contextmanager
def guarded_batch(request: dict):
    """Runs remotely, using the original lock and no parsed manifest copies."""
    rows = request["rows"]
    validate_rows(rows)
    project = Path(request["project_root"])
    if not project.is_absolute() or project.resolve() != project:
        raise SnapshotError("remote project must be existing and canonical")
    destination = base.import_root(project, request["manifest_sha256"])
    manifest_path = base.safe_path(destination, "manifest.json")
    if (
        base.regular_file(manifest_path).st_size > 32 * 1024**2
        or base.sha_file(manifest_path) != request["manifest_sha256"]
    ):
        raise SnapshotError("remote manifest bytes differ from frozen identity")
    with base.remote_lock(destination):
        needed = sum(row["bytes"] - row["offset"] for row in rows)
        if shutil.disk_usage(destination).free < needed + base.RESERVE_BYTES:
            raise SnapshotPaused("low free space before SFTP batch")
        paths = {}
        ready = []
        for row in rows:
            name = f".transfer/chunks/{row['index']:08d}"
            final = base.safe_path(destination, name)
            partial = base.safe_path(destination, name + ".part")
            if final.exists():
                raise SnapshotPaused("chunk state changed; resume from fresh status")
            if not partial.exists():
                if row["offset"]:
                    raise SnapshotError("expected partial chunk disappeared")
                # Exclusive creation; SFTP itself never creates or truncates files.
                with partial.open("xb"):
                    pass
                os.chmod(partial, 0o600)
            info = base.regular_file(partial)
            if (
                info.st_size != row["offset"]
                or base.sha_file(partial) != row["prefix_sha256"]
            ):
                raise SnapshotError("partial chunk offset or prefix changed")
            paths[row["index"]] = (partial, info.st_dev, info.st_ino)
            ready.append({"index": row["index"], "mtime": int(info.st_mtime)})
        yield {"status": "locked", "rows": ready}
        for row in rows:
            partial, device, inode = paths[row["index"]]
            info = base.regular_file(partial)
            if (
                (info.st_dev, info.st_ino) != (device, inode)
                or info.st_size != row["bytes"]
                or base.sha_file(partial) != row["sha256"]
            ):
                raise SnapshotError("SFTP chunk identity, length or SHA-256 changed")


def guard_program() -> str:
    """Emit a small stdlib-only worker; never load the large JSON inventory."""
    names = (
        base.valid_relative,
        base.safe_path,
        base.regular_file,
        base.sha_file,
        base.import_root,
        base.remote_lock,
    )
    program = (
        "import os,sys,json,stat,re,hashlib,shutil,select,time\n"
        "from pathlib import Path,PurePosixPath\n"
        "from contextlib import contextmanager\n"
        "from types import SimpleNamespace\n"
        "class SnapshotError(RuntimeError): pass\n"
        "class SnapshotPaused(SnapshotError): pass\n"
        "HEX=re.compile(r'[0-9a-f]{64}\\Z')\n"
        f"BLOCK_BYTES={base.BLOCK_BYTES}\n"
        f"MAX_WORKERS={MAX_WORKERS}\n"
        f"RESERVE_BYTES={base.RESERVE_BYTES}\n"
    )
    program += "\n\n".join(inspect.getsource(function) for function in names)
    program += "\nbase=SimpleNamespace(**globals())\n"
    program += inspect.getsource(validate_rows) + "\n"
    program += inspect.getsource(guarded_batch) + "\n"
    program += """
def read_line(limit, seconds):
    deadline = time.monotonic() + seconds
    output = bytearray()
    while len(output) <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            raise SnapshotPaused("SFTP coordinator deadline exceeded")
        data = os.read(0, min(65536, limit + 1 - len(output)))
        if not data:
            raise SnapshotPaused("SFTP coordinator disconnected")
        output.extend(data)
        if b"\\n" in output:
            if not output.endswith(b"\\n") or output.count(b"\\n") != 1:
                raise SnapshotError("invalid coordinator framing")
            return bytes(output[:-1])
    raise SnapshotError("coordinator request too large")

try:
    request = json.loads(read_line(65536, 60))
    timeout = request["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise SnapshotError("invalid coordinator timeout")
    with guarded_batch(request) as ready:
        print(json.dumps(ready), flush=True)
        # The local watchdog closes data streams before this failsafe expires.
        if read_line(32, timeout + 90) != b"finish":
            raise SnapshotPaused("SFTP batch aborted; partial chunks retained")
    print(json.dumps({"status": "verified"}), flush=True)
except Exception as exc:
    print(json.dumps({"status": "failed", "error_type": type(exc).__name__}),
          flush=True)
    sys.exit(1)
"""
    return program


class ParamikoFactory:
    """Use only the deployment identity and previously trusted host keys."""

    def __init__(self):
        import ci_cd_deploy as deploy

        self.host = deploy.REMOTE_HOST
        self.user = deploy.REMOTE_SSH_USER
        self.key = deploy._get_ssh_key()
        self.project_root = deploy.REMOTE_DIR
        self.known_hosts = Path.home() / ".ssh/known_hosts"
        if (
            not self.host
            or not Path(self.key).is_file()
            or not self.known_hosts.is_file()
        ):
            raise SnapshotError("deployment identity or existing known_hosts missing")

    def connect(self):
        try:
            import paramiko
        except ImportError as exc:
            raise SnapshotError("existing paramiko installation is required") from exc
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys(str(self.known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                hostname=self.host,
                port=22,
                username=self.user,
                key_filename=self.key,
                look_for_keys=False,
                allow_agent=False,
                timeout=15,
                auth_timeout=15,
                banner_timeout=15,
                compress=True,
            )
            transport = client.get_transport()
            transport.set_keepalive(10)
            transport.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return client
        except BaseException:
            client.close()
            raise


def read_guard_response(channel, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    while len(buffer) <= 8192:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SnapshotPaused("SFTP coordinator response timed out")
        channel.settimeout(remaining)
        data = channel.recv(1)
        if not data:
            raise SnapshotPaused("SFTP coordinator closed before verification")
        if data == b"\n":
            try:
                result = json.loads(buffer)
            except ValueError as exc:
                raise SnapshotError("invalid SFTP coordinator response") from exc
            if not isinstance(result, dict):
                raise SnapshotError("invalid SFTP coordinator response")
            return result
        buffer.extend(data)
    raise SnapshotError("SFTP coordinator response too large")


class RemoteGuard:
    def __init__(self, factory, request: dict):
        self.factory, self.request = factory, request
        self.client = self.channel = None

    def __enter__(self):
        try:
            self.client = self.factory.connect()
            self.channel = self.client.get_transport().open_session(timeout=15)
            self.channel.settimeout(60)
            self.channel.exec_command(
                "python3 -u -B -c " + shlex.quote(guard_program())
            )
            self.channel.sendall(base.canonical(self.request) + b"\n")
            ready = read_guard_response(self.channel, 60)
            if ready.get("status") != "locked":
                raise SnapshotPaused("SFTP guard refused batch; partials retained")
            return ready
        except BaseException:
            self.close()
            raise

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self.channel.settimeout(60)
                self.channel.sendall(b"finish\n")
                response = read_guard_response(self.channel, 60)
                if response.get("status") != "verified":
                    raise SnapshotError("SFTP guard did not verify completed batch")
            else:
                try:
                    self.channel.settimeout(10)
                    self.channel.sendall(b"abort\n")
                    read_guard_response(self.channel, 10)
                except Exception:  # noqa: BLE001,S110 - retain the primary failure
                    pass
        finally:
            self.close()

    def close(self):
        if self.channel is not None:
            self.channel.close()
        if self.client is not None:
            self.client.close()

    def healthy(self):
        return (
            self.channel is not None
            and not self.channel.closed
            and not self.channel.exit_status_ready()
        )


class BatchConnections:
    """Abort/timeout closes only this batch's channels and SSH connections."""

    def __init__(self, timeout: int, healthy=lambda: True):
        self.lock = threading.Lock()
        self.clients = set()
        self.cancelled = threading.Event()
        self.stopped = threading.Event()
        self.healthy = healthy
        self.deadline = time.monotonic() + timeout
        self.monitor = threading.Thread(target=self.watch, daemon=True)

    def __enter__(self):
        self.monitor.start()
        return self

    def __exit__(self, *unused):
        self.stopped.set()
        self.monitor.join(timeout=1)
        self.cancel()

    def watch(self):
        while not self.stopped.wait(0.1):
            if time.monotonic() >= self.deadline or not self.healthy():
                self.cancel()
                return

    def add(self, client):
        with self.lock:
            if self.cancelled.is_set():
                client.close()
                raise SnapshotPaused("SFTP batch cancelled; partial chunks retained")
            self.clients.add(client)

    def remove(self, client):
        with self.lock:
            self.clients.discard(client)
        client.close()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            clients = tuple(self.clients)
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001,S110 - close every other owned client
                pass

    def check(self):
        if (
            self.cancelled.is_set()
            or time.monotonic() >= self.deadline
            or not self.healthy()
        ):
            raise SnapshotPaused("SFTP batch cancelled or timed out; partials retained")


def check_sftp_path(sftp, destination: str, index: int) -> str:
    path = PurePosixPath(destination) / ".transfer/chunks" / f"{index:08d}.part"
    for directory in reversed(path.parents):
        info = sftp.lstat(str(directory))
        if not stat.S_ISDIR(info.st_mode):
            raise SnapshotError("remote ancestor is not an independent directory")
    if sftp.normalize(str(path.parent)) != str(path.parent):
        raise SnapshotError("remote chunks directory is not canonical")
    info = sftp.lstat(str(path))
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotError("SFTP target must be an existing regular partial file")
    return str(path)


def upload_one(factory, destination, row, content, ready, active):
    active.check()
    if row["offset"] == len(content):
        return
    client = factory.connect()
    active.add(client)
    try:
        sftp = client.open_sftp()
        try:
            sftp.get_channel().settimeout(
                min(60, max(1, active.deadline - time.monotonic()))
            )
            partial = check_sftp_path(sftp, destination, row["index"])
            info = sftp.lstat(partial)
            if info.st_size != row["offset"] or int(info.st_mtime) != ready["mtime"]:
                raise SnapshotError("SFTP partial changed since locked validation")
            # r+b sends neither CREATE, APPEND nor TRUNCATE flags in SFTP v3.
            with sftp.open(partial, "r+b", bufsize=0) as output:
                opened = output.stat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_size != row["offset"]
                    or int(opened.st_mtime) != ready["mtime"]
                ):
                    raise SnapshotError("opened partial differs from checked file")
                output.seek(row["offset"])
                output.set_pipelined(True)
                for offset in range(row["offset"], len(content), BLOCK_BYTES):
                    active.check()
                    output.write(content[offset : offset + BLOCK_BYTES])
                # close() collects every pipelined write response before returning.
            active.check()
            if sftp.lstat(partial).st_size != row["bytes"]:
                raise SnapshotError("SFTP partial length differs after write")
        finally:
            sftp.close()
    finally:
        active.remove(client)


def checked_status(status, destination, manifest):
    if status.get("destination") != destination:
        raise SnapshotError("remote status returned an unexpected destination")
    rows = status.get("chunks")
    chunks = manifest["archive"]["chunks"]
    if not isinstance(rows, list) or len(rows) != len(chunks):
        raise SnapshotError("remote status has an invalid chunk inventory")
    for index, state in enumerate(rows):
        row = chunks[index]
        if (
            not isinstance(state, dict)
            or state.get("index") != index
            or type(state.get("complete")) is not bool
            or type(state.get("offset")) is not int
            or not 0 <= state["offset"] <= row["bytes"]
            or (state["complete"] and state["offset"] != row["bytes"])
            or (
                not state["complete"]
                and not base.HEX.fullmatch(str(state.get("prefix_sha256", "")))
            )
        ):
            raise SnapshotError("invalid or reordered remote chunk state")
    return rows


def read_chunk(archive, stable, row):
    if base.signature(base.regular_file(archive)) != stable:
        raise SnapshotError("frozen local archive changed before SFTP batch")
    with archive.open("rb") as incoming:
        incoming.seek(row["offset"])
        content = incoming.read(row["bytes"])
    if (
        base.signature(base.regular_file(archive)) != stable
        or hashlib.sha256(content).hexdigest() != row["sha256"]
    ):
        raise SnapshotError("frozen local archive chunk SHA-256 changed")
    return content


def push_snapshot_sftp(
    snapshot: Path,
    transport,
    factory,
    *,
    concurrency: int = 4,
    timeout: int = 1200,
    resume_existing: bool = False,
    guard_factory=RemoteGuard,
) -> dict:
    if type(concurrency) is not int or not 1 <= concurrency <= MAX_WORKERS:
        raise SnapshotError("SFTP concurrency must be between one and eight")
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise SnapshotError("SFTP batch timeout must be between 1 and 7200 seconds")
    manifest = base.load_snapshot(snapshot)
    digest = hashlib.sha256(base.canonical(manifest)).hexdigest()
    destination = expected_destination(transport.project_root, digest)
    if factory.project_root != transport.project_root:
        raise SnapshotError("SFTP and control transports target different projects")
    archive = base.safe_path(snapshot, "snapshot.tar.gz")
    stable = base.signature(base.regular_file(archive))
    if not resume_existing:
        result = transport.call("initialize", manifest_sha256=digest, manifest=manifest)
        if result.get("destination") != destination:
            raise SnapshotError("remote initialization returned an unexpected path")
    states = checked_status(
        transport.call("status", manifest_sha256=digest), destination, manifest
    )
    pending = [state for state in states if not state["complete"]]
    for start in range(0, len(pending), concurrency):
        batch, contents = [], {}
        for state in pending[start : start + concurrency]:
            chunk = manifest["archive"]["chunks"][state["index"]]
            content = read_chunk(archive, stable, chunk)
            if (
                hashlib.sha256(content[: state["offset"]]).hexdigest()
                != state["prefix_sha256"]
            ):
                raise SnapshotError("remote partial prefix differs from frozen archive")
            row = {
                **chunk,
                "offset": state["offset"],
                "prefix_sha256": state["prefix_sha256"],
            }
            batch.append(row)
            contents[row["index"]] = content
        request = {
            "project_root": transport.project_root,
            "manifest_sha256": digest,
            "rows": batch,
            "timeout_seconds": timeout,
        }
        print(
            json.dumps(
                {
                    "phase": "sftp_batch",
                    "indices": [row["index"] for row in batch],
                    "concurrency": concurrency,
                }
            ),
            flush=True,
        )
        guard = guard_factory(factory, request)
        with guard as ready:
            ready_rows = {item["index"]: item for item in ready.get("rows", [])}
            if set(ready_rows) != {row["index"] for row in batch}:
                raise SnapshotError("guard did not bind every requested chunk")
            with BatchConnections(
                timeout, getattr(guard, "healthy", lambda: True)
            ) as active:
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = [
                        executor.submit(
                            upload_one,
                            factory,
                            destination,
                            row,
                            contents[row["index"]],
                            ready_rows[row["index"]],
                            active,
                        )
                        for row in batch
                    ]
                    try:
                        for future in as_completed(futures):
                            future.result()
                    except BaseException:
                        active.cancel()
                        for future in futures:
                            future.cancel()
                        raise
                active.check()
            if base.signature(base.regular_file(archive)) != stable:
                raise SnapshotError("frozen local archive changed during SFTP batch")
        # No SFTP writer remains. Keep original finalization and its global lock.
        for row in batch:
            transport.call(
                "chunk",
                manifest_sha256=digest,
                index=row["index"],
                offset=row["bytes"],
                prefix_sha256=row["sha256"],
                content=b"",
            )
            print(
                json.dumps(
                    {
                        "phase": "verified_chunk",
                        "index": row["index"],
                        "chunks": len(states),
                    }
                ),
                flush=True,
            )
    final_states = checked_status(
        transport.call("status", manifest_sha256=digest), destination, manifest
    )
    if not all(state["complete"] for state in final_states):
        raise SnapshotPaused(
            "incomplete chunks remain; materialization was not started"
        )
    if base.signature(base.regular_file(archive)) != stable:
        raise SnapshotError("frozen local archive changed before materialization")
    result = transport.call("materialize", manifest_sha256=digest)
    result.update(manifest_sha256=digest, transport="sftp", concurrency=concurrency)
    base.atomic_bytes(snapshot / "push_receipt.json", base.canonical(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--ssh-executable", default="ssh")
    parser.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args()
    try:
        base.ensure_quiet(PROJECT_ROOT)
        transport = base.SSHTransport(args.timeout_seconds, args.ssh_executable)
        result = push_snapshot_sftp(
            args.snapshot_dir.absolute(),
            transport,
            ParamikoFactory(),
            concurrency=args.concurrency,
            timeout=args.timeout_seconds,
            resume_existing=args.resume_existing,
        )
    except Exception as exc:  # noqa: BLE001 - sanitize external SSH diagnostics
        # In particular, never render Paramiko/SSH exceptions with credentials,
        # host/key paths, command payloads or remote diagnostic text.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": "SFTP upload failed; frozen snapshot and remote partials retained",
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
