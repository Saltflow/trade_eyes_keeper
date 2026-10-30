"""Scoped deployment must preserve production files and reject stale baselines."""

import base64
import itertools
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
import requests
import yaml

from scripts import deploy_feishu_assistant as deploy


def run_remote(request):
    # Execute the reviewed helper source with mocked networking in a temp root.
    exec(compile(deploy.REMOTE_PROGRAM, "remote_deploy", "exec"), {"request": request})  # noqa: S102


@pytest.mark.parametrize(
    "path",
    [
        "../config/config.yaml",
        "config/.env",
        "config/config.yaml",
        "/src/interactive/assistant/client.py",
        "src\\interactive\\assistant\\client.py",
        "src/interactive/assistant/../../main.py",
        "main.py",
        "src/interactive/assistant//client.py",
        "src/interactive/assistant/.hidden.py",
    ],
)
def test_rejects_paths_outside_explicit_assistant_scope(path):
    with pytest.raises(ValueError):
        deploy.safe_relative_path(path)


def test_assistant_sources_and_docker_context_are_allowlisted():
    for path in (
        "src/interactive/assistant/service.py",
        "tools/feishu-pi/bridge.mjs",
        "tools/feishu-pi/shared-runtime.mjs",
        "tools/feishu-pi/framing.mjs",
        "src/interactive/feishu_app.py",
        "docker/feishu-research/Dockerfile",
        "scripts/check_feishu_research.py",
        "src/data/baostock_access.py",
        "src/data/market_history.py",
        "src/instruments/point_in_time.py",
    ):
        assert deploy.safe_relative_path(path) == path


@pytest.fixture
def candidate(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / "reviewed"
    relative = "src/data/market_history.py"
    source = root / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b"reviewed source\n")
    monkeypatch.setattr(deploy, "PROJECT_ROOT", workspace)
    calls = []

    def remote(request):
        calls.append(request)
        return {"files": {relative: None}, "preflight": {"ready": True}}

    monkeypatch.setattr(deploy, "remote_request", remote)
    manifest = workspace / "manifest.json"
    deploy.main(
        [
            "--check",
            "--source-root",
            str(root),
            "--file",
            relative,
            "--manifest",
            str(manifest),
        ]
    )
    calls.clear()
    return workspace, root, source, manifest, calls


def test_candidate_root_and_contents_are_bound_in_manifest(candidate):
    _, root, source, manifest, calls = candidate
    saved = json.loads(manifest.read_text())
    assert saved["source_root"] == str(root.resolve())
    assert saved["source_identity"] == deploy.digest(str(root.resolve()).encode())
    assert saved["files"][0]["source_sha256"] == deploy.digest(source.read_bytes())
    deploy.main(["--apply", "--manifest", str(manifest)])
    assert len(calls) == 1
    assert base64.b64decode(calls[0]["files"][0]["content"]) == source.read_bytes()


def test_source_root_outside_workspace_is_rejected(candidate, tmp_path):
    with pytest.raises(ValueError, match="in the workspace"):
        deploy.source_root(tmp_path)


def test_source_root_override_cannot_switch_candidate_tree(candidate):
    workspace, _, _, manifest, calls = candidate
    with pytest.raises(ValueError, match="override does not match"):
        deploy.main(
            ["--apply", "--manifest", str(manifest), "--source-root", str(workspace)]
        )
    assert calls == []


def test_manifest_source_root_binding_cannot_be_changed(candidate):
    workspace, _, _, manifest, calls = candidate
    saved = json.loads(manifest.read_text())
    saved["source_root"] = str(workspace.resolve())
    manifest.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="source root binding changed"):
        deploy.main(["--apply", "--manifest", str(manifest)])
    assert calls == []


def test_candidate_source_changes_fail_before_remote_access(candidate):
    _, _, source, manifest, calls = candidate
    source.write_bytes(b"later unreviewed edit\n")
    with pytest.raises(ValueError, match="source changed after preflight"):
        deploy.main(["--apply", "--manifest", str(manifest)])
    assert calls == []


