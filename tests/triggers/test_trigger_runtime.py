"""Synthetic trigger receipts exercise the real shared queue without networks."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src.core import report_execution as reports
from src.core.execution import ExecutionRuntime, ExecutionStore, RunResult
from src.notification.manager import NotifierManager
from src.triggers.delivery import TriggerChatNotifier, binding_status
from src.triggers.facts import FactSnapshot, FactValue
from src.triggers.runtime import TriggerRuntime

NOW = datetime(2026, 11, 2, 1, 50, tzinfo=timezone.utc)


@pytest.fixture
def profile(tmp_path):
    return {
        "storage": {"data_dir": str(tmp_path)},
        # Synthetic unit pool only; no report/HTML/business system is run.
        "stocks": ["601088", "00700", "VOO"],
        "interactive": {
            "feishu": {
                "enabled": True,
                "allowed_chat_ids": ["chat-a", "chat-b"],
                "app_id": "test-app",
                "app_secret": "test-secret",
            }
        },
        "notification": {"email": {"enabled": True}, "feishu": {"enabled": True}},
    }


def make_runtime(config, **kwargs):
    execution = ExecutionRuntime(ExecutionStore(reports.runtime_path(config)))
    path = execution.store.path.parent.parent / "trigger-test.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    trigger = TriggerRuntime(
        config, execution, clock=lambda: NOW, config_path=path, **kwargs
    )
    return trigger, execution


def dated(trigger, *, kinds=("reminder",), key="date", owner="alice", chat="chat-a"):
    return trigger.store.create(
        owner,
        {
            "label": "逆回购到期",
            "schedule": {"at": NOW.isoformat()},
            "actions": [
                {"kind": kind, "payload": {"chat_id": chat, "text": "逆回购到期"}}
                for kind in kinds
            ],
        },
        key,
        now=NOW - timedelta(days=1),
    )


def admit(trigger):
    trigger.evaluator.evaluate(None, now=NOW)
    trigger._submit_pending(trigger.config)
    return trigger.store.actions("alice")


def run_and_settle(trigger, execution, action):
    execution.start()
    try:
        result = RunResult.from_dict(
            execution.wait(action["run_id"], timeout=5)["result"]
        )
        trigger.reconcile()
        return result
    finally:
        execution.stop(drain=True)


def test_atomic_admission_restart_and_unknown_never_replay(profile):
    notifier = Mock()
    notifier.send_reminder.return_value = {"status": "unknown", "reason": "timeout"}
    trigger, execution = make_runtime(profile, notifier_factory=lambda _: notifier)
    dated(trigger)
    action = admit(trigger)[0]
    assert action["state"] == "submitted"
    assert run_and_settle(trigger, execution, action).status == "unknown"
    assert trigger.store.actions("alice")[0]["state"] == "uncertain"
    reboot, _ = make_runtime(profile, notifier_factory=lambda _: notifier)
    admit(reboot)
    reboot.reconcile()
    assert len(reboot.store.actions("alice")) == 1
    notifier.send_reminder.assert_called_once()


def test_atomic_enqueue_failure_rolls_back_claim(profile, monkeypatch):
    trigger, execution = make_runtime(profile)
    dated(trigger)
    trigger.evaluator.evaluate(None, now=NOW)
    original = execution.store.submit
    monkeypatch.setattr(
        execution.store, "submit", Mock(side_effect=RuntimeError("synthetic crash"))
    )
    with pytest.raises(RuntimeError):
        trigger._submit_pending(profile)
    assert trigger.store.actions("alice")[0]["state"] == "pending"
    monkeypatch.setattr(execution.store, "submit", original)
    trigger._submit_pending(profile)
    assert trigger.store.actions("alice")[0]["state"] == "submitted"


def test_delete_after_enqueue_before_send_blocks_reminder(profile):
    notifier = Mock()
    trigger, execution = make_runtime(profile, notifier_factory=lambda _: notifier)
    dated(trigger)
    action = admit(trigger)[0]
    trigger.store.remove("alice", [1])
    result = run_and_settle(trigger, execution, action)
    assert result.status == "skipped"
    assert result.reason == "trigger_revoked"
    notifier.send_reminder.assert_not_called()


def test_edit_queued_revision_blocks_old_action(profile):
    notifier = Mock()
    trigger, execution = make_runtime(profile, notifier_factory=lambda _: notifier)
    item = dated(trigger)
    action = admit(trigger)[0]
    trigger.store.update("alice", 1, {"label": "新提醒"}, item["revision"])
    assert run_and_settle(trigger, execution, action).reason == "trigger_revoked"
    notifier.send_reminder.assert_not_called()


def test_report_actions_reuse_report_lane_full_pool_and_private_route(
    profile, monkeypatch
):
    observed = []

    def report(payload, ctx):
        config = payload["config_snapshot"]
        assert binding_status(config, run_id=ctx.run_id) == "allowed"
        observed.append(payload)
        return RunResult(
            "succeeded", "mock_report", deliveries={"feishu": {"status": "sent"}}
        )

    monkeypatch.setattr(reports, "_report_handler", report)
    trigger, execution = make_runtime(profile)
    dated(trigger, kinds=("brief", "daily"))
    actions = admit(trigger)
    execution.start()
    try:
        for action in actions:
            assert execution.wait(action["run_id"], timeout=5)["status"] == "succeeded"
        trigger.reconcile()
    finally:
        execution.stop(drain=True)
    assert {p["kind"] for p in observed} == {"brief", "daily"}
    for payload in observed:
        cfg = payload["config_snapshot"]
        assert cfg["stocks"] == profile["stocks"]
        assert cfg["_trigger_context"]["chat_id"] == "chat-a"
        assert "test-secret" not in str(payload)
        assert payload["force"] is True
        assert "#1" in cfg["_trigger_context"]["reason"]
    assert all(a["state"] == "completed" for a in trigger.store.actions("alice"))


def test_current_chat_notifier_never_constructs_broadcast_channels(
    profile, monkeypatch
):
    from src.notification import email_notifier, feishu_notifier

    monkeypatch.setattr(
        email_notifier,
        "EmailNotifier",
        Mock(side_effect=AssertionError("broadcast email")),
    )
    monkeypatch.setattr(
        feishu_notifier,
        "FeishuNotifier",
        Mock(side_effect=AssertionError("broadcast webhook")),
    )
    routed = {**profile, "_trigger_context": {"chat_id": "chat-a"}}
    notifier = NotifierManager(routed)
    assert notifier.email is None and notifier.telegram is None
    assert isinstance(notifier.feishu, TriggerChatNotifier)


def test_forged_route_cannot_borrow_someone_elses_action(profile):
    trigger, execution = make_runtime(profile)
    dated(trigger)
    action = admit(trigger)[0]
    original = execution.store.get(action["run_id"])["payload"]["binding"]
    assert (
        binding_status(
            {**profile, "_trigger_context": {**original, "chat_id": "chat-b"}}
        )
        == "invalid"
    )
    assert (
        binding_status({**profile, "_trigger_context": {**original, "owner": "bob"}})
        == "invalid"
    )


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("success", "succeeded"),
        ("timeout", "unknown"),
        ("invalid_ack", "unknown"),
        ("reject", "failed"),
    ],
)
def test_stateless_chat_http_has_classified_receipts(
    profile, monkeypatch, outcome, expected
):
    from src.triggers import delivery

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if "/auth/" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"code": 0, "tenant_access_token": "synthetic-token"},
            )
        if outcome == "timeout":
            raise delivery.requests.ReadTimeout()
        data = {"code": 0, "data": {"message_id": "test-message"}}
        if outcome == "invalid_ack":
            data = {}
        if outcome == "reject":
            data = {"code": 999}
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr(delivery.requests, "post", post)
    trigger, execution = make_runtime(profile)
    dated(trigger)
    action = admit(trigger)[0]
    result = run_and_settle(trigger, execution, action)
    assert result.status == expected
    assert len(calls) == 2
    assert calls[1][1]["json"]["receive_id"] == "chat-a"
    assert "逆回购到期" in calls[1][1]["json"]["content"]
    assert calls[1][1]["allow_redirects"] is False
    if outcome == "success":
        assert result.deliveries["feishu"]["platform_message_id"] == "test-message"


def test_delete_while_auth_in_flight_prevents_external_message(profile, monkeypatch):
    from src.triggers import delivery

    trigger, execution = make_runtime(profile)
    dated(trigger)
    action = admit(trigger)[0]
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        trigger.store.remove("alice", [1])
        return SimpleNamespace(
            status_code=200, json=lambda: {"code": 0, "tenant_access_token": "test"}
        )

    monkeypatch.setattr(delivery.requests, "post", post)
    assert run_and_settle(trigger, execution, action).status == "skipped"
    assert len(calls) == 1


def test_idle_timer_does_not_write_runs_and_future_date_has_no_provider_work(profile):
    trigger, execution = make_runtime(
        profile, fact_loader=Mock(side_effect=AssertionError("source queried"))
    )
    trigger.started = True
    trigger._timer()
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM execution_runs").fetchone()[0] == 0
    trigger.store.create(
        "alice",
        {
            "label": "未来",
            "schedule": {"at": (NOW + timedelta(days=18)).isoformat()},
            "actions": [{"kind": "reminder", "payload": {"chat_id": "chat-a"}}],
        },
        "future",
    )
    trigger._timer()
    with execution.store.connection() as db:
        assert db.execute("SELECT count(*) FROM execution_runs").fetchone()[0] == 0


def test_current_allowlist_revocation_during_auth_prevents_send(profile, monkeypatch):
    from src.triggers import delivery

    trigger, execution = make_runtime(profile)
    dated(trigger)
    action = admit(trigger)[0]
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        current = yaml.safe_load(trigger.config_path.read_text(encoding="utf-8"))
        current["interactive"]["feishu"]["allowed_chat_ids"] = ["chat-b"]
        trigger.config_path.write_text(yaml.safe_dump(current), encoding="utf-8")
        return SimpleNamespace(
            status_code=200, json=lambda: {"code": 0, "tenant_access_token": "test"}
        )

    monkeypatch.setattr(delivery.requests, "post", post)
    result = run_and_settle(trigger, execution, action)
    assert result.status == "failed"
    assert result.reason == "trigger_chat_unauthorized"
    assert len(calls) == 1


def test_minimal_fact_dependencies_latest_price_edge_and_recovery(profile, monkeypatch):
    from src.triggers import snapshots

    monkeypatch.setattr(snapshots, "load_report_snapshot", lambda cfg: {})
    calls = []
    price = [51]

    def loader(dependencies, config, **kwargs):
        calls.append(set(dependencies))
        return FactSnapshot(
            NOW,
            NOW,
            facts={
                "quotes.601088.last": FactValue(
                    price[0],
                    "available",
                    NOW,
                    NOW,
                    "synthetic",
                    path="quotes.601088.last",
                )
            },
        )

    trigger, _execution = make_runtime(profile, fact_loader=loader)
    trigger.store.create(
        "alice",
        {
            "label": "跌破50",
            "condition": {"field": "quotes.601088.last", "op": "lt", "value": 50},
            "actions": [{"kind": "brief", "payload": {"chat_id": "chat-a"}}],
        },
        "price",
    )
    ctx = SimpleNamespace(cancel_event=SimpleNamespace(is_set=lambda: False))
    assert trigger._evaluate({}, ctx).status == "succeeded"
    assert trigger.store.actions("alice") == []
    price[0] = 49
    assert trigger._evaluate({}, ctx).status == "succeeded"
    assert trigger._evaluate({}, ctx).status == "succeeded"
    assert len(trigger.store.actions("alice")) == 1
    assert calls == [{"quotes.601088.last"}] * 3


def test_slow_free_indicator_is_not_overwritten_by_empty_fast_report_snapshot(
    profile, monkeypatch
):
    from src.triggers import snapshots

    monkeypatch.setattr(snapshots, "load_report_snapshot", lambda cfg: {})

    def loader(dependencies, config, **kwargs):
        snapshot = FactSnapshot(NOW, NOW)
        snapshot.facts["stocks_data.601088.ma60"] = FactValue(
            45 if "stocks_data.601088.ma60" in dependencies else None,
            "available" if "stocks_data.601088.ma60" in dependencies else "unknown",
            NOW,
            NOW,
        )
        if "quotes.601088.last" in dependencies:
            snapshot.facts["quotes.601088.last"] = FactValue(49, "available", NOW, NOW)
        return snapshot

    trigger, _execution = make_runtime(profile, fact_loader=loader)
    # A completed independent slow job publishes its source-bearing snapshot.
    trigger._slow_snapshot = loader({"stocks_data.601088.ma60"}, profile)
    trigger._slow_loaded_key = trigger._facts_key(
        {"stocks_data.601088.ma60"}, profile, {}
    )
    facts = trigger._load_facts(
        profile, {"stocks_data.601088.ma60", "quotes.601088.last"}
    )
    assert facts.resolve("stocks_data.601088.ma60").value == 45
    assert facts.resolve("stocks_data.601088.ma60").status == "available"


def test_disabled_reports_do_not_block_independent_reminders(profile):
    notifier = Mock()
    notifier.send_reminder.return_value = {"status": "sent", "reason": "ack"}
    trigger, execution = make_runtime(profile, notifier_factory=lambda _: notifier)
    dated(trigger, kinds=("brief", "reminder"))
    reports.register_report_jobs(execution, config=profile)
    execution.disable("reports")
    actions = admit(trigger)
    assert actions[0]["state"] == "pending"
    assert actions[1]["state"] == "submitted"
    assert run_and_settle(trigger, execution, actions[1]).status == "succeeded"


def test_due_clock_lane_delivers_while_market_collection_is_blocked(
    profile, monkeypatch
):
    from src.triggers import snapshots

    monkeypatch.setattr(snapshots, "load_report_snapshot", lambda cfg: {})
    collecting, release, sent = threading.Event(), threading.Event(), threading.Event()

    def loader(*args, **kwargs):
        collecting.set()
        assert release.wait(8), "test did not release its source stub"
        return FactSnapshot(NOW, NOW)

    notifier = Mock()

    def send(_):
        sent.set()
        return {"status": "sent", "reason": "ack"}

    notifier.send_reminder.side_effect = send
    trigger, execution = make_runtime(
        profile, fact_loader=loader, notifier_factory=lambda _: notifier
    )
    trigger.store.create(
        "alice",
        {
            "label": "慢源",
            "condition": {"field": "quotes.601088.last", "op": "lt", "value": 50},
            "actions": [{"kind": "reminder", "payload": {"chat_id": "chat-a"}}],
        },
        "slow",
    )
    execution.start()
    try:
        trigger.start()
        trigger._timer()  # Synthetic business clock is ahead of OS timer time.
        assert collecting.wait(3)
        dated(trigger)
        trigger.kick()
        trigger._timer()
        assert sent.wait(3), "clock/reminder was blocked by market facts"
        assert not release.is_set()
    finally:
        release.set()
        execution.stop(drain=True)


def test_slow_history_collection_does_not_block_fresh_quote_action(
    profile, monkeypatch
):
    from src.triggers import snapshots

    monkeypatch.setattr(snapshots, "load_report_snapshot", lambda cfg: {})
    collecting, release, sent = threading.Event(), threading.Event(), threading.Event()

    def loader(dependencies, config, **kwargs):
        if "stocks_data.601088.ma60" in dependencies:
            collecting.set()
            assert release.wait(8)
            return FactSnapshot(NOW, NOW)
        return FactSnapshot(
            NOW, NOW, facts={"quotes.601088.last": FactValue(49, "available", NOW, NOW)}
        )

    notifier = Mock()

    def send(_):
        sent.set()
        return {"status": "sent", "reason": "ack"}

    notifier.send_reminder.side_effect = send
    trigger, execution = make_runtime(
        profile, fact_loader=loader, notifier_factory=lambda _: notifier
    )
    for key, field in (
        ("slow", "stocks_data.601088.ma60"),
        ("fast", "quotes.601088.last"),
    ):
        trigger.store.create(
            "alice",
            {
                "label": key,
                "condition": {"field": field, "op": "lt", "value": 50},
                "actions": [{"kind": "reminder", "payload": {"chat_id": "chat-a"}}],
            },
            key,
        )
    execution.start()
    try:
        trigger.start()
        trigger._timer()
        assert collecting.wait(3)
        assert sent.wait(3), "latest-price trigger waited for slow historical source"
        assert not release.is_set()
    finally:
        release.set()
        execution.stop(drain=True)
