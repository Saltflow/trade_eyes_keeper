"""Scheduler lifecycle preserves independent jobs, never legacy report timers."""

from unittest.mock import Mock

import pytest
import yaml

from src.core.schedule_manager import ScheduleManager, get_schedule_manager


def test_scheduler_lifecycle_and_readonly_inventory(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"scheduler": {"timezone": "Asia/Shanghai"}}))
    manager = ScheduleManager({"scheduler": {"timezone": "Asia/Shanghai"}}, path)
    manager.scheduler.add_job(lambda: None, "cron", hour=12, id="data_collection")
    manager.start()
    try:
        assert get_schedule_manager() is manager
        assert [row["id"] for row in manager.get_schedule()] == ["data_collection"]
        assert manager.get_schedule()[0]["timezone"] == "Asia/Shanghai"
        manager.start()
        assert len(manager.scheduler.get_jobs()) == 1
    finally:
        manager.stop()
    assert get_schedule_manager() is None


def test_invalid_timezone_falls_back_without_creating_reports():
    manager = ScheduleManager({"scheduler": {"timezone": "invalid"}})
    assert str(manager.timezone) == "Asia/Shanghai"
    assert not manager.scheduler.get_jobs()


def test_reload_updates_existing_report_worker_config_only(tmp_path, monkeypatch):
    config = {"scheduler": {"daily_enabled": True}, "stocks": ["600036"]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    runtime = object()
    register = Mock()
    monkeypatch.setattr("src.core.report_execution.register_report_jobs", register)
    manager = ScheduleManager(config, path, execution_runtime=runtime)
    manager.start()
    try:
        changed = {**config, "stocks": ["600519"]}
        path.write_text(yaml.safe_dump(changed))
        manager.reload_config()
        register.assert_called_once_with(runtime, path, config=manager.config)
        assert manager.config["stocks"] == ["600519"]
        assert manager.scheduler.get_jobs() == []
    finally:
        manager.stop()


def test_stopped_scheduler_reload_is_rejected():
    manager = ScheduleManager({})
    with pytest.raises(RuntimeError, match="尚未启动"):
        manager.reload_config()


def test_fixed_schedule_creation_and_dispatch_apis_are_removed():
    for name in (
        "_add_job",
        "_job_runner",
        "_scheduled_specs",
        "reschedule",
        "_persist_schedule",
    ):
        assert not hasattr(ScheduleManager, name)
