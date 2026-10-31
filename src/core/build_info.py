"""Process-fixed release identity, independent of a dirty checkout's Git HEAD."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from zoneinfo import ZoneInfo

from .runtime_files import atomic_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path("data/deployment/release.json")
logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# Executable support outside src belongs to the same release identity. These
# are exact public source paths; runtime settings and secrets remain excluded.
_SUPPORT_SOURCE_FILES = (
    "config/__init__.py",
    "requirements.txt",
    "config/in_kind_estimation.yaml",
    "tools/feishu-pi/bridge.mjs",
    "tools/feishu-pi/shared-runtime.mjs",
    "tools/feishu-pi/framing.mjs",
    "tools/feishu-pi/package.json",
    "tools/feishu-pi/package-lock.json",
)


def _version(root: Path) -> str:
    """Read only [project].version; works on the production Python 3.10."""
    path = root / "pyproject.toml"
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("project_path_escape")
    project = False
    for line in path.read_text("utf-8").splitlines():
        if line.strip().startswith("["):
            project = line.strip() == "[project]"
        if project:
            match = re.fullmatch(
                r"\s*version\s*=\s*[\"\x27]([\w.+-]{1,64})[\"\x27]\s*", line
            )
            if match:
                return match[1]
    raise ValueError("project_version_missing")


def _source_hashes(root: Path) -> dict[str, str]:
    """Cover actual application code/templates, never credentials or databases."""
    if (root / "src").is_symlink() or not (root / "src").is_dir():
        raise ValueError("invalid_source_directory")
    paths = [root / "main.py", root / "pyproject.toml"]
    for name in _SUPPORT_SOURCE_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            paths.append(path)
    for path in (root / "src").rglob("*"):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError("source_symlink")
        if path.is_file() and (
            path.suffix == ".py" or path.is_relative_to(root / "src/templates")
        ):
            paths.append(path)
    result = {}
    for path in sorted(paths):
        if any(
            parent.is_symlink()
            for parent in (path, *path.parents)
            if parent != root and parent.is_relative_to(root)
        ):
            raise ValueError("source_symlink")
        if not path.resolve().is_relative_to(root):
            raise ValueError("source_path_escape")
        result[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return result


def _digest(files: dict) -> str:
    value = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_manifest_key")
        result[key] = value
    return result


@lru_cache(maxsize=8)
def _snapshot(root_name: str) -> dict:
    root = Path(root_name)
    result = {
        "version": "unknown",
        "state": "unregistered",
        "verified": False,
        "source_commit": "",
        "source_sha256": "",
        "deployment_id": "",
        "deployed_at": "",
    }
    try:
        result["version"] = _version(root)
        path = root / MANIFEST_PATH
        if not path.resolve().is_relative_to(root):
            raise ValueError("manifest_path_escape")
        if not path.exists():
            return result
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("invalid_manifest_file")
        document = json.loads(path.read_text("utf-8"), object_pairs_hook=_unique_pairs)
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or document.get("version") != result["version"]
            or not isinstance(document.get("source_commit"), str)
            or not _COMMIT.fullmatch(document["source_commit"])
            or not isinstance(document.get("deployment_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,100}", document["deployment_id"])
        ):
            raise ValueError("invalid_release_identity")
        deployed = datetime.fromisoformat(document["deployed_at"])
        if deployed.tzinfo is None or deployed.utcoffset() is None:
            raise ValueError("deployment_time_requires_timezone")
        files = document["files"]
        if (
            not isinstance(files, dict)
            or not files
            or not all(isinstance(v, str) and _SHA.fullmatch(v) for v in files.values())
            or document.get("source_sha256") != _digest(files)
        ):
            raise ValueError("invalid_source_manifest")
        # Compare the whole managed set, including newly added source files.
        if files != _source_hashes(root):
            result.update(state="mismatch", reason="source_manifest_mismatch")
            logger.warning("Release identity: source manifest mismatch")
            return result
        result.update(
            state="verified",
            verified=True,
            source_commit=document["source_commit"],
            source_sha256=document["source_sha256"],
            deployment_id=document["deployment_id"],
            deployed_at=document["deployed_at"],
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        result.update(state="invalid", reason="invalid_release_manifest")
        logger.warning("Release identity: invalid or unavailable metadata")
    return result


def get_build_info(root: Path | None = None) -> dict:
    """Initialize once per process/root and return an independent small summary."""
    with _LOCK:
        return dict(_snapshot(str((root or PROJECT_ROOT).resolve())))


def build_label(info: dict | None = None) -> str:
    info = get_build_info() if info is None else info
    version = "v" + str(info["version"])
    if info.get("verified"):
        return f"{version} · {info['source_commit'][:7]} · 构建 {info['source_sha256'][:8]}"
    states = {"unregistered": "未登记发布", "mismatch": "构建与部署记录不符"}
    return f"{version} · {states.get(info.get('state'), '版本信息异常')}"


def write_release_manifest(root: Path, source_commit: str) -> dict:
    """Publish after source installation and before starting any application."""
    root = Path(root).resolve()
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise ValueError("a reviewed full source commit is required")
    files = _source_hashes(root)
    digest = _digest(files)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    document = {
        "schema_version": 1,
        "version": _version(root),
        "source_commit": source_commit,
        "source_sha256": digest,
        "deployment_id": (
            now.strftime("%Y%m%dT%H%M%S%f%z")
            + "-"
            + digest[:8]
            + "-"
            + uuid4().hex[:12]
        ),
        "deployed_at": now.isoformat(),
        "files": files,
    }
    path = root / MANIFEST_PATH
    if path.is_symlink() or not path.parent.resolve().is_relative_to(root):
        raise ValueError("manifest_path_escape")
    atomic_write_json(path, document, allow_nan=False)
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--commit")
    args = parser.parse_args(argv)
    if args.write:
        if not args.commit:
            parser.error("--write requires the reviewed --commit")
        write_release_manifest(args.root, args.commit)
        # Validate again; publication must not manufacture a verified result.
        info = _snapshot.__wrapped__(str(args.root.resolve()))
    else:
        info = get_build_info(args.root)
    print(json.dumps(info, ensure_ascii=False))
    return 0 if info["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
