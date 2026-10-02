#!/usr/bin/env python3
"""Hash-bound, explicit-file deployment for the Feishu assistant.

``--check --file PATH ... --manifest PATH`` records a read-only remote preflight.
``--source-root PATH`` selects a reviewed candidate tree inside this workspace.
``--apply --manifest PATH`` rechecks every source/destination, backs up touched
files, and enables only the assistant settings. Build the dedicated Docker image
and run its acceptance checks before apply. This helper never installs packages,
changes Git, stops services, or sends notifications.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import ci_cd_deploy as deploy

EXACT_FILES = {
    "src/interactive/feishu_app.py",
    "src/interactive/feishu_handler.py",
    "src/core/schedule_manager.py",
    "src/data/baostock_access.py",
    "src/data/market_history.py",
    "src/instruments/point_in_time.py",
    "scripts/deploy_feishu_assistant.py",
    "src/data/dataset_catalog.py",
    "src/data/dataset_documents.py",
    "src/data/cninfo_reports.py",
    "scripts/build_feishu_research_image.py",
    "scripts/preflight_feishu_assistant.py",
    "scripts/feishu_assistant_preflight.py",
    "scripts/feishu_research_worker.py",
    "scripts/check_feishu_research.py",
    "tools/feishu-pi/bridge.mjs",
    "docker/feishu-research/Dockerfile",
    "docker/feishu-research/requirements.txt",
    "docker/feishu-research/.dockerignore",
    "docker/feishu_research/Dockerfile",
    "docker/feishu_research/requirements.txt",
    "docker/feishu_research/.dockerignore",
    "docker/research/Dockerfile",
    "docker/research/requirements.txt",
    "docker/research/.dockerignore",
}


def safe_relative_path(value: str) -> str:
    """Accept only reviewed assistant code, never credentials or main config."""
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError("deployment paths must be relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("deployment path is not canonical")
    source = value.startswith("src/interactive/assistant/") and path.suffix == ".py"
    if not source and value not in EXACT_FILES:
        raise ValueError(f"file is outside the assistant deployment scope: {value}")
    if any(part.startswith(".") for part in path.parts) and value not in EXACT_FILES:
        raise ValueError("hidden source paths cannot be deployed")
    return value


def is_redirect(path: Path) -> bool:
    """Reject symlinks and Windows junctions, including redirects inside scope."""
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def source_root(value: Path | None = None) -> Path:
    project = PROJECT_ROOT.resolve()
    candidate = Path(os.path.abspath(value if value is not None else project))
    if not candidate.is_relative_to(project) or not candidate.is_dir():
        raise ValueError("source root must be an existing directory in the workspace")
    for part in [candidate, *candidate.parents]:
        if is_redirect(part):
            raise ValueError("source root cannot traverse a symlink or junction")
        if part == project:
            break
    resolved = candidate.resolve()
    if not resolved.is_relative_to(project):
        raise ValueError("source root resolves outside the workspace")
    return resolved


def source_file(relative: str, root: Path | None = None) -> Path:
    relative = safe_relative_path(relative)
    root = source_root(root)
    target = root / relative
    for part in [target, *target.parents]:
        if part == root:
            break
        if part.exists() and is_redirect(part):
            raise ValueError(f"symlink or junction cannot be deployed: {relative}")
    if not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"missing or unsafe source: {relative}")
    return target


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_identity() -> str:
    value = [deploy.REMOTE_HOST, deploy.REMOTE_SSH_USER, deploy.REMOTE_DIR]
    return digest(json.dumps(value, separators=(",", ":")).encode())


def ssh_arguments() -> list[str]:
    if not deploy.REMOTE_HOST or not Path(deploy._get_ssh_key()).is_file():
        raise ValueError("configured deployment host/key is unavailable")
    return [
        "ssh",
        "-i",
        deploy._get_ssh_key(),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "BatchMode=yes",
        f"{deploy.REMOTE_SSH_USER}@{deploy.REMOTE_HOST}",
        "python3 -B -",
    ]


# Data enters this remote program through a JSON literal, never shell expansion.
# Keep all secret material on the server; API errors are represented by type/code.
REMOTE_PROGRAM = r"""
import base64, datetime, fcntl, hashlib, json, os, pathlib
import shutil, stat, subprocess, tempfile, time, uuid
from urllib.parse import urlsplit
import yaml
from dotenv import load_dotenv

def sha(data):
    return hashlib.sha256(data).hexdigest()

root = pathlib.Path(request['root'])
if not root.is_absolute() or root.resolve() != root or not root.is_dir():
    raise ValueError('remote project must be an existing canonical directory')

def path_for(relative):
    p = pathlib.PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or p.as_posix() != relative:
        raise ValueError('noncanonical deployment path')
    result = root / relative
    current = result
    while current != root:
        if current.is_symlink():
            raise ValueError('symlink in deployment path')
        current = current.parent
    if result.exists() and not result.is_file():
        raise ValueError('destination is not a regular file')
    return result

