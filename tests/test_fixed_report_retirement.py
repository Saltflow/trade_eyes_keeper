"""Legacy fixed report settings cannot create or revive report execution."""

from copy import deepcopy

import pytest
import yaml

from src.core.schedule_manager import ScheduleManager
from src.interactive.assistant.config_tools import (
    ConfigProposal,
    ConfigurationTools,
    _revision,
)


@pytest.fixture
def legacy_project(tmp_path):
    config = {
        "stocks": [],
        "scheduler": {
            "timezone": "Asia/Shanghai",
            "daily_enabled": True,
            "run_on_startup": True,
            "run_time": "19:00",
            "daily_report_frequency": "off",
            "daily_report_triggers": {"obsolete": "invalid rules stay inert"},
            "data_collection_mode": "three_daily",
            "brief_reports": [
                {
                    "id": "morning_snapshot",
                    "enabled": True,
                    "run_time": "09:50",
                    "triggers": {
                        "mode": "any",
                        "conditions": [
                            {"path": "announcements", "operator": "non_empty"}
                        ],
                    },
                },
                {"id": "custom", "enabled": True, "run_time": "11:30"},
            ],
        },
    }
    path = tmp_path / "config/config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return tmp_path, path, config


def test_enabled_legacy_settings_never_schedule_reports(legacy_project, monkeypatch):
    _, path, config = legacy_project
    before = path.read_bytes()
    manager = ScheduleManager(config, path)
    start = manager.scheduler.start
    monkeypatch.setattr(manager.scheduler, "start", lambda: start(paused=True))
    manager.scheduler.add_job(lambda: None, "cron", hour=12, id="data_collection")
    manager.start()
    try:
        assert {job.id for job in manager.scheduler.get_jobs()} == {"data_collection"}
        manager.reload_config()
        assert {job.id for job in manager.scheduler.get_jobs()} == {"data_collection"}
        assert path.read_bytes() == before
        assert manager.config["scheduler"]["data_collection_mode"] == "three_daily"
    finally:
        manager.stop()


@pytest.mark.parametrize(
    "key,value",
    [
        ("scheduler.daily_enabled", False),
        ("scheduler.run_time", "20:30"),
        ("scheduler.daily_report_frequency", "weekly"),
        ("scheduler.daily_report_weekday", 2),
        ("scheduler.daily_report_triggers", {}),
        ("scheduler.brief_reports.morning_snapshot.enabled", False),
        ("scheduler.brief_reports.morning_snapshot.run_time", "10:30"),
        ("scheduler.brief_reports.morning_snapshot.triggers", {}),
    ],
)
def test_retired_settings_cannot_be_proposed(legacy_project, key, value):
    root, path, _ = legacy_project
    tools = ConfigurationTools(root)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="退役|不可修改"):
        tools.propose([{"key": key, "value": value}])
    assert path.read_bytes() == before


def test_pre_retirement_saved_proposal_is_rejected_without_rewrite(legacy_project):
    root, path, config = legacy_project
    effect = "确认保存后刷新运行中的调度器；未运行时在服务启动后生效"
    proposal = ConfigProposal(
        target="config/config.yaml",
        revision=_revision(config),
        changes=[{"key": "scheduler.run_time", "value": "20:30"}],
        diff=[
            {
                "key": "scheduler.run_time",
                "old": "19:00",
                "new": "20:30",
                "path": ["scheduler", "run_time"],
                "effect": effect,
                "affected_markets": ["a_share", "hk", "us"],
            }
        ],
        effect=effect,
        affected_markets=["a_share", "hk", "us"],
    ).to_dict()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="退役"):
        ConfigurationTools(root).apply(proposal)
    assert path.read_bytes() == before


def test_obsolete_jobs_removed_but_collection_retained(legacy_project, monkeypatch):
    _, path, config = legacy_project
    manager = ScheduleManager(config, path)
    start = manager.scheduler.start
    monkeypatch.setattr(manager.scheduler, "start", lambda: start(paused=True))
    for name in (
        "daily",
        "startup_task",
        "brief_custom",
        "optimize",
        "fundamental_quant",
    ):
        manager.scheduler.add_job(lambda: None, "cron", hour=12, id=name)
    manager.scheduler.add_job(lambda: None, "cron", hour=12, id="data_collection")
    manager.start()
    try:
        assert {job.id for job in manager.scheduler.get_jobs()} == {"data_collection"}
        changed = deepcopy(config)
        changed["scheduler"]["run_time"] = "also-invalid"
        path.write_text(yaml.safe_dump(changed), encoding="utf-8")
        manager.reload_config()
        assert {job.id for job in manager.scheduler.get_jobs()} == {"data_collection"}
    finally:
        manager.stop()
