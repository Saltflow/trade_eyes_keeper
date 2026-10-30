"""Real SQLite/outbox boundaries for reply-carried reminders; no platform calls."""

from copy import deepcopy

import pytest

from src.interactive.assistant.immediate_reminders import ImmediateDelivery
from src.personal_monitor import MonitorEngine, MonitorStore

NOW = "2026-10-21T06:00:00+00:00"
PAST = "2026-10-20T06:00:00+00:00"
FUTURE = "2099-10-22T06:00:00+00:00"


@pytest.fixture
def delivery(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite")
    store.register_route("owner", "chat")
    store.register_route("owner", "other-chat")
    return ImmediateDelivery(store)


def create(delivery, name="r", route="chat"):
    return delivery.store.create_reminder(
        "owner", name, PAST, "复核到期", route, now=NOW, reply_delivery=True
    )


def states(delivery):
    status = delivery.store.status("owner")
    return status["reminders"], status["outbox"]


def test_stage_and_claim_are_idempotent_and_only_success_completes(delivery):
    item = create(delivery)
    first = delivery.stage(item)
    assert delivery.stage(item)["outbox_id"] == first["outbox_id"]
    assert states(delivery)[0][0]["state"] == "receipt_pending"
    assert states(delivery)[1][0]["state"] == "unknown"
    assert MonitorEngine(delivery.store).claim_delivery("ordinary-worker", NOW) is None
    claims = delivery.claim([item, item], "chat")
    assert len(claims) == 1
    assert claims[0]["outbox_id"] == first["outbox_id"]
    assert delivery.claim([item], "chat") == []
    assert states(delivery)[0][0]["state"] == "queued"
    assert states(delivery)[1][0]["state"] == "submitted"
    assert delivery.finish(claims, "sent", {"platform_message_id": "captured-only"})
    assert states(delivery)[0][0]["state"] == "completed"
    assert states(delivery)[1][0]["state"] == "sent"
    assert delivery.finish(claims, "sent") == []
    assert delivery.active([item]) == []
    assert len(states(delivery)[1]) == 1


@pytest.mark.parametrize("operation", ["cancel", "edit_future", "edit_past"])
def test_canceled_or_edited_staged_version_cannot_enter_reply(delivery, operation):
    old = create(delivery)
    delivery.stage(old)
    if operation == "cancel":
        delivery.store.cancel_reminder("owner", "r", 1)
        candidates = [old]
    else:
        revised = delivery.store.edit_reminder(
            "owner",
            "r",
            1,
            due_at=FUTURE if operation == "edit_future" else PAST,
            now=NOW,
            reply_delivery=True,
        )
        candidates = [old, revised]
        if operation == "edit_past":
            delivery.stage(revised)
    active = delivery.active(candidates)
    assert [item["revision"] for item in active] == (
        [2] if operation == "edit_past" else []
    )
    claims = delivery.claim(candidates, "chat")
    assert [entry["item"]["revision"] for entry in claims] == (
        [2] if operation == "edit_past" else []
    )
    assert all(entry["item"]["revision"] != 1 for entry in claims)


@pytest.mark.parametrize("already_staged", [False, True])
def test_other_route_is_deferred_once_and_not_counted_as_current_chat_delivery(
    delivery, already_staged
):
    item = create(delivery, route="other-chat")
    if already_staged:
        delivery.stage(item)
    assert delivery.claim([item], "chat") == []
    deferred = delivery.defer(item)
    assert deferred["state"] == "queued"
    assert delivery.defer(item) is None
    assert delivery.active([item]) == []
    reminders, outbox = states(delivery)
    assert reminders[0]["state"] == "queued"
    assert len(outbox) == 1
    assert outbox[0]["route"] == "other-chat"
    engine = MonitorEngine(delivery.store)
    entry = engine.claim_delivery("ordinary-worker", NOW)
    assert entry["route"] == "other-chat"
    assert engine.mark_submitted(entry["id"], entry["token"], now=NOW)
    engine.finish_delivery(entry["id"], entry["token"], "sent", now=NOW)
    assert states(delivery)[0][0]["state"] == "completed"
    assert engine.claim_delivery("ordinary-worker", NOW) is None


@pytest.mark.parametrize(
    "outcome,reminder_state,outbox_state",
    [
        ("sent", "completed", "sent"),
        ("failed", "queued", "failed"),
        ("unknown", "unknown", "unknown"),
    ],
)
def test_classified_reply_results_have_real_delivery_states(
    delivery, outcome, reminder_state, outbox_state
):
    item = create(delivery)
    claims = delivery.claim([item], "chat")
    assert delivery.finish(claims, outcome, {"detail": "local capture"})
    reminders, outbox = states(delivery)
    assert reminders[0]["state"] == reminder_state
    assert outbox[0]["state"] == outbox_state
    with delivery.store.connection() as db:
        receipt = db.execute(
            "SELECT outcome FROM personal_immediate_receipts"
        ).fetchone()
    assert receipt[0] == outcome
    engine = MonitorEngine(delivery.store)
    if outcome == "failed":
        retry = engine.claim_delivery("retry", outbox[0]["next_attempt_at"])
        assert retry["id"] == claims[0]["outbox_id"]
    else:
        assert engine.claim_delivery("retry", "2099-01-01T00:00:00+00:00") is None


@pytest.mark.parametrize("operation", ["cancel", "edit"])
def test_late_receipt_records_old_send_without_reviving_new_reminder(
    delivery, operation
):
    item = create(delivery)
    claims = delivery.claim([item], "chat")
    if operation == "cancel":
        delivery.store.cancel_reminder("owner", "r", 1)
        expected = "cancelled"
    else:
        delivery.store.edit_reminder("owner", "r", 1, due_at=FUTURE, now=NOW)
        expected = "pending"
    assert delivery.finish(
        claims, "sent", {"platform_message_id": "actual-old-receipt"}
    )
    reminders, outbox = states(delivery)
    assert reminders[0]["revision"] == 2
    assert reminders[0]["state"] == expected
    assert outbox[0]["state"] == "sent"
    assert "actual-old-receipt" in outbox[0]["metadata"]
    assert delivery.recover() == []
    assert states(delivery)[0][0]["state"] == expected


@pytest.mark.parametrize("boundary", ["mutation", "stage", "submit"])
def test_restart_between_mutation_and_confirmation_is_unknown_without_auto_send(
    delivery, boundary
):
    item = create(delivery)
    if boundary == "stage":
        delivery.stage(item)
    elif boundary == "submit":
        delivery.claim([item], "chat")
    reopened = ImmediateDelivery(MonitorStore(delivery.store.path))
    assert len(reopened.recover()) == 1
    reminders, outbox = states(reopened)
    assert reminders[0]["state"] == "unknown"
    assert outbox[0]["state"] == "unknown"
    assert reopened.active([item]) == []
    assert reopened.claim([item], "chat") == []
    assert MonitorEngine(reopened.store).claim_delivery("worker", NOW) is None
    assert reopened.recover() == []


@pytest.mark.parametrize("operation", ["cancel", "edit"])
def test_recovery_after_cancel_or_reschedule_does_not_revive_old_version(
    delivery, operation
):
    item = create(delivery)
    delivery.stage(item)
    if operation == "cancel":
        delivery.store.cancel_reminder("owner", "r", 1)
        expected = "cancelled"
    else:
        delivery.store.edit_reminder("owner", "r", 1, due_at=FUTURE, now=NOW)
        expected = "pending"
    ImmediateDelivery(delivery.store).recover()
    reminders, _outbox = states(delivery)
    assert reminders[0]["revision"] == 2
    assert reminders[0]["state"] == expected
    assert delivery.claim([item], "chat") == []


def test_candidate_facts_come_from_authenticated_store_and_matching_chat(delivery):
    item = create(delivery)
    impostor = dict(item, owner="another-owner")
    assert delivery.active([impostor]) == []
    assert delivery.claim([impostor], "chat") == []
    assert delivery.claim([item], "other-chat") == []
    modified = deepcopy(item)
    modified.update(text="fabricated", route="other-chat")
    claims = delivery.claim([modified], "chat")
    assert claims[0]["item"]["text"] == item["text"]
    assert claims[0]["item"]["route"] == "chat"
