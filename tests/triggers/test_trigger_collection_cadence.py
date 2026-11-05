"""Reuse personal-monitor slots without repeated network reads or clock delays."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from zoneinfo import ZoneInfo

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.report_execution import runtime_path
from src.triggers.facts import FactSnapshot
from src.triggers.runtime import TriggerRuntime


def test_delayed_sample_evaluates_with_original_clock_and_report_provenance(tmp_path):
    from src.data.market_calendar import MarketSession
    from src.triggers import TriggerEvaluator, TriggerStore
    from src.triggers.facts import load_trigger_facts

    now = datetime(2026, 11, 5, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = now.replace(minute=34, second=56)
    config = {"stocks": ["00883"], "scheduler": {"data_collection_mode": "three_daily"}}
    session = MarketSession(now.replace(hour=9, minute=30), now.replace(hour=16, minute=0))
    providers = {"quote": lambda code: {"close": 22.8, "quote_time": source.isoformat()},
                 "market_session": lambda code, **kw: session}
    snapshot = load_trigger_facts({"quotes.00883.last"}, config, as_of=now, clock=lambda: now, providers=providers)
    fact = snapshot.resolve("quotes.00883.last")
    assert fact.status == "sampled" and fact.as_of == source
    assert snapshot.resolve("quotes.00883.fresh").value is False
    store = TriggerStore(tmp_path / "sample.sqlite3")
    store.create("sample", {"condition": {"field": "quotes.00883.last", "op": "lt",
                        "value": 23.28, "max_age_seconds": 300},
                        "actions": [{"kind": "daily"}]}, "sample", now=now)
    evaluator = TriggerEvaluator(store)
    actions = evaluator.evaluate(snapshot, now=now)
    assert len(actions) == 1
    sample = actions[0]["context"]["quote_samples"]["quotes.00883.last"]
    assert sample["source_time"] == source.isoformat() and sample["value"] == 22.8
    runtime = object.__new__(TriggerRuntime);runtime.config_path = tmp_path / "config.yaml"
    assert source.isoformat() in runtime._binding(actions[0], "sample")["reason"]
    assert "非实时" in runtime._binding(actions[0], "sample")["reason"]
    evaluator.evaluate(snapshot, now=now + timedelta(minutes=6))
    assert evaluator.last_outcomes[0]["truth"] == "unknown"
    # End-of-day collection accepts today's latest closing auction, never yesterday.
    for clock, quote_clock, expected in [
        (now.replace(hour=19, minute=0), now.replace(hour=16, minute=8), "sampled"),
        (now, source - timedelta(days=1), "stale"),
        (now, now + timedelta(seconds=1), "stale"),
        (now, now - timedelta(minutes=21), "stale"),
        (now, source.replace(hour=8), "stale"),
    ]:
        providers["quote"] = lambda code, stamp=quote_clock: {"close": 22.8, "quote_time": stamp.isoformat()}
        sample_snapshot = load_trigger_facts({"quotes.00883.last"}, config, as_of=clock, providers=providers)
        assert sample_snapshot.resolve("quotes.00883.last").status == expected


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
