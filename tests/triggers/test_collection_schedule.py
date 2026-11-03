"""Three source slots survive restart; clock/cached work performs no fetching."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from zoneinfo import ZoneInfo

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.report_execution import runtime_path
from src.triggers.collection_schedule import (
    collection_schedule,
    due_collection_slot,
    next_collection_slot,
)
from src.triggers.facts import FactSnapshot, FactValue
from src.triggers.runtime import TriggerRuntime

ZONE = ZoneInfo("Asia/Shanghai")
CTX = SimpleNamespace(cancel_event=SimpleNamespace(is_set=lambda: False))


def setup_runtime(tmp_path, current, loader):
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "stocks": ["601088"],
        "scheduler": {
            "timezone": "Asia/Shanghai",
            "daily_enabled": False,
            "brief_reports": [{"enabled": False}],
            "data_collection_times": ["09:50", "14:30", "19:00"],
        },
    }
    execution = ExecutionRuntime(ExecutionStore(runtime_path(config)))
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
    return runtime, execution, config


def run_rows(execution):
    with execution.store.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM execution_runs")]


def test_reports_disabled_still_collect_exactly_three_slots_and_no_repeat(tmp_path):
    current = [datetime(2026, 11, 3, 9, 50, tzinfo=ZONE)]

    def load(dependencies, config, **kwargs):
        return FactSnapshot(
            current[0],
            current[0],
            facts={
                "quotes.601088.last": FactValue(49, "available", current[0], current[0])
            },
        )

    loader = Mock(side_effect=load)
    runtime, execution, config = setup_runtime(tmp_path, current, loader)
    for hour, minute in [(9, 50), (14, 30), (19, 0)]:
        current[0] = current[0].replace(hour=hour, minute=minute)
        runtime._timer()
        row = execution.store.get(runtime._pending_run)
        assert row["source"] == "scheduled_data_collection"
        assert runtime._evaluate(row["payload"], CTX).status == "succeeded"
        # Complete the synthetic worker receipt before advancing the clock.
        with execution.store.connection() as db:
            db.execute(
                "UPDATE execution_runs SET status='succeeded' WHERE id=?", (row["id"],)
            )
        for _ in range(3):
            current[0] += timedelta(seconds=30)
            runtime._timer()
    assert loader.call_count == 3
    assert len(run_rows(execution)) == 3

    reboot = TriggerRuntime(
        config, execution, clock=lambda: current[0], fact_loader=loader
    )
    reboot.started = True
    reboot._timer()
    row = execution.store.get(reboot._pending_run)
    assert row["payload"] == {"cached_only": True}
    reboot._evaluate(row["payload"], CTX)
    assert loader.call_count == 3
    assert (
        len(
            [
                r
                for r in run_rows(execution)
                if r["source"] == "scheduled_data_collection"
            ]
        )
        == 3
    )


def test_restart_and_crud_outside_slots_do_not_fetch(tmp_path):
    current = [datetime(2026, 11, 3, 22, 0, tzinfo=ZONE)]
    loader = Mock(side_effect=AssertionError("unexpected network collection"))
    runtime, execution, _ = setup_runtime(tmp_path, current, loader)
    runtime._timer()
    row = execution.store.get(runtime._pending_run)
    assert runtime._evaluate(row["payload"], CTX).status == "succeeded"
    runtime.kick()
    assert runtime._evaluate({}, CTX).status == "succeeded"
    loader.assert_not_called()
    assert runtime.health()["next_collection_at"] == "2026-11-04T09:50:00+08:00"


def test_date_trigger_clock_is_admitted_between_collection_slots(tmp_path):
    current = [datetime(2026, 11, 3, 21, 0, tzinfo=ZONE)]
    loader = Mock(side_effect=AssertionError("clock must not fetch"))
    runtime, execution, _ = setup_runtime(tmp_path, current, loader)
    runtime.store.create(
        "fixture",
        {
            "schedule": {"at": current[0].isoformat()},
            "actions": [{"kind": "daily", "payload": {"chat_id": "fixture-chat"}}],
        },
        "date",
        now=current[0] - timedelta(days=1),
    )
    runtime._timer()
    assert any(row["job_id"] == "triggers.due" for row in run_rows(execution))
    loader.assert_not_called()


def test_slow_publication_reuses_quote_clock_without_another_fetch(tmp_path):
    current = [datetime(2026, 11, 3, 9, 52, tzinfo=ZONE)]
    loader = Mock(side_effect=AssertionError("cached publication fetched a source"))
    runtime, _, config = setup_runtime(tmp_path, current, loader)
    quote_time = current[0] - timedelta(minutes=2)
    runtime._snapshot = FactSnapshot(
        quote_time,
        quote_time,
        facts={
            "quotes.601088.last": FactValue(49, "available", quote_time, quote_time)
        },
    )
    slow = FactSnapshot(current[0], current[0])
    slow.facts["stocks_data.601088.ma60"] = FactValue(
        45, "available", quote_time, quote_time
    )
    dependencies = {"quotes.601088.last", "stocks_data.601088.ma60"}
    runtime._slow_snapshot = slow
    runtime._slow_loaded_key = runtime._facts_key(
        {"stocks_data.601088.ma60"}, config, {}
    )
    cached = runtime._cached_facts(config, dependencies)
    assert cached.resolve("quotes.601088.last").as_of == quote_time
    assert cached.resolve("stocks_data.601088.ma60").value == 45
    runtime._slow_loaded_key = "old-configuration"
    runtime._snapshot = cached
    assert (
        runtime._cached_facts(config, dependencies)
        .resolve("stocks_data.601088.ma60")
        .status
        == "unknown"
    )
    loader.assert_not_called()


def test_slots_use_configured_zone_and_bounded_misfire_window():
    config = {"scheduler": {"data_collection_times": ["09:50", "14:30", "19:00"]}}
    assert (
        due_collection_slot(
            config, datetime.fromisoformat("2026-11-03T01:50:30+00:00")
        ).isoformat()
        == "2026-11-03T09:50:00+08:00"
    )
    assert (
        due_collection_slot(config, datetime.fromisoformat("2026-11-03T10:00:00+08:00"))
        is None
    )
    assert (
        next_collection_slot(
            config, datetime.fromisoformat("2026-11-03T20:00:00+08:00")
        ).isoformat()
        == "2026-11-04T09:50:00+08:00"
    )


@pytest.mark.parametrize("values", [[], ["9:50"], ["24:00"], ["09:50", "09:50"]])
def test_invalid_schedule_fails_closed(values):
    with pytest.raises(ValueError):
        collection_schedule({"scheduler": {"data_collection_times": values}})
