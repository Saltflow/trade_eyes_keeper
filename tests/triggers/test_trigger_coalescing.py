"""Synthetic multi-announcement reports use one full-pool run per kind/poll."""

from datetime import datetime, timezone

import pytest

from src.core import report_execution as reports
from src.core.execution import ExecutionRuntime, ExecutionStore, RunResult
from src.triggers.delivery import binding_status
from src.triggers.facts import AnnouncementEvent, FactSnapshot
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
