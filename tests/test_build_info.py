"""Release provenance is checked once, without Git, providers or runtime state."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.core import build_info as builds


@pytest.fixture(autouse=True)
def isolated_cache():
    builds._snapshot.cache_clear()
    yield
    builds._snapshot.cache_clear()


@pytest.fixture
def source(tmp_path):
    (tmp_path / "src/templates").mkdir(parents=True)
    (tmp_path / "main.py").write_text("# application\n", "utf-8")
    (tmp_path / "src/feature.py").write_text("# feature\n", "utf-8")
    (tmp_path / "src/templates/report.html").write_text("<p>template</p>", "utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "1.18.0"\n[tool.fixture]\nversion = "other"\n', "utf-8"
    )
    return tmp_path


def publish(source):
    return builds.write_release_manifest(source, "a" * 40)


def test_deployment_records_actual_version_and_overlay_not_git_head(source):
    document = publish(source)
    info = builds.get_build_info(source)
    assert info["verified"] and info["version"] == "1.18.0"
    assert info["source_commit"] == "a" * 40
    assert info["source_sha256"] == document["source_sha256"]
    assert info["deployment_id"] and info["deployed_at"]
    assert "files" not in info
    assert builds.build_label(info).startswith("v1.18.0 · aaaaaaa · 构建 ")


def test_same_sources_have_same_bundle_and_separate_deployment_ids(source, monkeypatch):
    class FixedDateTime(builds.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 12, 0, tzinfo=tz)

    monkeypatch.setattr(builds, "datetime", FixedDateTime)
    first = publish(source)
    second = publish(source)
    assert first["source_sha256"] == second["source_sha256"]
    assert first["deployment_id"] != second["deployment_id"]
    (source / "src/feature.py").write_text("# new feature\n", "utf-8")
    assert publish(source)["source_sha256"] != first["source_sha256"]


@pytest.mark.parametrize("change", ["modify", "add", "delete", "template", "project"])
def test_old_manifest_never_verifies_a_different_source_set(source, change):
    publish(source)
    if change == "modify":
        (source / "src/feature.py").write_text("# changed\n", "utf-8")
    elif change == "add":
        (source / "src/new.py").write_text("# extra\n", "utf-8")
    elif change == "delete":
        (source / "src/feature.py").unlink()
    elif change == "template":
        (source / "src/templates/report.html").write_text("changed", "utf-8")
    else:
        (source / "pyproject.toml").write_text('[project]\nversion="1.18.1"', "utf-8")
    info = builds.get_build_info(source)
    assert not info["verified"] and info["state"] in {"mismatch", "invalid"}
    assert "aaaaaaa" not in builds.build_label(info)


def test_unregistered_source_does_not_infer_a_published_git_version(source):
    (source / ".git").mkdir()
    (source / ".git/HEAD").write_text("ref: refs/heads/unpublished", "utf-8")
    info = builds.get_build_info(source)
    assert info["state"] == "unregistered"
    assert builds.build_label(info) == "v1.18.0 · 未登记发布"


@pytest.mark.parametrize(
    "text",
    ["{", "[]", '{"schema_version":1,"schema_version":1}', '{"schema_version":0}'],
)
def test_bad_metadata_is_visible_but_does_not_break_a_report(source, text):
    path = source / builds.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text, "utf-8")
    info = builds.get_build_info(source)
    assert info["state"] == "invalid" and not info["verified"]
    assert "版本信息异常" in builds.build_label(info)


def test_manifest_path_escape_is_not_opened(source):
    document = publish(source)
    document["files"]["../../config/.env"] = "0" * 64
    document["source_sha256"] = builds._digest(document["files"])
    (source / builds.MANIFEST_PATH).write_text(json.dumps(document), "utf-8")
    assert builds.get_build_info(source)["state"] == "mismatch"


def test_snapshot_is_frozen_for_loaded_process_and_summary_is_not_mutable(source):
    publish(source)
    first = builds.get_build_info(source)
    first["version"] = "tampered caller"
    (source / "src/feature.py").write_text("# next deployment", "utf-8")
    assert builds.get_build_info(source)["version"] == "1.18.0"
    assert builds.get_build_info(source)["verified"]
    builds._snapshot.cache_clear()  # A new application's startup.
    assert not builds.get_build_info(source)["verified"]


def test_concurrent_readers_hash_once(source, monkeypatch):
    publish(source)
    original = builds._source_hashes
    calls = []

    def inspect(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(builds, "_source_hashes", inspect)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: builds.get_build_info(source), range(12)))
    assert all(value["verified"] for value in values)
    assert calls == [source]


def test_runtime_state_and_credentials_are_outside_bundle(source, monkeypatch):
    before = publish(source)
    (source / "config").mkdir()
    (source / "config/.env").write_text("NOT_A_REAL_KEY=fixture", "utf-8")
    (source / "data/runtime").mkdir()
    (source / "data/runtime/probe.sqlite3").write_bytes(b"fixture")
    original = Path.read_bytes

    def guarded(path):
        assert not path.is_relative_to(source / "config")
        assert not path.is_relative_to(source / "data")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    assert publish(source)["source_sha256"] == before["source_sha256"]


@pytest.mark.parametrize(
    "relative",
    [
        "config/__init__.py",
        "requirements.txt",
        "tools/feishu-pi/bridge.mjs",
        "tools/feishu-pi/shared-runtime.mjs",
        "tools/feishu-pi/framing.mjs",
        "tools/feishu-pi/package.json",
        "tools/feishu-pi/package-lock.json",
    ],
)
@pytest.mark.parametrize("change", ["modify", "add", "delete"])
def test_release_identity_covers_public_support_sources(source, relative, change):
    target = source / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if change != "add":
        target.write_bytes(b"reviewed source\n")
    before = publish(source)
    if change == "delete":
        target.unlink()
    else:
        target.write_bytes(b"different source\n")
    info = builds.get_build_info(source)
    assert info["state"] == "mismatch" and not info["verified"]
    after = publish(source)
    assert before["source_sha256"] != after["source_sha256"]


def test_public_config_source_is_hashed_without_reading_runtime_settings(
    source, monkeypatch
):
    (source / "config").mkdir()
    (source / "config/__init__.py").write_text("# public alert loader", "utf-8")
    (source / "config/.env").write_text("SECRET=fixture", "utf-8")
    (source / "config/config.yaml").write_text("runtime: fixture", "utf-8")
    original = Path.read_bytes

    def guarded(path):
        assert path.name not in {".env", "config.yaml"}
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    assert "config/__init__.py" in publish(source)["files"]


def test_support_source_parent_symlink_is_rejected(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bridge.mjs").write_text("# redirected", "utf-8")
    (source / "tools").mkdir()
    try:
        (source / "tools/feishu-pi").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit test symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        publish(source)


def test_symlink_sources_never_verify(source):
    try:
        (source / "src/linked.py").symlink_to(source / "main.py")
    except OSError:
        pytest.skip("OS does not permit test symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        publish(source)


def test_cli_write_and_read_emit_the_same_verified_identity(source, capsys):
    assert builds.main(["--root", str(source), "--write", "--commit", "a" * 40]) == 0
    written = json.loads(capsys.readouterr().out)
    assert builds.main(["--root", str(source)]) == 0
    read = json.loads(capsys.readouterr().out)
    assert written == read and read["verified"]


def test_unreviewed_commit_is_rejected(source):
    with pytest.raises(ValueError, match="reviewed"):
        builds.write_release_manifest(source, "master;bad-input")
    assert not (source / builds.MANIFEST_PATH).exists()


def test_filesystem_resolution_failure_is_optional_not_a_service_crash(
    source, monkeypatch
):
    publish(source)
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name == "release.json":
            raise RuntimeError("link resolution fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert builds.get_build_info(source)["state"] == "invalid"


@pytest.mark.parametrize("field", ["source_commit", "deployment_id"])
def test_numeric_identity_fields_cannot_pass_as_string_versions(source, field):
    document = publish(source)
    document[field] = int("1" * 40)
    (source / builds.MANIFEST_PATH).write_text(json.dumps(document), "utf-8")
    info = builds.get_build_info(source)
    assert not info["verified"] and info["state"] == "invalid"
    assert "版本信息异常" in builds.build_label(info)
