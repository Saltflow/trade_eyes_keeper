"""调度管理器 — 内嵌 APScheduler，由 health server 常驻运行。

替代外部 crontab，支持 /schedule 交互式修改。
"""

import logging
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

logger = logging.getLogger(__name__)

# task_id → job_id 映射
_JOB_IDS = {
    "daily": "daily",
    "morning_snapshot": "brief_morning_snapshot",
    "afternoon_snapshot": "brief_afternoon_snapshot",
    "optimize": "optimize",
}


class ScheduleManager:
    """管理 APScheduler 调度，内嵌于 health server。"""

    def __init__(self, config: dict, config_path: Path | None = None):
        self.config = config
        self.config_path = Path(config_path) if config_path else Path("config/config.yaml")
        tz_str = config.get("scheduler", {}).get("timezone", "Asia/Shanghai")
        try:
            timezone = pytz.timezone(tz_str)
        except pytz.exceptions.UnknownTimeZoneError:
            timezone = pytz.timezone("Asia/Shanghai")
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def start(self):
        """注册所有 job 并启动调度器。"""
        sched_cfg = self.config.get("scheduler", {})

        # 日报
        daily_time = sched_cfg.get("run_time", "19:00")
        if sched_cfg.get("daily_enabled", True):
            self._add_job("daily", daily_time, ["--once"], "每日日报")

        # 简报
        for br in sched_cfg.get("brief_reports", []):
            br_id = br.get("id", "morning_snapshot")
            if not br.get("enabled", True):
                continue
            br_time = br.get("run_time", "09:50")
            job_id = _JOB_IDS.get(br_id, f"brief_{br_id}")
            self._add_job(
                br_id, br_time,
                ["--brief", br_id],
                br.get("label", br_id),
                job_id_override=job_id,
            )

        # 策略优化（每天凌晨 2:00）
        opt_time = sched_cfg.get("optimize_time", "02:00")
        if sched_cfg.get("optimize_enabled", True):
            self._add_job(
                "optimize", opt_time,
                ["--optimize-v2"],
                "策略优化",
            )

        self.scheduler.start()
        logger.info(
            f"调度器已启动: {len(self.scheduler.get_jobs())} 个任务"
        )

    def _add_job(
        self,
        task_id: str,
        time_str: str,
        cli_args: list[str],
        name: str,
        job_id_override: str | None = None,
    ):
        """注册一个 job，通过子进程执行 main.py。"""
        hour, minute = self._parse_time(time_str)
        if hour is None:
            logger.warning(f"跳过无效调度时间: {task_id}={time_str}")
            return

        job_id = job_id_override or _JOB_IDS.get(task_id, task_id)

        def _run():
            import subprocess
            import sys
            import os
            from pathlib import Path

            project_root = Path(__file__).parent.parent.parent
            main_py = project_root / "main.py"
            cmd = [sys.executable, str(main_py)] + cli_args
            try:
                subprocess.Popen(
                    cmd, cwd=str(project_root),
                    env=os.environ.copy(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                logger.info(f"调度任务已启动子进程: {' '.join(cmd)}")
            except Exception as e:
                logger.exception(f"调度任务启动失败: {task_id}: {e}")

        trigger = CronTrigger(hour=hour, minute=minute)
        self.scheduler.add_job(
            func=_run,
            trigger=trigger,
            id=job_id,
            name=name,
            replace_existing=True,
        )
        logger.info(f"已注册: {name} ({hour:02d}:{minute:02d})")

    def stop(self):
        """停止调度器。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("调度器已停止")

    def get_schedule(self) -> list[dict]:
        """返回当前所有任务的调度信息。"""
        result = []
        for job in self.scheduler.get_jobs():
            result.append({
                "id": job.id,
                "name": job.name,
                "time": f"{job.next_run_time.hour:02d}:{job.next_run_time.minute:02d}",
                "next_run": str(job.next_run_time),
            })
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

        job_id = _JOB_IDS.get(task_id)
        if job_id is None:
            return False

        job = self.scheduler.get_job(job_id)
        if job is None:
            return False

        trigger = CronTrigger(hour=hour, minute=minute)
        self.scheduler.reschedule_job(job_id, trigger=trigger)
        logger.info(f"调度已修改: {task_id} → {time_str}")

        # 写回 config.yaml
        self._persist_schedule(task_id, time_str)
        return True

    def _persist_schedule(self, task_id: str, time_str: str):
        """将修改持久化到 config.yaml。"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            if task_id == "daily":
                config.setdefault("scheduler", {})["run_time"] = time_str
            elif task_id == "optimize":
                config.setdefault("scheduler", {})["optimize_time"] = time_str
            else:
                # brief_reports 里找对应的 id
                for br in config.get("scheduler", {}).get("brief_reports", []):
                    if br.get("id") == task_id:
                        br["run_time"] = time_str
                        break

            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            logger.info(f"调度配置已写入: {self.config_path}")
        except Exception as e:
            logger.error(f"写入调度配置失败: {e}")

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
        except (ValueError, AttributeError):
            return None, None
