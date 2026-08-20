"""调度管理测试 — APScheduler 内嵌 health server。"""

import yaml

from src.core.schedule_manager import ScheduleManager


def _make_config(tmp_path):
    """构造最小可用 config，写到 tmp_path。"""
    config = {
        "scheduler": {
            "run_time": "19:00",
            "timezone": "Asia/Shanghai",
            "brief_reports": [
                {
                    "id": "morning_snapshot",
                    "label": "早盘",
                    "run_time": "09:50",
                    "enabled": True,
                },
                {
                    "id": "afternoon_snapshot",
                    "label": "收盘",
                    "run_time": "14:30",
                    "enabled": True,
                },
            ],
        },
        "health_server": {"enabled": True, "host": "0.0.0.0", "port": 1934},
        "storage": {"cache_dir": str(tmp_path)},
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True)
    return config, config_path


class TestScheduleManager:
    def test_registers_jobs_on_start(self, tmp_path):
        """启动时注册 4 个 job（日报+2简报+优化器）。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            jobs = mgr.scheduler.get_jobs()
            job_ids = [j.id for j in jobs]
            assert "daily" in job_ids
            assert "brief_morning_snapshot" in job_ids
            assert "brief_afternoon_snapshot" in job_ids
            assert "optimize" not in job_ids
        finally:
            mgr.stop()

    def test_optimizer_schedule_requires_explicit_enable(self, tmp_path):
        config, cfg_path = _make_config(tmp_path)
        config["scheduler"]["optimize_enabled"] = True
        with open(cfg_path, "w", encoding="utf-8") as handle:
            yaml.dump(config, handle, allow_unicode=True)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            job = mgr.scheduler.get_job("optimize")
            assert job is not None
            assert job.next_run_time.hour == 2
            assert job.next_run_time.minute == 0
        finally:
            mgr.stop()

    def test_daily_job_time_correct(self, tmp_path):
        """日报 job 的 trigger 时间正确。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            job = mgr.scheduler.get_job("daily")
            assert job is not None
            # next_run_time 应该在 19:00
            assert job.next_run_time.hour == 19
            assert job.next_run_time.minute == 0
            assert job.misfire_grace_time == 3600
            assert job.coalesce is True
            assert job.max_instances == 1
        finally:
            mgr.stop()

    def test_brief_misfire_grace_is_configurable(self, tmp_path):
        config, cfg_path = _make_config(tmp_path)
        config["scheduler"]["brief_misfire_grace_seconds"] = 600
        config["scheduler"]["brief_reports"][0][
            "misfire_grace_seconds"
        ] = 1200
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            morning = mgr.scheduler.get_job("brief_morning_snapshot")
            afternoon = mgr.scheduler.get_job("brief_afternoon_snapshot")
            assert morning.misfire_grace_time == 1200
            assert afternoon.misfire_grace_time == 600
            assert morning.coalesce is True
            assert morning.max_instances == 1
        finally:
            mgr.stop()

    def test_invalid_misfire_grace_uses_safe_defaults(self, tmp_path):
        config, cfg_path = _make_config(tmp_path)
        config["scheduler"]["daily_misfire_grace_seconds"] = 0
        config["scheduler"]["brief_misfire_grace_seconds"] = "invalid"
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            assert mgr.scheduler.get_job("daily").misfire_grace_time == 3600
            assert (
                mgr.scheduler.get_job("brief_morning_snapshot").misfire_grace_time
                == 900
            )
        finally:
            mgr.stop()

    def test_get_schedule(self, tmp_path):
        """get_schedule 返回当前调度信息。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            sched = mgr.get_schedule()
            assert isinstance(sched, list)
            ids = [s["id"] for s in sched]
            assert "daily" in ids
            daily = next(s for s in sched if s["id"] == "daily")
            assert "19:00" in daily["time"]
        finally:
            mgr.stop()

    def test_reschedule_daily(self, tmp_path):
        """修改日报时间 → 立即生效。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            ok = mgr.reschedule("daily", "20:30")
            assert ok
            job = mgr.scheduler.get_job("daily")
            assert job.next_run_time.hour == 20
            assert job.next_run_time.minute == 30
        finally:
            mgr.stop()

    def test_reschedule_persists_to_config(self, tmp_path):
        """修改后写入 config.yaml。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            mgr.reschedule("daily", "20:30")
        finally:
            mgr.stop()
        with open(cfg_path, "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["scheduler"]["run_time"] == "20:30"

    def test_reschedule_invalid_time(self, tmp_path):
        """无效时间 → 返回 False。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            assert mgr.reschedule("daily", "25:00") is False
            assert mgr.reschedule("daily", "abc") is False
        finally:
            mgr.stop()

    def test_reschedule_invalid_task(self, tmp_path):
        """无效任务名 → 返回 False。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            assert mgr.reschedule("lunch", "12:00") is False
        finally:
            mgr.stop()

    def test_reschedule_brief(self, tmp_path):
        """修改简报时间。"""
        config, cfg_path = _make_config(tmp_path)
        mgr = ScheduleManager(config, config_path=cfg_path)
        mgr.start()
        try:
            ok = mgr.reschedule("morning_snapshot", "09:15")
            assert ok
            job = mgr.scheduler.get_job("brief_morning_snapshot")
            assert job.next_run_time.hour == 9
            assert job.next_run_time.minute == 15
        finally:
            mgr.stop()