def test_candidate_missing_file_does_not_fall_back_to_workspace(candidate):
    workspace, _, source, manifest, calls = candidate
    fallback = workspace / "src/data/market_history.py"
    fallback.parent.mkdir(parents=True)
    fallback.write_bytes(source.read_bytes())
    source.unlink()
    with pytest.raises(ValueError, match="missing or unsafe source"):
        deploy.main(["--apply", "--manifest", str(manifest)])
    assert calls == []


def test_source_root_rejects_junction_or_symlink(candidate, monkeypatch):
    _, root, _, _, _ = candidate
    real_lstat = Path.lstat

    def redirected(path):
        if path == root:
            return types.SimpleNamespace(
                st_file_attributes=0x400, st_mode=real_lstat(path).st_mode
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", redirected)
    with pytest.raises(ValueError, match="symlink or junction"):
        deploy.source_root(root)


PI_FILES = {
    "tools/feishu-pi/bridge.mjs",
    "tools/feishu-pi/shared-runtime.mjs",
    "tools/feishu-pi/framing.mjs",
}
PI_SUBSETS = [
    list(paths)
    for size in (1, 2)
    for paths in itertools.combinations(sorted(PI_FILES), size)
]


@pytest.mark.parametrize("paths", PI_SUBSETS)
@pytest.mark.parametrize("mode", ["check", "apply"])
def test_partial_pi_bundle_is_rejected_before_any_remote_access(candidate, paths, mode):
    _, root, _, manifest, calls = candidate
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"reviewed Pi source\n")
    if mode == "check":
        args = ["--check", "--source-root", str(root)]
        for path in paths:
            args.extend(["--file", path])
    else:
        saved = json.loads(manifest.read_text())
        saved["files"] = [
            {"path": path, "source_sha256": deploy.digest((root / path).read_bytes())}
            for path in paths
        ]
        manifest.write_text(json.dumps(saved))
        args = ["--apply", "--manifest", str(manifest)]
    with pytest.raises(ValueError, match="incomplete Pi runtime bundle"):
        deploy.main(args)
    assert calls == []


def pi_preflight(root, manifest):
    args = ["--check", "--source-root", str(root), "--manifest", str(manifest)]
    for path in sorted(PI_FILES):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode())
        args.extend(["--file", path])
    deploy.main(args)


def test_complete_pi_bundle_binds_every_source_and_sends_exact_contents(candidate):
    _, root, _, manifest, calls = candidate
    pi_preflight(root, manifest)
    calls.clear()
    saved = json.loads(manifest.read_text())
    assert {item["path"] for item in saved["files"]} == PI_FILES
    deploy.main(["--apply", "--manifest", str(manifest)])
    assert len(calls) == 1
    for item in calls[0]["files"]:
        assert base64.b64decode(item["content"]) == (root / item["path"]).read_bytes()


@pytest.mark.parametrize("path", sorted(PI_FILES))
@pytest.mark.parametrize("change", ["modify", "delete"])
def test_pi_dependency_drift_after_check_cannot_reach_apply(candidate, path, change):
    _, root, _, manifest, calls = candidate
    pi_preflight(root, manifest)
    calls.clear()
    if change == "modify":
        (root / path).write_bytes(b"unreviewed runtime\n")
    else:
        (root / path).unlink()
    with pytest.raises(ValueError, match="source changed|missing or unsafe"):
        deploy.main(["--apply", "--manifest", str(manifest)])
    assert calls == []


