"""Exercise parallel SFTP against real snapshot validation and finalization."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from scripts import push_dataset_snapshot_sftp as sftp_push

base = sftp_push.base
REMOTE = "/srv/project"


@pytest.fixture(autouse=True)
def quiet_project(monkeypatch):
    monkeypatch.setattr(base, "active_writers", lambda root: [])


def prepared(tmp_path, size=180000):
    root = tmp_path / "source"
    source = root / "data/point_in_time/market/000001.csv"
    source.parent.mkdir(parents=True)
    # Incompressible test bytes exercise several independently bounded chunks.
    source.write_bytes(b"date,raw_close\n" + os.urandom(size).hex().encode())
    snapshot = root / "temp_test_cache/frozen"
    base.prepare_snapshot(root, snapshot, chunk_bytes=65536)
    return snapshot, source


class LocalControl:
    project_root = REMOTE

    def __init__(self, root):
        self.root = root.resolve()
        (self.root / "data").mkdir(parents=True)
        self.operations = []
        self.factory = None

    def call(self, operation, **parameters):
        if operation in {"chunk", "materialize"}:
            assert not self.factory.locked
            assert self.factory.active == 0
        self.operations.append((operation, parameters.copy()))
        content = parameters.pop("content", b"")
        result = base.remote_dispatch(
            {"project_root": str(self.root), "operation": operation, **parameters},
            io.BytesIO(content),
        )
        for key in ("destination", "dataset_root"):
            if key in result:
                result[key] = (
                    REMOTE + "/" + Path(result[key]).relative_to(self.root).as_posix()
                )
        return result


class MockFile:
    def __init__(self, client, path, mode):
        self.client, self.path = client, path
        self.stream = path.open(mode, buffering=0)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.stream.close()

    def stat(self):
        return os.fstat(self.stream.fileno())

    def seek(self, offset):
        self.client.factory.seeks.append(offset)
        self.stream.seek(offset)

    def set_pipelined(self, value):
        assert value is True

    def write(self, content):
        factory = self.client.factory
        assert factory.locked
        if self.client.closed.is_set():
            raise OSError("owned SFTP connection closed")
        if factory.on_write:
            factory.on_write(self, content)
        if self.client.closed.is_set():
            raise OSError("owned SFTP connection closed")
        self.stream.write(content)


class MockSFTP:
    def __init__(self, client):
        self.client = client

    def get_channel(self):
        return self

    def settimeout(self, seconds):
        assert 0 < seconds <= 60

    def lstat(self, path):
        factory = self.client.factory
        if path in factory.overrides:
            return factory.overrides[path]
        if path in {"/", "/srv"}:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)
        return factory.local(path).lstat()

    def normalize(self, path):
        return path

    def open(self, path, mode, bufsize):
        assert mode == "r+b" and bufsize == 0
        assert self.client.factory.locked
        self.client.factory.opens.append(path)
        return MockFile(self.client, self.client.factory.local(path), mode)

    def close(self):
        pass


class MockClient:
    def __init__(self, factory):
        self.factory = factory
        self.closed = threading.Event()

    def open_sftp(self):
        return MockSFTP(self)

    def close(self):
        with self.factory.lock:
            if not self.closed.is_set():
                self.closed.set()
                self.factory.active -= 1
                self.factory.events.append("connection_closed")


class MockFactory:
    project_root = REMOTE

    def __init__(self, control):
        self.control = control
        control.factory = self
        self.lock = threading.Lock()
        self.locked = False
        self.active = self.maximum = 0
        self.events, self.opens, self.seeks, self.clients = [], [], [], []
        self.overrides = {}
        self.on_write = self.before_guard = None
        self.alive = True

    def local(self, remote):
        path = PurePosixPath(remote)
        assert path.is_relative_to(PurePosixPath(REMOTE))
        return self.control.root / path.relative_to(REMOTE).as_posix()

    def connect(self):
        assert self.locked
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            client = MockClient(self)
            self.clients.append(client)
            return client


class LocalGuard:
    def __init__(self, factory, request):
        self.factory = factory
        self.request = {**request, "project_root": str(factory.control.root)}

    def __enter__(self):
        if self.factory.before_guard:
            self.factory.before_guard(self)
        self.context = sftp_push.guarded_batch(self.request)
        ready = self.context.__enter__()
        self.factory.locked = True
        self.factory.events.append("guard_locked")
        return ready

    def __exit__(self, *error):
        assert self.factory.active == 0
        assert all(client.closed.is_set() for client in self.factory.clients)
        try:
            return self.context.__exit__(*error)
        finally:
            self.factory.locked = False
            self.factory.events.append("guard_released")

    def healthy(self):
        return self.factory.alive


def make_case(tmp_path, size=180000):
    snapshot, source = prepared(tmp_path, size)
    control = LocalControl(tmp_path / "server")
    factory = MockFactory(control)
    return snapshot, source, control, factory


def run(snapshot, control, factory, **options):
    return sftp_push.push_snapshot_sftp(
        snapshot, control, factory, guard_factory=LocalGuard, **options
    )


def initialize(snapshot, control):
    manifest = base.load_snapshot(snapshot)
    digest = hashlib.sha256(base.canonical(manifest)).hexdigest()
    reply = control.call("initialize", manifest_sha256=digest, manifest=manifest)
    destination = control.factory.local(reply["destination"])
    return manifest, digest, destination


@pytest.mark.parametrize("concurrency", [None, 8])
def test_parallel_roundtrip_original_verification_and_finalization(
    tmp_path, concurrency
):
    snapshot, source, control, factory = make_case(tmp_path, size=450000)
    workers = 4 if concurrency is None else concurrency
    barrier = threading.Barrier(workers)
    seen = set()
    lock = threading.Lock()

    def synchronize(output, content):
        with lock:
            first = output.path.name not in seen
            seen.add(output.path.name)
        if first and len(factory.opens) <= workers:
            barrier.wait(timeout=3)
        time.sleep(0.001)

    factory.on_write = synchronize
    options = {} if concurrency is None else {"concurrency": concurrency}
    result = run(snapshot, control, factory, **options)
    assert result["status"] == "complete"
    assert factory.maximum == workers
    assert result["concurrency"] == workers
    assert factory.active == 0
    target = (
        factory.local(result["dataset_root"]) / "data/point_in_time/market/000001.csv"
    )
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_nlink == 1
    chunks = [params for name, params in control.operations if name == "chunk"]
    assert len(chunks) >= workers
    assert all(row["content"] == b"" for row in chunks)
    assert control.operations[-1][0] == "materialize"
    assert (
        json.loads((snapshot / "push_receipt.json").read_text())["transport"] == "sftp"
    )
    assert not (control.root / "config").exists()


def test_resume_existing_prefix_and_completed_chunks_are_not_reuploaded(tmp_path):
    snapshot, source, control, factory = make_case(tmp_path)
    manifest, digest, destination = initialize(snapshot, control)
    archive = (snapshot / "snapshot.tar.gz").read_bytes()
    first, second = manifest["archive"]["chunks"][:2]
    control.call(
        "chunk",
        manifest_sha256=digest,
        index=0,
        offset=0,
        prefix_sha256=hashlib.sha256(b"").hexdigest(),
        content=archive[: first["bytes"]],
    )
    partial = destination / ".transfer/chunks/00000001.part"
    prefix = archive[second["offset"] : second["offset"] + 777]
    partial.write_bytes(prefix)
    control.operations.clear()
    result = run(snapshot, control, factory, resume_existing=True)
    assert result["status"] == "complete"
    assert 777 in factory.seeks
    assert all(not path.endswith("00000000.part") for path in factory.opens)
    assert all(name != "initialize" for name, _ in control.operations)
    assert (destination / ".transfer/chunks/00000001").read_bytes().startswith(prefix)
    assert (
        factory.local(result["dataset_root"]) / "data/point_in_time/market/000001.csv"
    ).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("fault", ["prefix", "oversize", "hardlink"])
def test_corrupt_or_linked_partial_never_reaches_sftp(tmp_path, fault):
    snapshot, _, control, factory = make_case(tmp_path)
    manifest, _, destination = initialize(snapshot, control)
    partial = destination / ".transfer/chunks/00000000.part"
    if fault == "hardlink":
        outside = tmp_path / "sentinel"
        outside.write_bytes(b"")
        os.link(outside, partial)
    else:
        partial.write_bytes(
            b"X"
            * (
                manifest["archive"]["chunks"][0]["bytes"] + 1
                if fault == "oversize"
                else 50
            )
        )
    original = partial.read_bytes()
    with pytest.raises(base.SnapshotError):
        run(snapshot, control, factory, resume_existing=True)
    assert not factory.opens and not factory.clients
    assert partial.read_bytes() == original
    assert all(name != "materialize" for name, _ in control.operations)


def test_prefix_change_between_status_and_guard_fails_before_write(tmp_path):
    snapshot, _, control, factory = make_case(tmp_path)
    _, _, destination = initialize(snapshot, control)
    partial = destination / ".transfer/chunks/00000000.part"
    partial.write_bytes((snapshot / "snapshot.tar.gz").read_bytes()[:10])
    factory.before_guard = lambda guard: partial.write_bytes(b"BAD-PREFIX")
    with pytest.raises(base.SnapshotError, match="prefix changed"):
        run(snapshot, control, factory, resume_existing=True)
    assert not factory.opens
    assert partial.read_bytes() == b"BAD-PREFIX"


@pytest.mark.parametrize("target", ["ancestor", "partial"])
def test_sftp_lstat_rejects_links_without_opening_target(tmp_path, target):
    snapshot, _, control, factory = make_case(tmp_path)
    _, _, destination = initialize(snapshot, control)
    remote = REMOTE + "/" + destination.relative_to(control.root).as_posix()
    suffix = (
        "/.transfer/chunks"
        if target == "ancestor"
        else "/.transfer/chunks/00000000.part"
    )
    factory.overrides[remote + suffix] = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
    with pytest.raises(base.SnapshotError):
        run(snapshot, control, factory, resume_existing=True, concurrency=1)
    assert not factory.opens
    assert factory.active == 0
    assert all(name not in {"chunk", "materialize"} for name, _ in control.operations)


@pytest.mark.parametrize("failure", ["worker", "timeout", "guard_exit"])
def test_failure_closes_writers_before_guard_release_and_preserves_partials(
    tmp_path, failure
):
    snapshot, _, control, factory = make_case(tmp_path, size=450000)
    barrier = threading.Barrier(4)
    visited = set()
    lock = threading.Lock()

    def block(output, content):
        with lock:
            first = output.path.name not in visited
            visited.add(output.path.name)
        if first:
            barrier.wait(timeout=3)
        if failure == "worker" and output.path.name == "00000000.part":
            output.stream.write(content[:100])
            raise OSError("deliberate disconnected writer")
        if failure == "guard_exit":
            factory.alive = False
        assert output.client.closed.wait(timeout=4)
        raise OSError("closed by owned batch cancellation")

    factory.on_write = block
    with pytest.raises((OSError, base.SnapshotError)):
        run(snapshot, control, factory, timeout=1 if failure == "timeout" else 10)
    assert factory.maximum == 4 and factory.active == 0
    assert all(client.closed.is_set() for client in factory.clients)
    assert factory.events[-1] == "guard_released"
    assert all(name not in {"chunk", "materialize"} for name, _ in control.operations)
    partials = list(control.root.glob("data/dataset_imports/*/.transfer/chunks/*.part"))
    assert len(partials) == 4
    if failure == "worker":
        assert any(path.stat().st_size == 100 for path in partials)


def test_mutated_local_archive_aborts_without_finalization(tmp_path):
    snapshot, _, control, factory = make_case(tmp_path)
    archive = snapshot / "snapshot.tar.gz"
    changed = threading.Event()

    def mutate(output, content):
        if not changed.is_set():
            changed.set()
            os.chmod(archive, 0o600)
            with archive.open("ab") as stream:
                stream.write(b"changed")

    factory.on_write = mutate
    with pytest.raises(base.SnapshotError, match="archive changed"):
        run(snapshot, control, factory)
    assert factory.active == 0
    assert all(name not in {"chunk", "materialize"} for name, _ in control.operations)


def test_failed_guard_hash_prevents_original_finalize(tmp_path):
    snapshot, _, control, factory = make_case(tmp_path)

    def corrupt(output, content):
        output.stream.write(b"X" * len(content))
        output.stream.seek(-len(content), 1)
        # The ordinary write then lands beyond EOF, causing a size/hash failure.
        output.stream.seek(len(content), 1)

    factory.on_write = corrupt
    with pytest.raises(base.SnapshotError):
        run(snapshot, control, factory)
    assert all(name not in {"chunk", "materialize"} for name, _ in control.operations)


@pytest.mark.parametrize(
    "project",
    [
        "/",
        "relative",
        "/srv/../other",
        "/srv//project",
        "/srv/project/",
        "/srv\\project",
    ],
)
def test_canonical_remote_root_required(project):
    with pytest.raises(base.SnapshotError):
        sftp_push.expected_destination(project, "a" * 64)


@pytest.mark.parametrize("concurrency", [0, 9, True])
def test_concurrency_bound_before_any_remote_calls(tmp_path, concurrency):
    snapshot, _, control, factory = make_case(tmp_path, size=10)
    with pytest.raises(base.SnapshotError, match="concurrency"):
        run(snapshot, control, factory, concurrency=concurrency)
    assert not control.operations


def test_guard_accepts_eight_distinct_rows_and_rejects_nine():
    rows = [
        {
            "index": index,
            "bytes": 1,
            "offset": 0,
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "prefix_sha256": hashlib.sha256(b"").hexdigest(),
        }
        for index in range(9)
    ]
    sftp_push.validate_rows(rows[:8])
    with pytest.raises(base.SnapshotError, match="one to eight"):
        sftp_push.validate_rows(rows)


@pytest.mark.parametrize("concurrency", [None, 8, 9])
def test_cli_parallelism_default_and_bounds(tmp_path, monkeypatch, concurrency):
    arguments = ["uploader", "--snapshot-dir", str(tmp_path)]
    if concurrency is not None:
        arguments.extend(["--concurrency", str(concurrency)])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(base, "SSHTransport", lambda *args: object())
    monkeypatch.setattr(sftp_push, "ParamikoFactory", lambda: object())
    received = []

    def capture(*args, **options):
        received.append(options["concurrency"])
        return {"status": "test-only"}

    monkeypatch.setattr(sftp_push, "push_snapshot_sftp", capture)
    if concurrency == 9:
        with pytest.raises(SystemExit) as error:
            sftp_push.main()
        assert error.value.code == 2
        assert not received
    else:
        sftp_push.main()
        assert received == [4 if concurrency is None else concurrency]


def test_unexpected_remote_destination_is_not_opened(tmp_path):
    snapshot, _, control, factory = make_case(tmp_path, size=10)
    call = control.call

    def wrong(operation, **params):
        result = call(operation, **params)
        result["destination"] = "/unrelated"
        return result

    control.call = wrong
    with pytest.raises(base.SnapshotError, match="unexpected path"):
        run(snapshot, control, factory)
    assert not factory.opens


def test_factory_strict_existing_host_keys_and_deploy_identity(tmp_path, monkeypatch):
    key, known = tmp_path / "deploy-key", tmp_path / ".ssh/known_hosts"
    key.write_text("fixture")
    known.parent.mkdir()
    known.write_text("fixture trusted hosts")
    deploy = SimpleNamespace(
        REMOTE_HOST="fixture.invalid",
        REMOTE_SSH_USER="deployer",
        REMOTE_DIR=REMOTE,
        _get_ssh_key=lambda: str(key),
    )
    monkeypatch.setitem(sys.modules, "ci_cd_deploy", deploy)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    events = []

    class RejectPolicy:
        pass

    class Client:
        def load_system_host_keys(self, filename):
            events.append(("known", filename))

        def set_missing_host_key_policy(self, policy):
            assert isinstance(policy, RejectPolicy)

        def connect(self, **params):
            events.append(("connect", params))

        def get_transport(self):
            return SimpleNamespace(
                set_keepalive=lambda seconds: None,
                sock=SimpleNamespace(setsockopt=lambda *args: None),
            )

        def close(self):
            events.append(("close",))

    monkeypatch.setitem(
        sys.modules,
        "paramiko",
        SimpleNamespace(SSHClient=Client, RejectPolicy=RejectPolicy),
    )
    factory = sftp_push.ParamikoFactory()
    factory.connect().close()
    assert events[0] == ("known", str(known))
    params = events[1][1]
    assert params["hostname"] == "fixture.invalid"
    assert params["key_filename"] == str(key)
    assert params["allow_agent"] is False and params["look_for_keys"] is False
    assert params["compress"] is True
    assert "password" not in params


def test_guard_uses_original_exclusive_lock(tmp_path):
    snapshot, _, control, _factory = make_case(tmp_path, size=10)
    manifest, digest, destination = initialize(snapshot, control)
    chunk = manifest["archive"]["chunks"][0]
    request = {
        "project_root": str(control.root),
        "manifest_sha256": digest,
        "rows": [
            {**chunk, "offset": 0, "prefix_sha256": hashlib.sha256(b"").hexdigest()}
        ],
    }
    failure = pytest.raises(base.SnapshotPaused, match="test abort")
    with failure, sftp_push.guarded_batch(request):
        with pytest.raises(OSError), base.remote_lock(destination):
            pytest.fail("existing operation lock must remain exclusive")
        raise base.SnapshotPaused("test abort")
    with base.remote_lock(destination):
        pass


def test_generated_guard_is_small_stdlib_only_and_compiles():
    program = sftp_push.guard_program()
    compile(program, "generated_guard", "exec")
    assert len(program.encode()) < 16000
    assert "validate_manifest" not in program and "load_import" not in program
    assert "fcntl.flock" in program


@pytest.mark.skipif(
    os.name == "nt", reason="coordinator stdin polling runs on deployment Linux"
)
def test_real_generated_guard_handshake_finish_and_hash_on_linux(tmp_path):
    snapshot, _, control, _factory = make_case(tmp_path, size=10)
    manifest, digest, destination = initialize(snapshot, control)
    chunk = manifest["archive"]["chunks"][0]
    request = {
        "project_root": str(control.root),
        "manifest_sha256": digest,
        "timeout_seconds": 10,
        "rows": [
            {**chunk, "offset": 0, "prefix_sha256": hashlib.sha256(b"").hexdigest()}
        ],
    }
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", sftp_push.guard_program()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdin.write(base.canonical(request) + b"\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["status"] == "locked"
        (destination / ".transfer/chunks/00000000.part").write_bytes(
            (snapshot / "snapshot.tar.gz").read_bytes()
        )
        process.stdin.write(b"finish\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["status"] == "verified"
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cli_exception_never_prints_credentials_or_ssh_command(
    tmp_path, monkeypatch, capsys
):
    marker = "private-host-secret-command"
    monkeypatch.setattr(sys, "argv", ["uploader", "--snapshot-dir", str(tmp_path)])
    monkeypatch.setattr(
        base, "SSHTransport", lambda *args: (_ for _ in ()).throw(OSError(marker))
    )
    with pytest.raises(SystemExit) as error:
        sftp_push.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert marker not in output.out and not output.err
    assert json.loads(output.out)["status"] == "failed"
