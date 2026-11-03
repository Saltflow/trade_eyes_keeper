"""Reuse personal-monitor slots without repeated network reads or clock delays."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from zoneinfo import ZoneInfo

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.report_execution import runtime_path
from src.triggers.facts import FactSnapshot
from src.triggers.runtime import TriggerRuntime


def test_three_collection_slots_survive_restart_and_disabled_reports(tmp_path):
    current = [datetime(2026, 11, 3, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))]
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "scheduler": {
            "data_collection_mode": "three_daily",
            "daily_enabled": False,
            "run_time": "19:00",
            "brief_reports": [
                {"run_time": "09:50", "enabled": False},
                {"run_time": "14:30", "enabled": False},
            ],
        },
    }
    execution = ExecutionRuntime(ExecutionStore(runtime_path(config)))
    loader = Mock(side_effect=lambda *a, **kw: FactSnapshot(current[0], current[0]))
    runtime = TriggerRuntime(
        config, execution, clock=lambda: current[0], fact_loader=loader
    )
    runtime.store.create(
        "fixture",
        {
            "condition": {"field": "quotes.601088.last", "op": "lt", "value": 1},
            "actions": [{"kind": "daily"}],
        },
        "price",
    )
    runtime.started = True
    ctx = SimpleNamespace(cancel_event=SimpleNamespace(is_set=lambda: False))
    for hour, minute in [(9, 50), (14, 30), (19, 0)]:
        current[0] = current[0].replace(hour=hour, minute=minute)
        runtime._timer()
        row = execution.store.get(runtime._pending_run)
        runtime._evaluate(row["payload"], ctx)
        # Simulate the completed worker receipt, then poll the same slot again.
        with execution.store.connection() as db:
            db.execute(
                "UPDATE execution_runs SET status='succeeded' WHERE id=?", (row["id"],)
            )
        current[0] += timedelta(seconds=30)
        runtime._timer()
    assert loader.call_count == 3
    reboot = TriggerRuntime(
        config, execution, clock=lambda: current[0], fact_loader=loader
    )
    reboot.started = True
    reboot._timer()
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM execution_runs").fetchone()[0] == 3
    current[0] = current[0].replace(hour=22)
    reboot.kick()
    reboot._timer()
    reboot._evaluate(execution.store.get(reboot._pending_run)["payload"], ctx)
    loader.assert_called()
    assert loader.call_count == 3
    assert reboot.health()["next_collection_at"] == "2026-11-04T09:50:00+08:00"
    reboot.store.create(
        "fixture",
        {
            "schedule": {"at": current[0].isoformat()},
            "actions": [{"kind": "daily", "payload": {"chat_id": "fixture-chat"}}],
        },
        "due",
        now=current[0] - timedelta(days=1),
    )
    reboot._timer()
    with execution.store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM execution_runs WHERE job_id='triggers.due'"
            ).fetchone()[0]
            == 1
        )
    assert loader.call_count == 3
