"""Migrate this deployment's old cron jobs into its single service scheduler.

Deployments call this after stopping the old service and before starting the new
one. Unsupported schedules fail before either YAML or crontab is changed.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config_store import ConfigStore
from .process_lock import exclusive_process_lock


class LegacyCronMigrationError(RuntimeError):
    """The old schedule could not be represented or installed safely."""


@dataclass(frozen=True)
class _LegacyJob:
    mode: str
    fields: tuple[str, ...]
    arguments: tuple[str, ...]
    timezone: str | None


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


def _daily_time(jobs: list[_LegacyJob], timezone: str) -> str:
    times = set()
    for job in jobs:
        if (
            len(job.fields) != 5
            or job.fields[2:] != ("*", "*", "*")
            or not job.fields[0].isdigit()
            or not job.fields[1].isdigit()
            or not (0 <= int(job.fields[0]) < 60)
            or not (0 <= int(job.fields[1]) < 24)
        ):
            raise LegacyCronMigrationError(
                "旧项目 cron 不是每天固定时分，无法自动迁移；配置与 cron 未修改"
            )
        if job.timezone is not None and job.timezone != timezone:
            raise LegacyCronMigrationError(
                "旧项目 cron 的 CRON_TZ 与 scheduler.timezone 不一致；"
                "配置与 cron 未修改"
            )
        times.add(f"{int(job.fields[1]):02d}:{int(job.fields[0]):02d}")
    if len(times) != 1:
        raise LegacyCronMigrationError(
            "旧项目 cron 有多个运行时间，无法自动迁移；配置与 cron 未修改"
        )
    return times.pop()


def _adopt_missing_schedule(config: dict, jobs: list[_LegacyJob]) -> list[str]:
    scheduler = config.setdefault("scheduler", {})
    timezone = scheduler.get("timezone", "Asia/Shanghai")
    changed = []
    optimizer_jobs = [job for job in jobs if job.mode == "--optimize"]
    if optimizer_jobs and "optimize_enabled" not in scheduler:
        if any(job.arguments != ("--optimize",) for job in optimizer_jobs):
            raise LegacyCronMigrationError(
                "旧搜参 cron 带有额外参数，无法迁为全市场任务；配置与 cron 未修改"
            )
        scheduler["optimize_time"] = _daily_time(optimizer_jobs, timezone)
        scheduler["optimize_enabled"] = True
        changed.extend(["scheduler.optimize_enabled", "scheduler.optimize_time"])
    daily_jobs = [job for job in jobs if job.mode == "--once"]
    if daily_jobs and scheduler.get("daily_enabled", True):
        if "run_time" not in scheduler:
            scheduler["run_time"] = _daily_time(daily_jobs, timezone)
            changed.append("scheduler.run_time")
    brief_jobs: dict[str, list[_LegacyJob]] = {}
    for job in jobs:
        if job.mode != "--brief":
            continue
        if len(job.arguments) > 2 or job.arguments[0] != "--brief":
            raise LegacyCronMigrationError(
                "旧简报 cron 参数无法自动迁移；配置与 cron 未修改"
            )
        report_id = job.arguments[1] if len(job.arguments) == 2 else "morning_snapshot"
        brief_jobs.setdefault(report_id, []).append(job)
    for report_id, matching_jobs in brief_jobs.items():
        reports = scheduler.setdefault("brief_reports", [])
        report = next((item for item in reports if item.get("id") == report_id), None)
        if report is None:
            report = {"id": report_id, "enabled": True}
            reports.append(report)
        if report.get("enabled", True) and "run_time" not in report:
            report["run_time"] = _daily_time(matching_jobs, timezone)
            changed.append(f"scheduler.brief_reports.{report_id}.run_time")
    return changed


def migrate_legacy_cron(config_path: Path | str, project_root: Path | str) -> dict:
    """Adopt missing schedule fields and remove only this project's old jobs.

    Explicit YAML settings win. In particular, explicit ``optimize_enabled:
    false`` disables and removes the old optimizer cron without enabling it.
    A missing flag adopts an existing, single daily optimizer time; no cron
    never enables optimization. The returned summary contains no credentials.
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
        jobs = []
        cron_timezone = None
        for line in original.splitlines(keepends=True):
            stripped = line.strip()
            timezone_match = re.fullmatch(r"CRON_TZ\s*=\s*(.*)", stripped)
            if timezone_match:
                cron_timezone = timezone_match.group(1).strip("\"'") or None
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
            jobs.append(
                _LegacyJob(modes[0], tuple(parts[:-1]), arguments, cron_timezone)
            )
        summary = {"removed_jobs": len(jobs), "migrated_fields": []}
        if not jobs:
            return summary
        updated_crontab = "".join(kept_lines)
        if updated_crontab and not updated_crontab.endswith("\n"):
            updated_crontab += "\n"
        install_attempted = False

        def apply_migration(config: dict) -> None:
            nonlocal install_attempted
            summary["migrated_fields"] = _adopt_missing_schedule(config, jobs)
            if _read_crontab() != original:
                raise LegacyCronMigrationError("crontab 已被其他进程修改；迁移已取消")
            # Keep the config lock across installing cron and committing YAML.
            # If YAML commit fails, the exception handler restores the old cron.
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
                        "配置写入失败且 crontab 随后被修改，未覆盖新的 cron；请人工核对"
                    ) from None
            raise
        return summary
