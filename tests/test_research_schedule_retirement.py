"""Retired research and timed optimization cannot revive automatic execution."""

from copy import deepcopy

import pytest
import yaml

from src.core.schedule_manager import (
    ScheduleManager,
    get_schedule_manager,
    set_schedule_manager,
)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    config = {
        "scheduler": {
            "timezone": "America/New_York",
            "daily_enabled": True,
            "run_time": "19:00",
            "optimize_enabled": True,
            "brief_reports": [{"id": "morning_snapshot", "run_time": "09:50"}],
        },
        "fundamental_quant": {
            "enabled": True,
            "materials": {"enabled": True},
            "schedule": {"enabled": True, "daily_time": "20:00"},
            "notifications": {"enabled": True},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    current = ScheduleManager(config, config_path=path)
    original_start = current.scheduler.start
    monkeypatch.setattr(current.scheduler, "start", lambda: original_start(paused=True))

    def unexpected_process(*_args, **_kwargs):
        pytest.fail("retirement tests must not launch a subprocess")

    monkeypatch.setattr("subprocess.Popen", unexpected_process)
    set_schedule_manager(None)
    yield current
    current.stop()
    assert get_schedule_manager() is None


def job_ids(manager):
    return {job.id for job in manager.scheduler.get_jobs()}


def test_enabled_stale_research_config_never_registers_jobs(manager):
    expected = set()
    manager.start()
    assert job_ids(manager) == expected
    for job in manager.scheduler.get_jobs():
        assert str(job.trigger.timezone) == "America/New_York"
    assert not hasattr(ScheduleManager, "_fundamental_specs")


def test_stale_brief_alias_is_filtered_without_changing_other_jobs(manager):
    manager.config["scheduler"]["brief_reports"].extend(
        [
            {"id": "fundamental_quant", "run_time": "20:00"},
            {"id": "fundamental_quant_weekly", "run_time": "09:00"},
            {"id": "optimize", "run_time": "02:00"},
            {"id": "custom", "run_time": "11:30"},
        ]
    )
    expected = set()
    manager.start()
    assert job_ids(manager) == expected


@pytest.mark.parametrize(
    "legacy_id",
    ["fundamental_quant", "fundamental_quant_weekly", "optimize", "brief_optimize"],
)
def test_startup_removes_previously_registered_legacy_jobs(manager, legacy_id):
    manager.scheduler.add_job(lambda: None, "cron", hour=20, id=legacy_id)
    manager.start()
    assert job_ids(manager) == set()


def test_reload_removes_legacy_jobs_preserves_unowned_and_disables_daily(manager):
    manager.start()
    for legacy_id in (
        "fundamental_quant",
        "fundamental_quant_weekly",
        "fundamental_quant_startup",
        "optimize",
        "brief_optimize",
    ):
        manager.scheduler.add_job(lambda: None, "cron", hour=20, id=legacy_id)
    manager.scheduler.add_job(lambda: None, "cron", hour=12, id="independent")
    saved = deepcopy(manager.config)
    saved["scheduler"].update(daily_enabled=False, run_on_startup=True)
    manager.config_path.write_text(yaml.safe_dump(saved), encoding="utf-8")
    manager.reload_config()
    assert job_ids(manager) == {"independent"}
    assert manager.config["fundamental_quant"]["schedule"]["enabled"] is True


def test_reload_does_not_restore_legacy_jobs_when_new_config_is_invalid(manager):
    manager.start()
    for legacy_id in ("fundamental_quant", "optimize", "brief_optimize"):
        manager.scheduler.add_job(lambda: None, "cron", hour=20, id=legacy_id)
    saved = deepcopy(manager.config)
    saved["scheduler"]["run_time"] = "invalid"
    manager.config_path.write_text(yaml.safe_dump(saved), encoding="utf-8")
    manager.reload_config()
    assert job_ids(manager) == set()
