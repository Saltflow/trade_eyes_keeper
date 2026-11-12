"""Service scheduler lifecycle; report automation belongs to managed triggers.

Legacy fixed report settings remain readable but never register callbacks.
Independent non-report jobs on the scheduler are preserved.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from .config_store import DEFAULT_CONFIG_PATH, ConfigStore

logger = logging.getLogger(__name__)
_schedule_manager: ScheduleManager | None = None


def get_schedule_manager() -> ScheduleManager | None:
    return _schedule_manager


def set_schedule_manager(manager: ScheduleManager | None) -> None:
    global _schedule_manager
    _schedule_manager = manager


class ScheduleManager:
    """Keep service lifecycle and non-report jobs without legacy report rules."""

    def __init__(
        self, config: dict, config_path: Path | None = None, *, execution_runtime=None
    ):
        self.config = config
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.execution_runtime = execution_runtime
        self._change_lock = RLock()
        tz_str = config.get("scheduler", {}).get("timezone", "Asia/Shanghai")
        try:
            self.timezone = pytz.timezone(tz_str)
        except pytz.exceptions.UnknownTimeZoneError:
            self.timezone = pytz.timezone("Asia/Shanghai")
            logger.warning("未知时区 %s，使用 Asia/Shanghai", tz_str)
        self.scheduler = BackgroundScheduler(
            timezone=self.timezone,
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    @staticmethod
    def _retired_job(task_id: str) -> bool:
        name = str(task_id).lower()
        return name in {"daily", "startup_task", "optimize"} or name.startswith(
            ("brief_", "fundamental_quant")
        )

    def _remove_retired_jobs(self) -> None:
        for job in self.scheduler.get_jobs():
            if self._retired_job(job.id):
                self.scheduler.remove_job(job.id)
                logger.info("已移除退役调度: %s", job.id)

    def start(self) -> None:
        self._remove_retired_jobs()
        if not self.scheduler.running:
            self.scheduler.start()
        set_schedule_manager(self)
        logger.info("调度器已启动: %s 个非报告任务", len(self.scheduler.get_jobs()))

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if get_schedule_manager() is self:
            set_schedule_manager(None)

    def reload_config(self) -> None:
        """Refresh report worker configuration without restoring fixed jobs."""
        with self._change_lock:
            if not self.scheduler.running:
                raise RuntimeError("调度器尚未启动，配置在下次启动后生效")
            self._remove_retired_jobs()
            saved = ConfigStore(self.config_path).load_runtime()
            if self.execution_runtime is not None:
                from .report_execution import register_report_jobs

                register_report_jobs(
                    self.execution_runtime, self.config_path, config=saved
                )
            self.config = saved

    def get_schedule(self) -> list[dict]:
        """List existing non-report service jobs; managed rules use TriggerStore."""
        result = []
        for job in self.scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            if next_run is None or self._retired_job(job.id):
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