def file_hash(relative):
    target = path_for(relative)
    return sha(target.read_bytes()) if target.exists() else None

def preflight():
    cfg = yaml.safe_load(path_for('config/config.yaml').read_text()) or {}
    load_dotenv(path_for('config/.env'), override=False)
    llm = cfg.get('llm', {}) or {}
    key = os.environ.get('DEEPSEEK_API_KEY') or llm.get('api_key')
    endpoint = urlsplit(llm.get('base_url') or 'https://api.deepseek.com/v1')
    models = {'available': False}
    if key and endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com':
        try:
            import requests
            response = requests.get(endpoint._replace(query='', fragment='').geturl().rstrip('/') + '/models',
                headers={'Authorization': 'Bearer ' + key}, timeout=(5, 15), allow_redirects=False)
            models['http_status'] = response.status_code
            if response.status_code == 200:
                ids = [item.get('id') for item in response.json().get('data', [])]
                models['available'] = request['settings']['model'] in ids
        except Exception as exc:
            models['error_type'] = type(exc).__name__
    docker = {'available': False, 'image_present': False}
    if shutil.which('docker'):
        try:
            version = subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'],
                capture_output=True, text=True, timeout=15)
            docker['available'] = version.returncode == 0
            if docker['available']:
                docker['server_version'] = version.stdout.strip()
                image = subprocess.run(['docker', 'image', 'inspect', request['settings']['docker_image']],
                    capture_output=True, text=True, timeout=15)
                docker['image_present'] = image.returncode == 0
                if image.returncode == 0:
                    metadata = json.loads(image.stdout)[0]
                    docker['image_id'] = metadata.get('Id')
                    docker['image_os'] = metadata.get('Os')
        except Exception as exc:
            docker['error_type'] = type(exc).__name__
    free = shutil.disk_usage(root).free
    return {'models': models, 'docker': docker, 'disk_free_bytes': free,
            'ready': bool(models['available'] and docker['available'] and
                          docker['image_present'] and docker.get('image_os') == 'linux' and
                          free >= 512 * 1024**2)}

files = request['files']
baselines = {item['path']: file_hash(item['path']) for item in files}
config_hash = file_hash('config/config.yaml')
env_hash = file_hash('config/.env')
probe = preflight()
if request['mode'] == 'check':
    print(json.dumps({'files': baselines, 'config_sha256': config_hash,
                      'env_sha256': env_hash, 'preflight': probe}))
