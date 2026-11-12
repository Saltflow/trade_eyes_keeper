"""Remove this deployment's old report cron jobs without creating replacements.

Deployments call this after stopping the old service and before starting the new
one. Unsupported schedules fail before either YAML or crontab is changed.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
from pathlib import Path

from .config_store import ConfigStore
from .process_lock import exclusive_process_lock


class LegacyCronMigrationError(RuntimeError):
    """The project's old cron could not be identified or removed safely."""


def _project_invocation(command: str, project_root: str) -> tuple[str, ...] | None:
    """Recognize an exact project script invocation, never a substring match."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    cwd = None
    if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
        cwd = posixpath.normpath(tokens[1])
        tokens = tokens[3:]
    if tokens and tokens[0] == "env":
        tokens = tokens[1:]
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens or not re.fullmatch(
        r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)", posixpath.basename(tokens[0])
    ):
        return None
    tokens = tokens[1:]
    while tokens and tokens[0] in {"-u", "-B", "-E", "-I", "-O", "-OO", "-s", "-S"}:
        tokens = tokens[1:]
    if not tokens:
        return None
    script = tokens[0]
    if not posixpath.isabs(script):
        if cwd is None:
            return None
        script = posixpath.join(cwd, script)
    if posixpath.normpath(script) != posixpath.join(project_root, "main.py"):
        return None
    arguments = tokens[1:]
    if not any(mode in arguments for mode in ("--once", "--brief", "--optimize")):
        return None
    if any(token in {"&&", "||", ";", "&", "|"} for token in arguments):
        raise LegacyCronMigrationError(
            "项目 cron 含有额外 shell 命令，无法只移除调度任务；配置与 cron 未修改"
        )
    for index, token in enumerate(arguments):
        if "<" in token or ">" in token:
            # In `2>&1`, shlex splits the fd number from the redirection.
            end = index - 1 if index and arguments[index - 1].isdigit() else index
            arguments = arguments[:end]
            break
    return tuple(arguments)


def _read_crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and "no crontab" in result.stderr.lower():
        return ""
    raise LegacyCronMigrationError("无法读取当前用户的 crontab；迁移未执行")


def _install_crontab(contents: str) -> None:
    result = subprocess.run(
        ["crontab", "-"],
        input=contents,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode:
        raise LegacyCronMigrationError("安装迁移后的 crontab 失败；配置未保存")


def migrate_legacy_cron(config_path: Path | str, project_root: Path | str) -> dict:
    """Remove only this project's retired cron jobs, without importing schedules.

    Existing YAML and historical trigger state stay unchanged. No old occurrence
    is submitted or replayed. The returned summary contains no credentials.
    """
    root = posixpath.normpath(str(project_root).replace("\\", "/"))
    if not posixpath.isabs(root):
        raise ValueError("project_root must be an absolute deployment path")
    store = ConfigStore(config_path)
    migration_lock = store.path.with_name(f".{store.path.name}.cron-migration.lock")
    with exclusive_process_lock(migration_lock) as acquired:
        if not acquired:
            raise LegacyCronMigrationError("另一进程正在迁移此项目的 cron")
        original = _read_crontab()
        kept_lines = []
        removed_jobs = 0
        for line in original.splitlines(keepends=True):
            stripped = line.strip()
            parts = stripped.split(maxsplit=5)
            if stripped.startswith("@"):
                parts = stripped.split(maxsplit=1)
            arguments = None
            if not stripped.startswith("#") and len(parts) in {2, 6}:
                arguments = _project_invocation(parts[-1], root)
            if arguments is None:
                kept_lines.append(line)
                continue
            modes = [
                mode
                for mode in ("--once", "--brief", "--optimize")
                if mode in arguments
            ]
            if len(modes) != 1:
                raise LegacyCronMigrationError("旧项目 cron 有多个任务模式；未执行迁移")
            removed_jobs += 1
        summary = {"removed_jobs": removed_jobs, "migrated_fields": []}
        if not removed_jobs:
            return summary
        updated_crontab = "".join(kept_lines)
        if updated_crontab and not updated_crontab.endswith("\n"):
            updated_crontab += "\n"
        install_attempted = False

        def apply_migration(config: dict) -> None:
            nonlocal install_attempted
            if _read_crontab() != original:
                raise LegacyCronMigrationError("crontab 已被其他进程修改；迁移已取消")
            # Reuse the existing configuration lock without changing any YAML.
            # Uncertain cron installation still restores the prior cron if safe.
            install_attempted = True
            _install_crontab(updated_crontab)

        try:
            store.update(apply_migration)
        except Exception:
            if install_attempted:
                current_crontab = _read_crontab()
                if current_crontab == updated_crontab:
                    _install_crontab(original)
                elif current_crontab != original:
                    raise LegacyCronMigrationError(
                        "退役操作未完成且 crontab 随后被修改，未覆盖新的 cron；请人工核对"
                    ) from None
            raise
        return summary
