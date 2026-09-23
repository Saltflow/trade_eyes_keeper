"""The service's single APScheduler entry point, shared with Bot commands.

Task execution uses the same main.py commands as explicit command-line runs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from threading import RLock

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config_store import DEFAULT_CONFIG_PATH, ConfigStore, runtime_config

logger = logging.getLogger(__name__)

DEFAULT_DAILY_MISFIRE_GRACE_SECONDS = 3600
DEFAULT_BRIEF_MISFIRE_GRACE_SECONDS = 900
DEFAULT_OPTIMIZE_MISFIRE_GRACE_SECONDS = 7200

# task_id → job_id 映射
_JOB_IDS = {
    "daily": "daily",
    "morning_snapshot": "brief_morning_snapshot",
    "afternoon_snapshot": "brief_afternoon_snapshot",
    "optimize": "optimize",
}

_schedule_manager: ScheduleManager | None = None


def get_schedule_manager() -> ScheduleManager | None:
    """Return this service process's live scheduler, if it has started."""
    return _schedule_manager


def set_schedule_manager(manager: ScheduleManager | None) -> None:
    """Register a live scheduler without depending on an HTTP server."""
    global _schedule_manager
    _schedule_manager = manager


class ScheduleManager:
    """Manage the service schedule and persist Bot edits to the raw config."""

    def __init__(self, config: dict, config_path: Path | None = None):
        self.config = config
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._change_lock = RLock()
        tz_str = config.get("scheduler", {}).get("timezone", "Asia/Shanghai")
        try:
            timezone = pytz.timezone(tz_str)
        except pytz.exceptions.UnknownTimeZoneError:
            timezone = pytz.timezone("Asia/Shanghai")
            logger.warning("未知时区 %s，使用 Asia/Shanghai", tz_str)
        self.timezone = timezone
        self.scheduler = BackgroundScheduler(
            timezone=timezone,
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    @staticmethod
    def _grace_seconds(value, default: int) -> int:
        """Return a positive APScheduler misfire window."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def start(self):
        """注册所有 job 并启动调度器。"""
        if self.scheduler.running:
            return
        sched_cfg = self.config.get("scheduler", {})

        # 日报
        daily_time = sched_cfg.get("run_time", "19:00")
        if sched_cfg.get("daily_enabled", True):
            self._add_job(
                "daily",
                daily_time,
                ["--once"],
                "每日日报",
                misfire_grace_seconds=self._grace_seconds(
                    sched_cfg.get("daily_misfire_grace_seconds"),
                    DEFAULT_DAILY_MISFIRE_GRACE_SECONDS,
                ),
            )
            if sched_cfg.get("run_on_startup", False):
                self.scheduler.add_job(
                    func=self._job_runner("daily", ["--once"]),
                    trigger="date",
                    run_date=datetime.now(self.timezone),
                    id="startup_task",
                    name="启动时立即执行日报",
                    replace_existing=True,
                )

        # 简报
        for br in sched_cfg.get("brief_reports", []):
            br_id = br.get("id", "morning_snapshot")
            if not br.get("enabled", True):
                continue
            br_time = br.get("run_time", "09:50")
            job_id = _JOB_IDS.get(br_id, f"brief_{br_id}")
            self._add_job(
                br_id,
                br_time,
                ["--brief", br_id],
                br.get("label", br_id),
                job_id_override=job_id,
                misfire_grace_seconds=self._grace_seconds(
                    br.get(
                        "misfire_grace_seconds",
                        sched_cfg.get("brief_misfire_grace_seconds"),
                    ),
                    DEFAULT_BRIEF_MISFIRE_GRACE_SECONDS,
                ),
            )

        # 策略优化（每天凌晨 2:00）
        opt_time = sched_cfg.get("optimize_time", "02:00")
        # A full 155k-candidate run is opt-in; daily overlap can exhaust RAM.
        if sched_cfg.get("optimize_enabled", False):
            self._add_job(
                "optimize",
                opt_time,
                ["--optimize"],
                "策略优化",
                misfire_grace_seconds=self._grace_seconds(
                    sched_cfg.get("optimize_misfire_grace_seconds"),
                    DEFAULT_OPTIMIZE_MISFIRE_GRACE_SECONDS,
                ),
            )

        self.scheduler.start()
        set_schedule_manager(self)
        logger.info(f"调度器已启动: {len(self.scheduler.get_jobs())} 个任务")

    def _add_job(
        self,
        task_id: str,
        time_str: str,
        cli_args: list[str],
        name: str,
        job_id_override: str | None = None,
        misfire_grace_seconds: int = DEFAULT_BRIEF_MISFIRE_GRACE_SECONDS,
    ):
        """注册一个 job，通过子进程执行 main.py。"""
        hour, minute = self._parse_time(time_str)
        if hour is None:
            logger.warning(f"跳过无效调度时间: {task_id}={time_str}")
            return

        job_id = job_id_override or _JOB_IDS.get(task_id, task_id)

        trigger = CronTrigger(hour=hour, minute=minute, timezone=self.timezone)
        self.scheduler.add_job(
            func=self._job_runner(task_id, cli_args),
            trigger=trigger,
            id=job_id,
            name=name,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=misfire_grace_seconds,
        )
        logger.info(
            "已注册: %s (%02d:%02d, misfire_grace=%ss)",
            name,
            hour,
            minute,
            misfire_grace_seconds,
        )

    @staticmethod
    def _job_runner(task_id: str, cli_args: list[str]):
        def run():
            project_root = Path(__file__).resolve().parents[2]
            cmd = [sys.executable, str(project_root / "main.py"), *cli_args]
            try:
                log_file = project_root / "logs" / "quant_system.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("a", encoding="utf-8") as output:
                    process = subprocess.Popen(
                        cmd,
                        cwd=str(project_root),
                        env=os.environ.copy(),
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                logger.info("调度任务已启动 pid=%s: %s", process.pid, task_id)
            except Exception:
                logger.exception("调度任务启动失败: %s", task_id)

        return run

    def stop(self):
        """停止调度器。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("调度器已停止")
        if get_schedule_manager() is self:
            set_schedule_manager(None)

    def get_schedule(self) -> list[dict]:
        """返回当前所有任务的调度信息。"""
        result = []
        for job in self.scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            if next_run is None or job.id == "startup_task":
                continue
            next_run = next_run.astimezone(self.timezone)
            result.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "time": f"{next_run.hour:02d}:{next_run.minute:02d}",
                    "next_run": str(next_run),
                    "timezone": str(self.timezone),
                }
            )
        return result

    def reschedule(self, task_id: str, time_str: str) -> bool:
        """修改任务时间，立即生效 + 写回 config。

        Args:
            task_id: "daily" / "morning_snapshot" / "afternoon_snapshot" / "optimize"
            time_str: "HH:MM"

        Returns:
            True = 成功, False = 无效任务或时间
        """
        hour, minute = self._parse_time(time_str)
        if hour is None:
            return False

        with self._change_lock:
            job_id = _JOB_IDS.get(task_id, f"brief_{task_id}")
            if self.scheduler.get_job(job_id) is None:
                return False
            normalized_time = f"{hour:02d}:{minute:02d}"
            try:
                saved = self._persist_schedule(task_id, normalized_time)
            except Exception:
                logger.exception("调度配置保存失败，运行时间未修改: %s", task_id)
                return False
            trigger = CronTrigger(hour=hour, minute=minute, timezone=self.timezone)
            try:
                self.scheduler.reschedule_job(job_id, trigger=trigger)
            except Exception as exc:
                raise RuntimeError(
                    "调度配置已保存，但运行时刷新失败，请重启服务"
                ) from exc
            self.config = runtime_config(saved)
            logger.info("调度已修改: %s → %s", task_id, normalized_time)
            return True

    def _persist_schedule(self, task_id: str, time_str: str):
        """将修改持久化到 config.yaml。"""

        def mutate(config: dict) -> None:
            if task_id == "daily":
                if not config.get("scheduler", {}).get("daily_enabled", True):
                    raise ValueError("daily schedule is disabled")
                config.setdefault("scheduler", {})["run_time"] = time_str
            elif task_id == "optimize":
                if not config.get("scheduler", {}).get("optimize_enabled", False):
                    raise ValueError("optimizer schedule is disabled")
                config.setdefault("scheduler", {})["optimize_time"] = time_str
            else:
                # brief_reports 里找对应的 id
                for br in config.get("scheduler", {}).get("brief_reports", []):
                    if br.get("id") == task_id:
                        if not br.get("enabled", True):
                            raise ValueError("brief schedule is disabled")
                        br["run_time"] = time_str
                        break
                else:
                    raise ValueError(f"unknown brief schedule: {task_id}")

        saved = ConfigStore(self.config_path).update(mutate)
        logger.info("调度配置已写入: %s", self.config_path)
        return saved

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int | None, int | None]:
        """解析 HH:MM 格式时间。"""
        try:
            if ":" in time_str:
                h, m = time_str.split(":")
            elif "." in time_str:
                h, m = time_str.split(".")
            else:
                return None, None
            hour, minute = int(h.strip()), int(m.strip())
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None, None
            return hour, minute
        except (ValueError, AttributeError, TypeError):
            return None, None