else:
    if not probe['ready']:
        raise ValueError('DeepSeek model or dedicated Linux Docker image preflight failed')
    expected = request['baseline']
    def verify():
        for item in files:
            if file_hash(item['path']) != expected['files'][item['path']]:
                raise ValueError('destination changed since preflight: ' + item['path'])
        if file_hash('config/config.yaml') != expected['config_sha256']:
            raise ValueError('production config changed since preflight')
        if file_hash('config/.env') != expected['env_sha256']:
            raise ValueError('production credentials changed since preflight')
    verify()
    for item in files:
        content = base64.b64decode(item['content'], validate=True)
        if sha(content) != item['source_sha256']:
            raise ValueError('invalid source digest')
    deployment_id = 'feishu_assistant_' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
    audit_path = root / 'data' / 'deployments' / deployment_id
    for directory in [root / 'data', root / 'data' / 'deployments', audit_path]:
        if directory.is_symlink():
            raise ValueError('unsafe deployment audit directory')
        directory.mkdir(exist_ok=True)
    audit_path.chmod(0o700)
    backup = audit_path / 'backup'
    backup.mkdir(mode=0o700)
    touched = []
    original = {}
    new_hashes = {}
    def replace(relative, content):
        target = path_for(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        fd, name = tempfile.mkstemp(prefix='.assistant-deploy-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, mode)
            os.replace(name, target)
        finally:
            pathlib.Path(name).unlink(missing_ok=True)
    lock = root / 'config' / '.config.yaml.lock'
    if lock.is_symlink():
        raise ValueError('unsafe config lock')
    with lock.open('a+') as handle:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('production config is busy')
                time.sleep(0.05)
        try:
            verify()
            cfg = yaml.safe_load(path_for('config/config.yaml').read_text())
            fs = cfg.setdefault('interactive', {}).setdefault('feishu', {})
            assistant = fs.setdefault('assistant', {})
            if not isinstance(assistant, dict):
                raise ValueError('existing assistant config is not a mapping')
            assistant.update(request['settings'])
            config_bytes = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False).encode()
            changes = [(item['path'], base64.b64decode(item['content'])) for item in files]
            changes.append(('config/config.yaml', config_bytes))
            for relative, content in changes:
                target = path_for(relative)
                original[relative] = target.read_bytes() if target.exists() else None
                if target.exists():
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                new_hashes[relative] = sha(content)
            (audit_path / 'manifest.json').write_text(json.dumps({
                'baseline': expected, 'new_hashes': new_hashes,
                'settings': request['settings'], 'preflight': probe,
            }, indent=2), encoding='utf-8')
            for relative, content in changes:
                baseline = expected['config_sha256'] if relative == 'config/config.yaml' else expected['files'][relative]
                if file_hash(relative) != baseline:
                    raise ValueError('destination changed during apply: ' + relative)
                replace(relative, content)
                touched.append(relative)
            result = {'status': 'applied', 'backup_directory': str(backup),
                      'changed_files': touched, 'settings': request['settings'],
                      'service_restart_required': True}
        except Exception:
            conflicts = []
            for relative in reversed(touched):
                if file_hash(relative) != new_hashes[relative]:
                    conflicts.append(relative)
                    continue
                if original[relative] is None:
                    path_for(relative).unlink()
                else:
                    replace(relative, original[relative])
            (audit_path / 'failure.json').write_text(json.dumps({
                'status': 'rolled_back' if not conflicts else 'rollback_conflict',
                'rollback_conflicts': conflicts,
            }), encoding='utf-8')
            raise
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    (audit_path / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
"""


def remote_request(request: dict) -> dict:
    program = "request = json.loads(" + repr(json.dumps(request)) + ")\n"
    program = "import json\n" + program + REMOTE_PROGRAM
    result = subprocess.run(
        ssh_arguments(),
        input=program,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    if result.returncode:
        # A traceback could contain remote credentials; never forward it.
        raise RuntimeError(f"remote preflight/apply failed (exit {result.returncode})")
    return json.loads(result.stdout)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", "--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    result.add_argument("--file", action="append", default=[])
    result.add_argument("--manifest", type=Path)
    result.add_argument("--source-root", type=Path)
    result.add_argument("--image", default="trade-eyes-research:local")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.check:
        if not args.file:
            raise ValueError("--check requires an explicit --file list")
        root = source_root(args.source_root)
        paths = sorted({safe_relative_path(value) for value in args.file})
        files = [
            {
                "path": path,
                "source_sha256": digest(source_file(path, root).read_bytes()),
            }
            for path in paths
        ]
        settings = {
            "enabled": True,
            "model": "deepseek-flash",
            "reasoning_effort": "high",
            "request_timeout_seconds": 90,
            "max_tokens": 16384,
            "max_tool_rounds": 16,
            "max_tool_calls": 48,
            "conversation_timeout_seconds": 300,
            "stock_data_auto_backfill": True,
            "stock_data_backfill_cooldown_seconds": 1800,
            "stock_data_backfills_per_hour": 6,
            "docker_image": args.image,
            "cpus": 1,
            "memory_mb": 768,
        }
        manifest = {
            "version": 2,
            "target_identity": target_identity(),
            "source_root": str(root),
            "source_identity": digest(str(root).encode("utf-8")),
            "settings": settings,
            "files": files,
        }
        manifest["baseline"] = remote_request(
            {
                "mode": "check",
                "root": deploy.REMOTE_DIR,
                "files": files,
                "settings": settings,
            }
        )
        if args.manifest:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest, indent=2))
    else:
        if not args.manifest or args.file:
            raise ValueError("--apply requires --manifest and rejects --file overrides")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if (
            manifest.get("version") != 2
            or manifest.get("target_identity") != target_identity()
        ):
            raise ValueError("manifest does not match the configured deployment target")
        recorded_root = manifest.get("source_root")
        if not isinstance(recorded_root, str) or not recorded_root:
            raise ValueError("manifest has no bound source root")
        root = source_root(Path(recorded_root))
        if str(root) != recorded_root or digest(
            str(root).encode("utf-8")
        ) != manifest.get("source_identity"):
            raise ValueError("manifest source root binding changed")
        if args.source_root is not None and source_root(args.source_root) != root:
            raise ValueError("source root override does not match the preflight")
        from src.interactive.assistant.settings import AssistantSettings

        settings = AssistantSettings.parse_obj(manifest["settings"])
        if not settings.enabled or settings.cpus != 1 or settings.memory_mb != 768:
            raise ValueError("manifest does not contain the reviewed server limits")
        files = []
        if not isinstance(manifest.get("files"), list) or not manifest["files"]:
            raise ValueError("manifest must contain an explicit source file list")
        seen = set()
        for item in manifest["files"]:
            if item["path"] in seen:
                raise ValueError("manifest contains duplicate files")
            seen.add(item["path"])
            content = source_file(item["path"], root).read_bytes()
            if digest(content) != item["source_sha256"]:
                raise ValueError(f"source changed after preflight: {item['path']}")
            files.append({**item, "content": base64.b64encode(content).decode("ascii")})
        print(
            json.dumps(
                remote_request(
                    {
                        "mode": "apply",
                        "root": deploy.REMOTE_DIR,
                        "files": files,
                        "settings": manifest["settings"],
                        "baseline": manifest["baseline"],
                    }
                ),
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(
            f"Assistant deployment failed: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        raise SystemExit(1)
