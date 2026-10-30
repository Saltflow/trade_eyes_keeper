"""Offline verification of scoped uploads, stable snapshots, and resumption."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def push(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/push_dataset_snapshot.py"
    spec = importlib.util.spec_from_file_location("push_dataset_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "push_dataset_test", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "active_writers", lambda root: [])
    return module


def write(path, content=b"date,raw_close\n2026-09-28,10\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def prepare(push, tmp_path, *, duplicate=False, content=None):
    root = tmp_path / "source"
    payload = content if content is not None else b"date,raw_close\n2026-09-28,10\n"
    source = write(root / "data/point_in_time/market/000001.csv", payload)
    if duplicate:
        write(
            root
            / "data/server_imports/old/dataset/data/point_in_time/market/000001.csv",
            payload,
        )
    snapshot = root / "temp_test_cache/snapshot"
    summary = push.prepare_snapshot(root, snapshot, chunk_bytes=65536)
    return root, source, snapshot, summary


class LocalTransport:
    def __init__(self, push, project):
        self.push = push
        self.project = project.resolve()
        (project / "data").mkdir(parents=True)
        self.operations = []

    def call(self, operation, **parameters):
        self.operations.append(operation)
        content = parameters.pop("content", b"")
        return self.push.remote_dispatch(
            {"project_root": str(self.project), "operation": operation, **parameters},
            io.BytesIO(content),
        )


def initialized(push, tmp_path, *, content=None):
    _, _, snapshot, summary = prepare(push, tmp_path, content=content)
    manifest = push.load_snapshot(snapshot)
    transport = LocalTransport(push, tmp_path / "server")
    result = transport.call(
        "initialize", manifest_sha256=summary["manifest_sha256"], manifest=manifest
    )
    return snapshot, manifest, transport, Path(result["destination"])


def test_roundtrip_deduplicates_transfer_preserves_ordinary_sources(push, tmp_path):
    _, source, snapshot, summary = prepare(push, tmp_path, duplicate=True)
    assert summary["files"] == 2
    assert summary["unique_objects"] == 1
    transport = LocalTransport(push, tmp_path / "server")
    sentinel = write(transport.project / "data/point_in_time/original.csv", b"preserve")
    receipt = push.push_snapshot(snapshot, transport)
    manifest = push.load_snapshot(snapshot)
    assert receipt["status"] == "complete"
    assert receipt["verified_files"] == 2
    for row in manifest["files"]:
        copied = Path(receipt["dataset_root"]) / row["path"]
        assert copied.read_bytes() == source.read_bytes()
        assert copied.stat().st_nlink == 1
        assert push.sha_file(copied) == row["sha256"]
    assert sentinel.read_bytes() == b"preserve"
    assert not (transport.project / "config").exists()
    assert push.push_snapshot(snapshot, transport) == receipt


def test_shared_policy_excludes_nested_configs_source_and_runtime(push, tmp_path):
    root = tmp_path / "source"
    write(root / "data/point_in_time/market/000001.csv")
    for relative in (
        "data/analysis/inspect.py",
        "data/analysis/acquisition_config.yaml",
        "data/server_imports/old/dataset/config/config.json",
        "data/server_imports/old/dataset/data/point_in_time/provider_state/state.json",
        "data/reference_universe/batch/ref_portfolio.json",
        "data/reference_universe/batch/logs/run.txt",
    ):
        write(root / relative, b"not business data")
    snapshot = root / "temp_test_cache/eligible"
    summary = push.prepare_snapshot(root, snapshot)
    assert summary["files"] == 1
    assert sum(summary["excluded_counts"].values()) == 6


@pytest.mark.parametrize("body", [b'{"api_key":"private-value"}', b'{"token":"x"}'])
def test_sensitive_content_pauses_without_exporting_value(push, tmp_path, body):
    root = tmp_path / "source"
    write(root / "data/point_in_time/market/extra.json", body)
    snapshot = root / "temp_test_cache/blocked"
    with pytest.raises(push.SnapshotPaused) as failure:
        push.prepare_snapshot(root, snapshot)
    assert "private-value" not in str(failure.value)
    assert not (snapshot / "manifest.json").exists()


def test_public_source_url_and_empty_credential_are_not_secrets(push, tmp_path):
    content = b'{"source_url":"https://example.org/annual.pdf","api_key":""}'
    _, _, snapshot, _ = prepare(push, tmp_path, content=content)
    assert push.load_snapshot(snapshot)["files"][0]["bytes"] == len(content)


def test_active_source_writer_pauses_before_freeze(push, tmp_path, monkeypatch):
    root = tmp_path / "source"
    write(root / "data/point_in_time/market/a.csv")
    monkeypatch.setattr(
        push,
        "active_writers",
        lambda root: [{"pid": 100, "script": "backfill_point_in_time.py"}],
    )
    with pytest.raises(push.SnapshotPaused, match="active project writers"):
        push.prepare_snapshot(root, root / "temp_test_cache/frozen")
    assert not (root / "temp_test_cache/frozen").exists()


@pytest.mark.parametrize("change", ["bytes", "membership"])
def test_source_changes_pause_whole_snapshot(push, tmp_path, monkeypatch, change):
    root = tmp_path / "source"
    source = write(root / "data/point_in_time/market/a.csv")
    original = push.freeze_file

    def mutating(*args):
        result = original(*args)
        if change == "bytes":
            source.write_bytes(b"changed after freezing")
        else:
            write(source.with_name("added.csv"))
        return result

    monkeypatch.setattr(push, "freeze_file", mutating)
    snapshot = root / "temp_test_cache/unstable"
    with pytest.raises(push.SnapshotPaused, match="changed"):
        push.prepare_snapshot(root, snapshot)
    assert not (snapshot / "manifest.json").exists()


def test_source_change_during_archive_is_detected(push, tmp_path, monkeypatch):
    root = tmp_path / "source"
    source = write(root / "data/point_in_time/market/a.csv")
    original = push.sha_file

    def mutating(path):
        result = original(path)
        if path.name == "snapshot.tar.gz":
            source.write_bytes(b"written while archive compressed")
        return result

    monkeypatch.setattr(push, "sha_file", mutating)
    snapshot = root / "temp_test_cache/unstable"
    with pytest.raises(push.SnapshotPaused, match="changed"):
        push.prepare_snapshot(root, snapshot)
    assert not (snapshot / "manifest.json").exists()


def test_inventory_read_error_is_not_silently_skipped(push, tmp_path, monkeypatch):
    from src.data import dataset_catalog

    monkeypatch.setattr(
        dataset_catalog,
        "iter_catalog_entries",
        lambda root: iter(
            [
                {
                    "relative_path": "data/analysis/a.json",
                    "eligible": False,
                    "reason": "content_check_error:PermissionError",
                }
            ]
        ),
    )
    with pytest.raises(push.SnapshotPaused, match="requires review"):
        push.source_entries(tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "../escape.csv",
        "/escape.csv",
        "data//bad.csv",
        "data/./bad.csv",
        "data\\bad.csv",
        "data/bad:stream.csv",
        "data/analysis/acquisition_config.json",
        "data/server_imports/a/dataset/config/snapshot.json",
        "data/analysis/source.py",
    ],
)
def test_manifest_rejects_unsafe_or_disallowed_paths(push, tmp_path, name):
    _, _, snapshot, _ = prepare(push, tmp_path)
    manifest = push.load_snapshot(snapshot)
    manifest["files"][0]["path"] = name
    with pytest.raises(push.SnapshotError):
        push.validate_manifest(manifest)


@pytest.mark.parametrize("fault", ["duplicate", "total", "chunk_offset", "object_sha"])
def test_manifest_integrity_contract(push, tmp_path, fault):
    _, _, snapshot, _ = prepare(push, tmp_path)
    manifest = push.load_snapshot(snapshot)
    if fault == "duplicate":
        manifest["files"].append(copy.deepcopy(manifest["files"][0]))
    elif fault == "total":
        manifest["logical_bytes"] += 1
    elif fault == "chunk_offset":
        manifest["archive"]["chunks"][0]["offset"] += 1
    else:
        manifest["objects"][0]["sha256"] = "invalid"
    with pytest.raises(push.SnapshotError):
        push.validate_manifest(manifest)


def test_interrupted_chunk_resumes_verified_prefix(push, tmp_path):
    snapshot, manifest, transport, destination = initialized(
        push, tmp_path, content=os.urandom(130000)
    )
    row = manifest["archive"]["chunks"][0]
    content = (snapshot / "snapshot.tar.gz").read_bytes()[: row["bytes"]]
    digest = push.snapshot_summary(manifest)["manifest_sha256"]
    with pytest.raises(push.SnapshotPaused, match="partial chunk retained"):
        transport.call(
            "chunk",
            manifest_sha256=digest,
            index=0,
            offset=0,
            prefix_sha256=hashlib.sha256(b"").hexdigest(),
            content=content[:100],
        )
    state = transport.call("status", manifest_sha256=digest)["chunks"][0]
    assert state["offset"] == 100
    assert state["prefix_sha256"] == hashlib.sha256(content[:100]).hexdigest()
    receipt = push.push_snapshot(snapshot, transport)
    assert receipt["status"] == "complete"
    assert not (destination / ".transfer/chunks/00000000.part").exists()


def test_chunk_prefix_and_content_corruption_are_refused(push, tmp_path):
    _, manifest, transport, destination = initialized(push, tmp_path)
    digest = push.snapshot_summary(manifest)["manifest_sha256"]
    with pytest.raises(push.SnapshotError, match="prefix changed"):
        transport.call(
            "chunk",
            manifest_sha256=digest,
            index=0,
            offset=0,
            prefix_sha256="0" * 64,
            content=b"",
        )
    row = manifest["archive"]["chunks"][0]
    with pytest.raises(push.SnapshotError, match="SHA-256 mismatch"):
        transport.call(
            "chunk",
            manifest_sha256=digest,
            index=0,
            offset=0,
            prefix_sha256=hashlib.sha256(b"").hexdigest(),
            content=b"x" * row["bytes"],
        )
    assert not (destination / "verification.json").exists()


def test_existing_different_file_is_never_overwritten(push, tmp_path):
    snapshot, manifest, transport, destination = initialized(push, tmp_path)
    original = write(
        destination / "dataset" / manifest["files"][0]["path"], b"preserve existing"
    )
    with pytest.raises(push.SnapshotError, match="refusing overwrite"):
        push.push_snapshot(snapshot, transport)
    assert original.read_bytes() == b"preserve existing"


@pytest.mark.parametrize("kind", ["symlink", "unexpected", "duplicate", "missing"])
def test_archive_members_cannot_escape_or_omit_inventory(push, tmp_path, kind):
    _, manifest, _, destination = initialized(push, tmp_path)
    archive = tmp_path / "malicious.tar.gz"
    row = manifest["objects"][0]
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("objects/" + row["sha256"])
        data = b"date,raw_close\n2026-09-28,10\n"
        info.size = len(data)
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
            bundle.addfile(info)
        elif kind == "unexpected":
            info.name = "../../outside"
            bundle.addfile(info, io.BytesIO(data))
        elif kind == "duplicate":
            bundle.addfile(info, io.BytesIO(data))
            bundle.addfile(info, io.BytesIO(data))
    with pytest.raises(push.SnapshotError):
        push.extract_objects(destination, manifest, archive)
    assert not (tmp_path / "outside").exists()


def test_hardlinked_chunk_refused(push, tmp_path):
    _, manifest, _, destination = initialized(push, tmp_path)
    outside = write(tmp_path / "original", b"preserve")
    linked = destination / ".transfer/chunks/00000000.part"
    try:
        os.link(outside, linked)
    except OSError as error:
        pytest.skip(f"hardlinks unavailable: {error}")
    with pytest.raises(push.SnapshotError, match="independent"):
        push.chunk_status(destination, manifest)
    assert outside.read_bytes() == b"preserve"


def test_symlink_ancestor_outside_base_is_rejected(push, tmp_path):
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(push.SnapshotError, match="symlink"):
        push.safe_path(link / "nested", "file.csv")


def test_space_guard_refuses_new_import(push, tmp_path, monkeypatch):
    _, _, snapshot, summary = prepare(push, tmp_path)
    transport = LocalTransport(push, tmp_path / "server")
    monkeypatch.setattr(push.shutil, "disk_usage", lambda root: SimpleNamespace(free=0))
    with pytest.raises(push.SnapshotPaused, match="insufficient remote space"):
        transport.call(
            "initialize",
            manifest_sha256=summary["manifest_sha256"],
            manifest=push.load_snapshot(snapshot),
        )
    assert not (transport.project / "data/dataset_imports").exists()


def test_generated_worker_runs_without_project_imports_and_preflight_is_read_only(
    push, tmp_path
):
    project = tmp_path / "remote"
    (project / "data").mkdir(parents=True)
    encoded = base64.b64encode(zlib.compress(push.remote_program().encode())).decode()
    command = (
        "import base64,zlib;exec(zlib.decompress(base64.b64decode("
        + repr(encoded)
        + ")))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", command],
        input=push.canonical(
            {"project_root": str(project.resolve()), "operation": "preflight"}
        )
        + b"\n",
        capture_output=True,
        check=True,
    )
    response = json.loads(completed.stdout)
    assert response["ok"]
    assert response["result"]["read_only"]
    assert not (project / "data/dataset_imports").exists()


def test_frozen_archive_tampering_blocks_any_remote_call(push, tmp_path):
    _, _, snapshot, _ = prepare(push, tmp_path)
    with (snapshot / "snapshot.tar.gz").open("ab") as stream:
        stream.write(b"tampered")
    transport = LocalTransport(push, tmp_path / "server")
    with pytest.raises(push.SnapshotError, match="archive changed"):
        push.push_snapshot(snapshot, transport)
    assert not transport.operations


def test_generated_worker_verifies_full_manifest_and_materializes(push, tmp_path):
    _, _, snapshot, summary = prepare(push, tmp_path)
    manifest = push.load_snapshot(snapshot)
    project = tmp_path / "remote"
    (project / "data").mkdir(parents=True)
    encoded = base64.b64encode(zlib.compress(push.remote_program().encode())).decode()
    command = (
        "import base64,zlib;exec(zlib.decompress(base64.b64decode("
        + repr(encoded)
        + ")))"
    )

    def call(operation, content=b"", **parameters):
        request = {
            "project_root": str(project.resolve()),
            "operation": operation,
            "manifest_sha256": summary["manifest_sha256"],
            **parameters,
        }
        result = subprocess.run(
            [sys.executable, "-I", "-c", command],
            input=push.canonical(request) + b"\n" + content,
            capture_output=True,
            check=True,
        )
        response = json.loads(result.stdout)
        assert response["ok"], response
        return response["result"]

    call("initialize", manifest=manifest)
    call(
        "chunk",
        content=(snapshot / "snapshot.tar.gz").read_bytes(),
        index=0,
        offset=0,
        prefix_sha256=hashlib.sha256(b"").hexdigest(),
    )
    result = call("materialize")
    assert result["verified_files"] == 1
    row = manifest["files"][0]
    assert push.sha_file(Path(result["dataset_root"]) / row["path"]) == row["sha256"]


def test_matching_archive_hash_does_not_hide_wrong_member_bytes(push, tmp_path):
    _, _, snapshot, _ = prepare(push, tmp_path)
    manifest = push.load_snapshot(snapshot)
    row = manifest["objects"][0]
    archive = snapshot / "snapshot.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("objects/" + row["sha256"])
        info.size = row["bytes"]
        bundle.addfile(info, io.BytesIO(b"x" * row["bytes"]))
    content = archive.read_bytes()
    manifest["archive"] = {
        "bytes": len(content),
        "sha256": push.sha_file(archive),
        "chunks": [
            {
                "index": 0,
                "offset": 0,
                "bytes": len(content),
                "sha256": push.sha_file(archive),
            }
        ],
    }
    (snapshot / "manifest.json").write_bytes(push.canonical(manifest))
    transport = LocalTransport(push, tmp_path / "server")
    with pytest.raises(push.SnapshotError, match="member SHA-256"):
        push.push_snapshot(snapshot, transport)
    assert not transport.operations


@pytest.mark.parametrize("executable", ["ssh", "C:/Program Files/Git/usr/bin/ssh.exe"])
def test_ssh_compression_preserves_guards_payload_and_finite_timeout(
    push, tmp_path, monkeypatch, executable
):
    key = write(tmp_path / "fixture-key", b"not a real credential")
    deploy = SimpleNamespace(
        REMOTE_HOST="offline-test.invalid",
        REMOTE_SSH_USER="fixture-user",
        REMOTE_DIR="/fixture/project",
        _get_ssh_key=lambda: str(key),
    )
    monkeypatch.setitem(sys.modules, "ci_cd_deploy", deploy)
    monkeypatch.setattr(push, "remote_program", lambda: "print('fixture worker')")
    calls = []

    def run(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"ok":true,"result":{"accepted":true}}',
        )

    monkeypatch.setattr(push.subprocess, "run", run)
    transport = push.SSHTransport(timeout=1200, ssh_executable=executable)
    payload = b"\x00\xffexact binary payload\n"
    assert transport.call("chunk", index=3, content=payload) == {"accepted": True}
    arguments, options = calls[0]
    assert arguments[:3] == [executable, "-C", "-T"]
    assert arguments.count("-C") == 1
    for value in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ConnectTimeout=15",
        "ServerAliveInterval=10",
        "ServerAliveCountMax=3",
    ):
        assert value in arguments
    assert "fixture-user@offline-test.invalid" in arguments
    assert options["timeout"] == 1200
    assert options["capture_output"] is True
    assert options["check"] is False
    assert not options.get("shell")
    header, body = options["input"].split(b"\n", 1)
    assert json.loads(header) == {
        "project_root": "/fixture/project",
        "operation": "chunk",
        "index": 3,
    }
    assert body == payload


def test_preflight_cli_passes_explicit_ssh_path_without_shell(
    push, tmp_path, monkeypatch
):
    executable = "C:/Program Files/Git/usr/bin/ssh.exe"
    seen = []

    def transport(timeout, ssh_executable):
        seen.append((timeout, ssh_executable))
        return SimpleNamespace(call=lambda operation: {"read_only": True})

    monkeypatch.setattr(push, "SSHTransport", transport)
    monkeypatch.setattr(push, "load_snapshot", lambda snapshot: {})
    monkeypatch.setattr(push, "snapshot_summary", lambda manifest: {"files": 1})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "push_dataset_snapshot.py",
            "preflight",
            "--snapshot-dir",
            str(tmp_path),
            "--timeout-seconds",
            "1200",
            "--ssh-executable",
            executable,
        ],
    )
    push.main()
    assert seen == [(1200, executable)]


@pytest.mark.parametrize("kind", ["timeout", "os_error", "snapshot"])
def test_cli_failure_is_nonzero_json_without_ssh_command(
    push, tmp_path, monkeypatch, capsys, kind
):
    exposed = "DO-NOT-PRINT-KEYPATH-HOST-ENCODED-COMMAND"
    if kind == "timeout":
        failure = subprocess.TimeoutExpired(["ssh", "-i", exposed], timeout=600)
    elif kind == "os_error":
        failure = OSError(5, exposed)
    else:
        failure = push.SnapshotError("snapshot SHA-256 mismatch")

    def fail(*args):
        raise failure

    monkeypatch.setattr(push, "SSHTransport", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["push_dataset_snapshot.py", "push", "--snapshot-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as stopped:
        push.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert not captured.err
    assert exposed not in captured.out
    assert "Traceback" not in captured.out
    response = json.loads(captured.out)
    assert response["status"] == "failed"
    assert response["error_type"] == type(failure).__name__