@pytest.fixture
def remote(monkeypatch, tmp_path):
    config = {
        "llm": {"base_url": "https://api.deepseek.com/v1"},
        "interactive": {
            "feishu": {
                "enabled": True,
                "allowed_chat_ids": ["*"],
                "assistant": {"timeout_seconds": 99},
            }
        },
        "scheduler": {"run_time": "19:00"},
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.yaml").write_text(yaml.safe_dump(config))
    (tmp_path / "config/.env").write_text("DEEPSEEK_API_KEY=testing-only-secret\n")
    source = tmp_path / "src/interactive/assistant/client.py"
    source.parent.mkdir(parents=True)
    source.write_text("old\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "testing-only-secret")
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        types.SimpleNamespace(
            LOCK_EX=1,
            LOCK_NB=2,
            LOCK_UN=4,
            flock=lambda *_: None,
        ),
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        requests,
        "get",
        lambda *_, **__: types.SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"id": "deepseek-flash"}]},
        ),
    )

    def docker(args, **kwargs):
        stdout = (
            "24.0.4"
            if "version" in args
            else json.dumps(
                [
                    {"Id": "sha256:" + "a" * 64, "Os": "linux"},
                ]
            )
        )
        return types.SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", docker)
    relative = "src/interactive/assistant/client.py"
    content = b"new\n"
    files = [
        {
            "path": relative,
            "source_sha256": deploy.digest(content),
            "content": base64.b64encode(content).decode(),
        }
    ]
    request = {
        "mode": "apply",
        "root": str(tmp_path.resolve()),
        "files": files,
        "settings": {
            "enabled": True,
            "model": "deepseek-flash",
            "docker_image": "trade-eyes-research:local",
            "cpus": 1,
            "memory_mb": 768,
        },
        "baseline": {
            "files": {relative: deploy.digest(source.read_bytes())},
            "config_sha256": deploy.digest(
                (tmp_path / "config/config.yaml").read_bytes()
            ),
            "env_sha256": deploy.digest((tmp_path / "config/.env").read_bytes()),
        },
    }
    return tmp_path, request, config


def test_remote_check_does_not_write_production_or_audit(remote, capsys):
    root, request, _ = remote
    request["mode"] = "check"
    run_remote(request)
    result = json.loads(capsys.readouterr().out)
    assert result["preflight"]["ready"]
    assert not (root / "data").exists()
    assert (root / request["files"][0]["path"]).read_text() == "old\n"


def test_remote_apply_preserves_unrelated_config_env_and_backs_up(remote, capsys):
    root, request, original = remote
    credentials = (root / "config/.env").read_bytes()
    run_remote(request)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "applied"
    assert result["service_restart_required"]
    current = yaml.safe_load((root / "config/config.yaml").read_text())
    assert current["scheduler"] == original["scheduler"]
    assert current["interactive"]["feishu"]["allowed_chat_ids"] == ["*"]
    assert current["interactive"]["feishu"]["assistant"]["timeout_seconds"] == 99
    assert current["interactive"]["feishu"]["assistant"]["memory_mb"] == 768
    assert (root / "config/.env").read_bytes() == credentials
    backup = next((root / "data/deployments").iterdir()) / "backup"
    assert (backup / request["files"][0]["path"]).read_text() == "old\n"
    assert not (backup / "config/.env").exists()


def test_remote_refuses_changed_file_before_writing(remote):
    root, request, _ = remote
    source = root / request["files"][0]["path"]
    source.write_text("production edit\n")
    with pytest.raises(ValueError, match="destination changed since preflight"):
        run_remote(request)
    assert source.read_text() == "production edit\n"
    assert not (root / "data").exists()


def test_failed_apply_rolls_back_only_its_own_writes(remote, monkeypatch):
    root, request, _ = remote
    config_before = (root / "config/config.yaml").read_bytes()
    real_replace = os.replace

    def fail_config(source, destination):
        if str(destination).endswith("config.yaml"):
            raise OSError("injected config write failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_config)
    with pytest.raises(OSError, match="injected"):
        run_remote(request)
    assert (root / request["files"][0]["path"]).read_text() == "old\n"
    assert (root / "config/config.yaml").read_bytes() == config_before
    failure = next((root / "data/deployments").glob("*/failure.json"))
    assert json.loads(failure.read_text())["status"] == "rolled_back"
