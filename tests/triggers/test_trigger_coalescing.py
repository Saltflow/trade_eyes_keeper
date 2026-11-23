"""Synthetic multi-announcement reports use one full-pool run per kind/poll."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import report_execution as reports
from src.core.execution import ExecutionRuntime, ExecutionStore, RunResult
from src.triggers.delivery import binding_status
from src.triggers.facts import AnnouncementEvent, FactSnapshot, FactValue
from src.triggers.runtime import TriggerRuntime


@pytest.mark.parametrize("kinds", [("brief", "daily"), ("brief", "brief")])
def test_q3_coalescing_preserves_events_and_separate_action_indices(
    tmp_path, monkeypatch, kinds
):
    now = datetime(2026, 11, 2, 1, 50, tzinfo=timezone.utc)
    config = {"storage": {"data_dir": str(tmp_path)}, "stocks": ["601088", "00700"]}
    execution = ExecutionRuntime(ExecutionStore(reports.runtime_path(config)))
    observed = []

    def report(payload, ctx):
        routed = payload["config_snapshot"]
        assert binding_status(routed, run_id=ctx.run_id) == "allowed"
        observed.append(routed["_trigger_context"])
        return RunResult("succeeded", "ack", deliveries={"feishu": {"status": "sent"}})

    monkeypatch.setattr(reports, "_report_handler", report)
    trigger = TriggerRuntime(config, execution, clock=lambda: now)
    trigger.store.create(
        "alice",
        {
            "label": "三季报",
            "event": {
                "kind": "announcement.q3_report",
                "report_period": "Q3",
                "day_offsets": [0, 1],
            },
            "actions": [
                {"kind": kind, "payload": {"chat_id": "chat-a"}} for kind in kinds
            ],
        },
        "q3",
    )
    events = [
        AnnouncementEvent(
            id="test-" + code,
            kind="announcement.q3_report",
            symbol=code,
            occurred_at=now,
            published_at=now,
            report_period="2026Q3",
            source="synthetic",
            title="第三季度报告",
            url="https://example.invalid/" + code,
            publication_date=now.date(),
            observed_at=now,
        )
        for code in config["stocks"]
    ]
    snapshot = FactSnapshot(now, now, events=events)
    trigger.evaluator.evaluate(snapshot, now=now)
    trigger._submit_pending(config)
    actions = trigger.store.actions("alice")
    assert len(actions) == 4
    assert len({action["run_id"] for action in actions}) == 2
    execution.start()
    try:
        for run_id in {action["run_id"] for action in actions}:
            assert execution.wait(run_id, timeout=5)["status"] == "succeeded"
        trigger.reconcile()
    finally:
        execution.stop(drain=True)
    assert len(observed) == 2
    assert all(len(binding["action_ids"]) == 2 for binding in observed)
    assert all(
        "601088" in binding["reason"] and "00700" in binding["reason"]
        for binding in observed
    )
    assert all(
        action["state"] == "completed" for action in trigger.store.actions("alice")
    )

    # Same official events on the next poll cannot create a second batch.
    trigger.evaluator.evaluate(snapshot, now=now)
    trigger._submit_pending(config)
    assert len(trigger.store.actions("alice")) == 4


def test_price_slot_groups_by_owner_chat_and_report_with_individual_receipts(
    tmp_path, monkeypatch
):
    now = datetime(2026, 11, 23, 11, tzinfo=timezone(timedelta(hours=8)))
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "stocks": ["00883", "01816"],
        "scheduler": {"data_collection_mode": "three_daily", "run_time": "11:00"},
    }
    execution = ExecutionRuntime(ExecutionStore(reports.runtime_path(config)))
    deliveries = {"email": {"status": "sent"}, "feishu": {"status": "sent"}}
    observed = []

    def report(payload, ctx):
        routed = payload["config_snapshot"]
        assert binding_status(routed, run_id=ctx.run_id) == "allowed"
        observed.append(routed["_trigger_context"])
        return RunResult("succeeded", "ack", deliveries=deliveries)

    monkeypatch.setattr(reports, "_report_handler", report)
    fields = ["quotes.00883.last", "quotes.01816.last"]
    snapshot = FactSnapshot(now, now, {
        path: FactValue(20, "sampled", now, now) for path in fields
    })
    loader = Mock(return_value=snapshot)
    runtime = TriggerRuntime(config, execution, clock=lambda: now, fact_loader=loader)
    rules = [
        ("alice", "chat", "brief", fields[0]),
        ("alice", "chat", "brief", fields[1]),
        ("alice", "chat-b", "brief", fields[0]),
        ("alice", "chat", "daily", fields[0]),
        ("bob", "chat", "brief", fields[1]),
    ]
    for i, (owner, chat, kind, field) in enumerate(rules):
        runtime.store.create(owner, {
            "condition": {"field": field, "op": "lt", "value": 21},
            "actions": [{"kind": kind, "payload": {"chat_id": chat}}],
        }, str(i), now=now)
    ctx = SimpleNamespace(cancel_event=SimpleNamespace(is_set=lambda: False))
    assert runtime._evaluate({"collection_slot": now.isoformat()}, ctx).status == "succeeded"
    assert loader.call_count == 1
    actions = runtime.store.actions("alice") + runtime.store.actions("bob")
    run_ids = {a["run_id"] for a in actions}
    assert len(actions) == 5 and len(run_ids) == 4
    execution.start()
    try:
        for run_id in run_ids:
            assert execution.wait(run_id, timeout=5)["status"] == "succeeded"
        runtime.reconcile()
    finally:
        execution.stop(drain=True)
    shared = next(b for b in observed if len(b.get("action_ids", [])) == 2)
    assert all(s in shared["reason"] for s in ("#1", "#2", "00883", "01816", "21", "=20", "源时间"))
    for action in runtime.store.actions("alice") + runtime.store.actions("bob"):
        assert action["state"] == "completed"
        assert action["result"]["deliveries"] == deliveries

    # A newly added rule cannot fire from a previous cached sample on CRUD.
    runtime.store.create("bob", {
        "condition": {"field": fields[0], "op": "gt", "value": 10},
        "actions": [{"kind": "brief", "payload": {"chat_id": "chat"}}],
    }, "new", now=now)
    assert runtime._evaluate({"cached_only": True}, ctx).status == "succeeded"
    assert len(runtime.store.actions("bob")) == 1
    assert loader.call_count == 1


def test_shared_price_report_rechecks_every_member_revision(tmp_path):
    now = datetime(2026, 11, 23, 3, tzinfo=timezone.utc)
    config = {"storage": {"data_dir": str(tmp_path)}}
    execution = ExecutionRuntime(ExecutionStore(reports.runtime_path(config)))
    runtime = TriggerRuntime(config, execution, clock=lambda: now)
    path = "quotes.00883.last"
    snapshot = FactSnapshot(now, now, {path: FactValue(20, "sampled", now, now)})
    for limit in (21, 22):
        runtime.store.create("alice", {
            "condition": {"field": path, "op": "lt", "value": limit},
            "actions": [{"kind": "brief", "payload": {"chat_id": "chat"}}],
        }, str(limit), now=now)
    runtime.evaluator.evaluate(snapshot, now=now, collection_slot=now.isoformat())
    runtime._submit_pending(config)
    run_id = runtime.store.actions("alice")[0]["run_id"]
    with execution.store.connection() as db:
        db.execute("UPDATE execution_runs SET status='running' WHERE id=?", (run_id,))
    routed = execution.store.get(run_id)["payload"]["config_snapshot"]
    assert binding_status(routed, run_id=run_id) == "allowed"
    other = 3 - routed["_trigger_context"]["number"]
    runtime.store.update("alice", other, {"label": "changed"}, expected_revision=1)
    assert binding_status(routed, run_id=run_id) == "revoked"
