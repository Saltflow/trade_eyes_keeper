"""Offline snapshot integrity, extraction boundaries, and interrupted pulls."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import shlex
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def sync():
    path = Path(__file__).resolve().parents[1] / "scripts/sync_server_market_data.py"
    spec = importlib.util.spec_from_file_location("sync_market_data_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path, content=b"date,close\n2026-09-18,10\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _manifest_entry(name, content):
    return {
        "path": name,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _archive(path, rows):
    entries = []
    with tarfile.open(path, "w:gz") as archive:
        for name, content in rows:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
            entries.append(_manifest_entry(name, content))
    return {"contract": "server-market-snapshot-1", "files": entries}


def _symlink(link, target, *, directory=False):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"OS cannot create a test symlink: {error}")


def _export(sync, tmp_path, *, name="server", payload=None):
    root = tmp_path / name
    contents = payload if payload is not None else bytes(range(256)) * 20
    _write(root / "data/point_in_time/market/000001.csv", contents)
    job = tmp_path / f"{name}-job"
    status = sync.export_snapshot(root, job)
    return root, job, status


def _pull_args(tmp_path, *, attempts=1):
    return argparse.Namespace(
        output_dir=tmp_path / "download",
        remote_job_dir="/remote/snapshot",
        ssh_executable="ssh",
        host="offline-test.invalid",
        identity_file=tmp_path / "unused-key",
        known_hosts=tmp_path / "unused-known-hosts",
        export_timeout_seconds=30,
        transfer_timeout_seconds=30,
        attempts=attempts,
    )


def _mock_remote(sync, monkeypatch, active_job, *, interrupt_once=False):
    calls = []
    interrupted = False

    def read_remote(_args, filename):
        return (active_job[0] / filename).read_bytes()

    def transfer(command, *, stdout, stderr, timeout, check=False, **_options):
        nonlocal interrupted
        assert check is False
        words = shlex.split(command[-1])
        assert words[:2] == ["python3", "-c"]
        offset = int(words[-1])
        calls.append(offset)
        content = (active_job[0] / "snapshot.tar.gz").read_bytes()
        if interrupt_once and not interrupted:
            stdout.write(content[offset : len(content) // 2])
            interrupted = True
            return SimpleNamespace(returncode=255)
        stdout.write(content[offset:])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sync, "remote_read", read_remote)
    monkeypatch.setattr(sync.subprocess, "run", transfer)
    monkeypatch.setattr(sync.time, "sleep", lambda _seconds: None)
    return calls


def test_export_manifest_and_verified_extract_roundtrip_exclude_nondata(sync, tmp_path):
    root, job = tmp_path / "server", tmp_path / "export"
    included = {
        "data/reference_universe/index_constituents.json": b'{"companies": []}',
        "data/point_in_time/market/000001.csv": b"date,raw_close,qfq_close\n",
        "data/point_in_time/market/000001.actions.json": b"[]",
        "data/point_in_time/fundamentals/000001.statements.json": b"{}",
        "cache/data/000001.csv.meta": b'{"source": "local-test"}',
        "cache/announcement_content/notice.json": b'{"text": "notice"}',
        "cache/announcement_extraction/notice.json": b'{"cash": 1}',
        "data/000001_history.csv": b"date,close\n",
        "data/000001_history.csv.meta.json": b"{}",
    }
    excluded = [
        "config/config.yaml",
        "config/.env",
        "data/optimizer/run_summary.yaml",
        "data/restore/point_in_time/market/000001.csv",
        "data/email_archive/mail.json",
        "data/reference_universe/snapshot.tar.gz",
        "data/reference_universe/snapshot.tgz",
        "data/reference_universe/snapshot.zip",
        "data/point_in_time/market/000001.csv.part",
        "data/point_in_time/market/000001.csv.tmp",
        "data/point_in_time/.staging/pending.json",
        "cache/data/log.txt",
    ]
    for relative, content in included.items():
        _write(root / relative, content)
    for relative in excluded:
        _write(root / relative, b"excluded")
    outside = _write(tmp_path / "data/point_in_time/market/000001.csv", b"keep")

    status = sync.export_snapshot(root, job)
    manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))

    assert status["state"] == "complete"
    assert status["total_files"] == status["completed_files"] == len(included)
    assert status["source_bytes"] == sum(map(len, included.values()))
    assert status["sha256"] == sync.digest(job / "snapshot.tar.gz")
    assert status["manifest_sha256"] == sync.digest(job / "manifest.json")
    assert {row["path"] for row in manifest["files"]} == set(included)
    assert not (job / "export.lock").exists()
    assert not (job / "snapshot.tar.gz.part").exists()
    destination = tmp_path / "local/dataset"
    sync.extract_verified(job / "snapshot.tar.gz", manifest, destination)
    assert outside.read_bytes() == b"keep"
    for relative, content in included.items():
        assert (destination / relative).read_bytes() == content


def test_export_skips_file_symlinks(sync, tmp_path):
    root = tmp_path / "server"
    real = _write(root / "cache/data/000001.csv")
    secret = _write(tmp_path / "outside/private.json", b"private")
    _symlink(root / "cache/data/linked.json", secret)

    assert sync.source_files(root) == [real]


def test_export_does_not_traverse_symlink_at_allowed_directory_root(sync, tmp_path):
    root = tmp_path / "server"
    outside = _write(tmp_path / "outside/private.json", b"private").parent
    _symlink(root / "cache/data", outside, directory=True)

    assert sync.source_files(root) == []


def test_completed_export_is_reused_without_rereading_sources(
    sync, tmp_path, monkeypatch
):
    root, job, original = _export(sync, tmp_path)
    archive_time = (job / "snapshot.tar.gz").stat().st_mtime_ns

    def unexpected_read(_root):
        raise AssertionError("a valid completed export must be reused")

    monkeypatch.setattr(sync, "source_files", unexpected_read)
    assert sync.export_snapshot(root, job) == original
    assert (job / "snapshot.tar.gz").stat().st_mtime_ns == archive_time


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_completed_export_rebuilds_when_manifest_is_unusable(sync, tmp_path, damage):
    root, job, _ = _export(sync, tmp_path)
    manifest_path = job / "manifest.json"
    if damage == "missing":
        manifest_path.unlink()
    else:
        manifest_path.write_text('{"files": []}', encoding="utf-8")

    status = sync.export_snapshot(root, job)

    assert manifest_path.exists()
    assert sync.digest(manifest_path) == status["manifest_sha256"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sync.extract_verified(job / "snapshot.tar.gz", manifest, tmp_path / "dataset")


@pytest.mark.parametrize(
    "member_name",
    [
        "../outside.csv",
        "data/../../outside.csv",
        "/outside.csv",
        "C:/outside.csv",
        r"..\outside.csv",
    ],
)
def test_extract_rejects_escape_paths_without_overwriting_outside(
    sync, tmp_path, member_name
):
    sentinel = _write(tmp_path / "outside.csv", b"keep")
    archive = tmp_path / "snapshot.tar.gz"
    manifest = _archive(archive, [(member_name, b"replace")])

    with pytest.raises(ValueError):
        sync.extract_verified(archive, manifest, tmp_path / "dataset")

    assert sentinel.read_bytes() == b"keep"


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_extract_rejects_archive_symlinks_and_hardlinks(sync, tmp_path, member_type):
    archive_path = tmp_path / "snapshot.tar.gz"
    name = "data/point_in_time/market/linked.csv"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = member_type, "../../outside.csv"
        archive.addfile(member)
    manifest = {"files": [_manifest_entry(name, b"")]}

    with pytest.raises(ValueError):
        sync.extract_verified(archive_path, manifest, tmp_path / "dataset")


@pytest.mark.parametrize("duplicate_kind", ["manifest", "archive"])
def test_extract_rejects_duplicate_entries(sync, tmp_path, duplicate_kind):
    archive = tmp_path / "snapshot.tar.gz"
    row = ("data/point_in_time/market/000001.csv", b"payload")
    manifest = _archive(archive, [row] * (2 if duplicate_kind == "archive" else 1))
    if duplicate_kind == "archive":
        manifest["files"] = manifest["files"][:1]
    else:
        manifest["files"].append(dict(manifest["files"][0]))

    with pytest.raises(ValueError):
        sync.extract_verified(archive, manifest, tmp_path / "dataset")


@pytest.mark.parametrize("alias", ["data//file.csv", "data/./file.csv"])
def test_extract_rejects_distinct_names_for_same_target(sync, tmp_path, alias):
    archive = tmp_path / "snapshot.tar.gz"
    manifest = _archive(archive, [("data/file.csv", b"first"), (alias, b"second")])

    with pytest.raises(ValueError):
        sync.extract_verified(archive, manifest, tmp_path / "dataset")


@pytest.mark.parametrize("failure", ["checksum", "size", "missing", "unexpected"])
def test_extract_rejects_corrupt_missing_and_unexpected_files(sync, tmp_path, failure):
    archive = tmp_path / "snapshot.tar.gz"
    manifest = _archive(archive, [("data/file.csv", b"payload")])
    if failure == "checksum":
        manifest["files"][0]["sha256"] = "0" * 64
    elif failure == "size":
        manifest["files"][0]["bytes"] += 1
    elif failure == "missing":
        manifest["files"].append(_manifest_entry("data/absent.csv", b"missing"))
    else:
        manifest["files"] = []

    with pytest.raises(ValueError):
        sync.extract_verified(archive, manifest, tmp_path / "dataset")


@pytest.mark.parametrize("link_location", ["destination", "parent", "temporary"])
def test_existing_filesystem_symlinks_cannot_overwrite_outside_dataset(
    sync, tmp_path, link_location
):
    outside = tmp_path / "outside"
    sentinel = _write(outside / "file.csv", b"keep")
    destination = tmp_path / "dataset"
    if link_location == "destination":
        _symlink(destination, outside, directory=True)
        name = "file.csv"
    elif link_location == "parent":
        _symlink(destination / "data", outside, directory=True)
        name = "data/file.csv"
    else:
        destination.mkdir()
        _symlink(destination / f".file.csv.{sync.os.getpid()}.tmp", sentinel)
        name = "file.csv"
    archive = tmp_path / "snapshot.tar.gz"
    manifest = _archive(archive, [(name, b"replacement")])

    try:
        sync.extract_verified(archive, manifest, destination)
    except (ValueError, OSError):
        pass

    assert sentinel.read_bytes() == b"keep"


def test_pull_resumes_same_snapshot_after_an_interrupted_process(
    sync, tmp_path, monkeypatch
):
    root, job, status = _export(sync, tmp_path)
    calls = _mock_remote(sync, monkeypatch, [job], interrupt_once=True)
    args = _pull_args(tmp_path)
    with pytest.raises(RuntimeError, match="retries exhausted"):
        sync.pull_snapshot(args)
    partial_size = (args.output_dir / "snapshot.tar.gz.part").stat().st_size
    assert 0 < partial_size < status["archive_bytes"]

    result = sync.pull_snapshot(args)

    assert result["state"] == "complete"
    assert calls == [0, partial_size]
    assert not (args.output_dir / "snapshot.tar.gz.part").exists()
    relative = "data/point_in_time/market/000001.csv"
    assert (args.output_dir / "dataset" / relative).read_bytes() == (
        root / relative
    ).read_bytes()


def test_existing_temporary_hardlink_cannot_overwrite_outside_dataset(sync, tmp_path):
    sentinel = _write(tmp_path / "outside/file.csv", b"keep")
    destination = tmp_path / "dataset"
    destination.mkdir()
    temporary = destination / f".file.csv.{sync.os.getpid()}.tmp"
    try:
        temporary.hardlink_to(sentinel)
    except OSError as error:
        pytest.skip(f"OS cannot create a test hardlink: {error}")
    archive = tmp_path / "snapshot.tar.gz"
    manifest = _archive(archive, [("file.csv", b"replacement")])

    try:
        sync.extract_verified(archive, manifest, destination)
    except (ValueError, OSError):
        pass

    assert sentinel.read_bytes() == b"keep"


def test_pull_does_not_reuse_partial_prefix_from_different_snapshot(
    sync, tmp_path, monkeypatch
):
    _, old_job, _ = _export(sync, tmp_path, name="old")
    new_root, new_job, _ = _export(
        sync, tmp_path, name="new", payload=bytes(reversed(range(256))) * 20
    )
    active_job = [old_job]
    calls = _mock_remote(sync, monkeypatch, active_job, interrupt_once=True)
    args = _pull_args(tmp_path)
    with pytest.raises(RuntimeError, match="retries exhausted"):
        sync.pull_snapshot(args)
    active_job[0] = new_job

    result = sync.pull_snapshot(args)

    assert result["state"] == "complete"
    assert calls == [0, 0]
    relative = "data/point_in_time/market/000001.csv"
    assert (args.output_dir / "dataset" / relative).read_bytes() == (
        new_root / relative
    ).read_bytes()


def test_pull_retains_oversized_partial_and_restarts_from_zero(
    sync, tmp_path, monkeypatch
):
    _, job, _ = _export(sync, tmp_path)
    calls = _mock_remote(sync, monkeypatch, [job], interrupt_once=True)
    args = _pull_args(tmp_path)
    with pytest.raises(RuntimeError, match="retries exhausted"):
        sync.pull_snapshot(args)
    partial = args.output_dir / "snapshot.tar.gz.part"
    oversized = (job / "snapshot.tar.gz").read_bytes() + b"unexpected extra data"
    partial.write_bytes(oversized)

    result = sync.pull_snapshot(args)

    assert result["state"] == "complete"
    assert calls == [0, 0]
    retained = list(args.output_dir.glob("snapshot.oversized.*.part"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == oversized


def test_pull_can_retry_from_zero_when_retained_partial_is_corrupt(
    sync, tmp_path, monkeypatch
):
    _, job, _ = _export(sync, tmp_path)
    calls = _mock_remote(sync, monkeypatch, [job], interrupt_once=True)
    args = _pull_args(tmp_path)
    with pytest.raises(RuntimeError, match="retries exhausted"):
        sync.pull_snapshot(args)
    partial = args.output_dir / "snapshot.tar.gz.part"
    content = bytearray(partial.read_bytes())
    content[len(content) // 2] ^= 1
    partial.write_bytes(content)
    args.attempts = 2

    result = sync.pull_snapshot(args)

    assert result["state"] == "complete"
    assert calls[-1] == 0
    assert sync.digest(args.output_dir / "snapshot.tar.gz") == result["sha256"]


def test_pull_reuses_verified_archive_and_repairs_only_its_dataset(
    sync, tmp_path, monkeypatch
):
    root, job, _ = _export(sync, tmp_path)
    calls = _mock_remote(sync, monkeypatch, [job])
    args = _pull_args(tmp_path)
    sync.pull_snapshot(args)
    relative = "data/point_in_time/market/000001.csv"
    _write(args.output_dir / "dataset" / relative, b"damaged local file")
    sentinel = _write(tmp_path / relative, b"outside must remain")

    result = sync.pull_snapshot(args)

    assert result["state"] == "complete"
    assert calls == [0]
    assert sentinel.read_bytes() == b"outside must remain"
    assert (args.output_dir / "dataset" / relative).read_bytes() == (
        root / relative
    ).read_bytes()


def test_pull_rejects_manifest_tampering_before_transfer(sync, tmp_path, monkeypatch):
    _, job, _ = _export(sync, tmp_path)
    calls = _mock_remote(sync, monkeypatch, [job])
    (job / "manifest.json").write_text('{"files": []}', encoding="utf-8")
    args = _pull_args(tmp_path)

    with pytest.raises(ValueError, match="manifest checksum mismatch"):
        sync.pull_snapshot(args)

    assert not calls
    assert not (args.output_dir / "dataset").exists()
