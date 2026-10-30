"""SYNTHETIC fixtures: real assistant/tool/store/runtime wiring.

OCR responses and quote responses are fixtures; this is NOT real-image accuracy
or live Feishu delivery acceptance.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from zoneinfo import ZoneInfo

from src.interactive.assistant.personal_tools import (
    PersonalTools,
    message_datetime,
    reminder_due,
    screenshot_time,
)
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.feishu_handler import handle_feishu_message
from src.personal_monitor import MonitorEngine, MonitorStore

TZ = ZoneInfo("Asia/Shanghai")


def model_call(name, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": name + "-1",
                "type": "function",
                "function": {
                    "name": "personal_monitor_" + name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


@pytest.fixture
def service(tmp_path):
    client = Mock()

    def quoted_fixture(code, as_of):
        from src.personal_monitor_runtime import exchange_state

        moment = datetime.fromisoformat(as_of)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TZ)
        market = exchange_state("XSHG", moment)
        return {
            "price": 40,
            "currency": "CNY",
            "provider_time": (
                moment if market["market_open"] else market["session_close"]
            ).isoformat(),
            "source": "controlled_quote_fixture",
            "market_timezone": "Asia/Shanghai",
            "market_closed": not market["market_open"],
            "latest_trading_date": market["latest_completed_session"],
        }

    assistant = FeishuAssistant(
        {"interactive": {"feishu": {"assistant": {"enabled": True}}}},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        client=client,
        configuration=Mock(),
        research=Mock(),
        start_workers=False,
        image_loader=lambda message, key: {"image": b"controlled-transport-fixture"},
        personal_quote_loader=quoted_fixture,
    )
    yield assistant
    assistant.stop()


def test_image_event_reaches_assistant_then_atomic_personal_snapshot(service):
    now = datetime.now(TZ).isoformat()
    service.client.read_position_images.return_value = {
        "account_hint": "isolated",
        "as_of": now,
        "positions": [
            {
                "code": "601088",
                "quantity": 1000,
                "price": 40,
                "market_value": 40000,
                "currency": "CNY",
            }
        ],
    }

    def complete(messages, tools, **kwargs):
        results = [
            json.loads(item["content"]) for item in messages if item["role"] == "tool"
        ]
        evidence = next(item for item in results if item.get("untrusted_image_facts"))
        if not any(item.get("operation") == "import_image" for item in results):
            return model_call(
                "import_image",
                {
                    "account": "isolated",
                    "batch_id": evidence["batch_id"],
                },
            )
        return {"role": "assistant", "content": "已更新截图中明确的持仓。"}

    service.client.complete.side_effect = complete
    service.personal_store.ensure_account("owner", "isolated")
    app = Mock(enabled=True, assistant_enabled=True)
    app.gate.is_allowed.return_value = True
    app.access.accept.return_value = "accepted"
    app.get_assistant.return_value = service
    body = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "message_id": "image-message-1",
                "chat_id": "chat",
                "chat_type": "p2p",
                "message_type": "image",
                "create_time": str(int(datetime.now(TZ).timestamp() * 1000)),
                "content": json.dumps({"image_key": "img_fixture"}),
            },
        },
    }
    assert handle_feishu_message(app, body) == "accepted"
    queued = service.execution_runtime.store.list_runs(job_id="conversation.turn")
    assert len(queued) == 1
    service.execution_runtime.start()
    assert service.execution_runtime.wait(queued[0]["id"], 15)["status"] == "succeeded"
    state = service.personal_store.status("owner", "isolated")
    assert state["accounts"][0]["positions"][0]["quantity"] == 1000
    assert state["outbox"] == []
    assert state["reminders"] == []
    assert "实际操作回执" in service.send_message.call_args.args[1]
    assert not service.submit("chat", "owner", "", "image-message-1")


def test_image_import_can_retry_after_account_creation_and_refresh_status(service):
    service.client.read_position_images.return_value = {
        "account_hint": "new-account",
        "as_of": datetime.now(TZ).isoformat(),
        "positions": [
            {
                "code": "601088",
                "quantity": 1000,
                "price": 40,
                "market_value": 40000,
                "currency": "CNY",
            }
        ],
    }
    sequence = iter(
        ["status", "import_image", "account", "import_image", "status", "done"]
    )

    def complete(messages, tools, **kwargs):
        action = next(sequence)
        if action == "done":
            return {"role": "assistant", "content": "已完成。"}
        args = {"account": "new-account"}
        if action == "import_image":
            evidence = next(
                json.loads(m["content"])
                for m in messages
                if m["role"] == "tool" and "untrusted_image_facts" in m["content"]
            )
            args["batch_id"] = evidence["batch_id"]
        return model_call(action, args)

    service.client.complete.side_effect = complete
    service.handle(
        "chat",
        "owner",
        "新建账户new-account，导入这张持仓截图",
        "retry-image",
        attachments=[{"message_id": "msg", "image_key": "img"}],
    )
    account = service.personal_store.status("owner")["accounts"][0]
    assert account["positions"][0]["quantity"] == 1000
    assert service.client.complete.call_count == 6
    assert "其余操作尚未完成" not in service.send_message.call_args.args[1]


def test_slow_ocr_cannot_overwrite_manual_update(service):
    now = datetime.now(TZ)
    service.personal_store.ensure_account("owner", "isolated")

    def recognize(images, **kwargs):
        service.personal_store.import_snapshot(
            "owner",
            "isolated",
            [{"instrument": "601088", "quantity": 700}],
            now.isoformat(),
            expected_revision=0,
            source="manual",
        )
        return {
            "as_of": now.isoformat(),
            "positions": [
                {
                    "code": "601088",
                    "quantity": 1000,
                    "price": 40,
                    "market_value": 40000,
                    "currency": "CNY",
                }
            ],
        }

    service.client.read_position_images.side_effect = recognize
    batch = service.personal.prepare_images(
        "owner", [{"message_id": "msg", "image_key": "img"}], "", {}
    )
    with pytest.raises(ValueError):
        service.personal.invoke(
            "owner",
            "chat",
            "import_image",
            {
                "account": "isolated",
                "batch_id": batch["batch_id"],
            },
            {"image_batch": batch, "id": "turn"},
        )
    position = service.personal_store.status("owner")["accounts"][0]["positions"][0]
    assert position["quantity"] == 700


def test_bad_image_row_preserves_old_position_and_needs_correction(service):
    now = datetime.now(TZ).isoformat()
    service.personal_store.ensure_account("owner", "isolated")
    service.personal_store.import_snapshot(
        "owner",
        "isolated",
        [{"instrument": "601088", "quantity": 300}],
        now,
        expected_revision=0,
        source="manual",
    )
    service.client.read_position_images.return_value = {
        "as_of": now,
        "positions": [
            {
                "code": "601088",
                "quantity": 1000,
                "price": 40,
                "market_value": 4000,
                "currency": "CNY",
            }
        ],
    }
    batch = service.personal.prepare_images(
        "owner", [{"message_id": "msg", "image_key": "img"}], "", {}
    )
    result = service.personal.invoke(
        "owner",
        "chat",
        "import_image",
        {
            "account": "isolated",
            "batch_id": batch["batch_id"],
        },
        {"image_batch": batch, "id": "turn"},
    )
    assert result["result"]["image_rejected"]
    assert (
        service.personal_store.status("owner")["accounts"][0]["positions"][0][
            "quantity"
        ]
        == 300
    )


def test_text_reminder_model_tool_receipt_and_restart_delivery(service):
    service.client.complete.side_effect = [
        model_call("create_reminder", {"after_days": 28, "text": "决定是否滚存"}),
        {"role": "assistant", "content": "已创建提醒，将于1900-01-01提醒。"},
    ]
    base = datetime(2026, 12, 20, 14, 12, tzinfo=TZ)
    service.handle(
        "chat",
        "owner",
        "28天后提醒我决定是否滚存",
        "text-1",
        message_time={"create_time": str(int(base.timestamp() * 1000))},
    )
    saved = service.personal_store.status("owner")["reminders"][0]
    assert datetime.fromisoformat(saved["due_at"]).astimezone(TZ) == base + timedelta(
        days=28
    )
    assert saved["route"] == "chat"
    assert "实际操作回执" in service.send_message.call_args.args[1]
    assert "1900-01-01" not in service.send_message.call_args.args[1]
    reopened = MonitorEngine(MonitorStore(service.personal_store.path))
    reopened.enqueue_due(base + timedelta(days=29))
    assert len(service.personal_store.status("owner")["outbox"]) == 1
    reopened.enqueue_due(base + timedelta(days=30))
    assert len(service.personal_store.status("owner")["outbox"]) == 1


def test_same_create_call_repeated_in_turn_is_idempotent(service):
    context = {"id": "turn", "message_id": "msg", "effective_time": datetime.now(TZ)}
    args = {"after_days": 28, "text": "到期决定是否滚存"}
    first = service.personal.invoke("owner", "chat", "create_reminder", args, context)
    second = service.personal.invoke("owner", "chat", "create_reminder", args, context)
    assert first["result"]["reminder_id"] == second["result"]["reminder_id"]
    assert len(service.personal_store.status("owner")["reminders"]) == 1


def test_changed_model_generated_id_cannot_duplicate_same_message_action(service):
    context = {"id": "turn", "message_id": "msg", "effective_time": datetime.now(TZ)}
    args = {"after_days": 28, "text": "到期决定是否滚存"}
    first = service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        dict(args, reminder_id="model-choice-1"),
        context,
    )
    second = service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        dict(args, reminder_id="model-choice-2"),
        context,
    )
    assert first["result"]["reminder_id"] == second["result"]["reminder_id"]


def test_transport_receipt_expiry_does_not_reenable_same_platform_message(service):
    assert service.submit("chat", "owner", "28天后提醒我", "permanent-message")
    with service.store.connection() as db:
        db.execute("DELETE FROM receipts")
    assert not service.submit("chat", "owner", "28天后提醒我", "permanent-message")
    assert service.execution_runtime.store.count_pending("conversation.turn") == 1


def test_missing_image_time_can_be_supplied_after_restart_without_reupload(service):
    service.personal_store.ensure_account("owner", "isolated")
    service.client.read_position_images.return_value = {
        "account_hint": "isolated",
        "positions": [
            {
                "code": "601088",
                "quantity": 1000,
                "price": 40,
                "market_value": 40000,
                "currency": "CNY",
            }
        ],
    }
    batch = service.personal.prepare_images(
        "owner",
        [{"message_id": "original-image", "image_key": "img"}],
        "",
        {},
        chat="chat",
    )
    reopened = PersonalTools(
        MonitorStore(service.personal_store.path),
        quote_loader=service.personal.quote_loader,
    )
    context = {"id": "follow-up", "effective_time": datetime.now(TZ)}
    evidence = reopened.invoke("owner", "chat", "image_evidence", {}, context)
    assert evidence["batch_id"] == batch["batch_id"]
    assert reopened.invoke("other", "chat", "image_evidence", {}, {}) == {
        "status": "no_images"
    }
    assert reopened.invoke("owner", "other-chat", "image_evidence", {}, {}) == {
        "status": "no_images"
    }
    result = reopened.invoke(
        "owner",
        "chat",
        "import_image",
        {
            "batch_id": batch["batch_id"],
            "account": "isolated",
            "use_message_time": True,
        },
        context,
    )
    assert result["result"]["applied"] == ["601088"]
    assert reopened.image_batch("owner", "chat") is None


def test_visible_image_account_conflict_does_not_update_other_account(service):
    service.personal_store.ensure_account("owner", "other-account")
    service.client.read_position_images.return_value = {
        "account_hint": "broker-A",
        "as_of": datetime.now(TZ).isoformat(),
        "positions": [
            {
                "code": "601088",
                "quantity": 1000,
                "price": 40,
                "market_value": 40000,
                "currency": "CNY",
            }
        ],
    }
    batch = service.personal.prepare_images(
        "owner", [{"message_id": "msg", "image_key": "img"}], "", {}, chat="chat"
    )
    with pytest.raises(ValueError, match="账户"):
        service.personal.invoke(
            "owner",
            "chat",
            "import_image",
            {
                "batch_id": batch["batch_id"],
                "account": "other-account",
            },
            {"image_batch": batch, "id": "turn"},
        )
    assert service.personal_store.status("owner")["accounts"][0]["positions"] == []


def test_manual_correction_does_not_rewrite_other_screenshot_rows(service):
    store = service.personal_store
    store.ensure_account("owner", "isolated")
    now = datetime.now(TZ).isoformat()
    store.import_snapshot(
        "owner",
        "isolated",
        [
            {
                "instrument": "601088",
                "quantity": 1000,
                "quote_evidence": {"source": "preserve-me"},
            },
            {"instrument": "01088", "quantity": 400},
        ],
        now,
        expected_revision=0,
    )
    result = service.personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "isolated",
            "as_of": now,
            "expected_revision": 1,
            "rows": [
                {"instrument": "01088", "quantity": 500},
                {"instrument": "601088", "quantity": 1000},
            ],
        },
        {"id": "correction", "original_text": "只更正01088为500股，其他行保留"},
    )
    assert result["result"]["applied"] == ["01088"]
    positions = {
        row["instrument"]: row
        for row in store.status("owner")["accounts"][0]["positions"]
    }
    assert positions["601088"]["source"] == "screenshot"
    assert positions["601088"]["quote_evidence"]["source"] == "preserve-me"


def test_uncertain_ocr_cannot_be_written_through_manual_tool(service):
    store = service.personal_store
    store.ensure_account("owner", "isolated")
    with pytest.raises(ValueError, match="import_image"):
        service.personal.invoke(
            "owner",
            "chat",
            "import_manual",
            {
                "account": "isolated",
                "as_of": datetime.now(TZ).isoformat(),
                "expected_revision": 0,
                "rows": [{"instrument": "601088", "quantity": 1000}],
            },
            {"id": "bad-model", "original_text": "导入这张截图", "attachments": [{}]},
        )
    assert store.status("owner")["accounts"][0]["positions"] == []


def test_manual_date_timezone_and_future_watermark_guard(service):
    store = service.personal_store
    store.ensure_account("owner", "isolated")
    args = {
        "account": "isolated",
        "as_of": "2020-01-01",
        "expected_revision": 0,
        "rows": [{"instrument": "601088", "quantity": 1000}],
    }
    service.personal.invoke(
        "owner",
        "chat",
        "import_manual",
        args,
        {"id": "date", "original_text": "记录601088在2020-01-01持有1000股"},
    )
    assert (
        store.status("owner")["accounts"][0]["positions"][0]["as_of"]
        == "2019-12-31T16:00:00+00:00"
    )
    with pytest.raises(ValueError, match="未来"):
        service.personal.invoke(
            "owner",
            "chat",
            "import_manual",
            dict(args, as_of="2099-01-01", expected_revision=1),
            {"id": "future"},
        )
    assert store.status("owner")["accounts"][0]["revision"] == 1


@pytest.mark.parametrize(
    "outcome,reminder_state,outbox_state",
    [
        ("sent", "completed", None),
        ("failed", "queued", "failed"),
        ("unknown", "unknown", "unknown"),
    ],
)
def test_past_due_reply_delivery_is_part_of_durable_reminder_state(
    service,
    outcome,
    reminder_state,
    outbox_state,
):
    service._personal_send = Mock(
        return_value={"outcome": outcome, "detail": "controlled result"}
    )
    service.client.complete.side_effect = [
        model_call(
            "create_reminder",
            {"due_at": "2020-01-01T09:00:00+08:00", "text": "复核到期"},
        ),
        {"role": "assistant", "content": "已到期，请复核。"},
    ]
    service.handle("chat", "owner", "这个日期已经过去，现在提醒我一次", "past-due-msg")
    state = service.personal_store.status("owner")
    assert state["reminders"][0]["state"] == reminder_state
    assert service._personal_send.call_count == 1
    if outbox_state:
        assert len(state["outbox"]) == 1
        assert state["outbox"][0]["state"] == outbox_state
        assert "已到期提醒" in state["outbox"][0]["text"]
    else:
        assert len(state["outbox"]) == 1
        assert state["outbox"][0]["state"] == "sent"


def test_unknown_past_due_reply_requires_explicit_resend(service):
    context = {"id": "turn", "effective_time": datetime.now(TZ)}
    service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        {"due_at": "2020-01-01T09:00:00+08:00", "text": "复核到期"},
        context,
    )
    service.personal.record_immediate_delivery(context, "unknown", "timeout")
    engine = MonitorEngine(service.personal_store)
    assert engine.claim_delivery("worker") is None
    entry = service.personal_store.status("owner")["outbox"][0]
    receipt = service.personal.invoke(
        "owner", "chat", "resend", {"outbox_id": entry["id"]}, context
    )
    assert receipt["result"]["duplicate_possible"] is True
    assert engine.claim_delivery("worker") is not None


def test_crash_before_immediate_reply_never_claims_delivery_completed(service):
    store = service.personal_store
    store.register_route("owner", "chat")
    # Simulate death after the durable mutation and before the assistant stages
    # or sends its normal reply. Reopening must retain uncertainty, not success.
    created = store.create_reminder(
        "owner",
        "crash",
        "2020-01-01T09:00:00+08:00",
        "到期核查",
        "chat",
        reply_delivery=True,
    )
    assert created["state"] == "receipt_pending"
    reopened = PersonalTools(MonitorStore(store.path))
    state = reopened.store.status("owner")
    assert state["reminders"][0]["state"] == "unknown"
    assert state["outbox"][0]["state"] == "unknown"
    assert MonitorEngine(reopened.store).claim_delivery("worker") is None


def test_committed_operation_gets_factual_receipt_even_if_model_fails_afterward(
    service,
):
    from src.interactive.assistant.client import AssistantAPIError

    service.client.complete.side_effect = [
        model_call("create_reminder", {"after_days": 28, "text": "到期决定"}),
        AssistantAPIError("controlled provider outage"),
    ]
    service.handle("chat", "owner", "28天后提醒我到期决定", "model-outage")
    state = service.personal_store.status("owner")
    assert len(state["reminders"]) == 1
    reply = service.send_message.call_args.args[1]
    assert "实际操作回执" in reply
    assert state["reminders"][0]["reminder_id"] in reply


def test_model_cannot_choose_unverified_destination_or_other_owner(service):
    context = {"id": "turn", "effective_time": datetime.now(TZ)}
    with pytest.raises(ValueError, match="目标会话"):
        service.personal.invoke(
            "owner",
            "chat",
            "create_reminder",
            {
                "after_days": 1,
                "text": "提醒",
                "route": "other-private-chat",
            },
            context,
        )
    service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        {
            "after_days": 1,
            "text": "提醒",
            "reminder_id": "mine",
        },
        context,
    )
    assert (
        service.personal.invoke("other", "other-chat", "status", {}, {})["reminders"]
        == []
    )
    with pytest.raises((ValueError, KeyError)):
        service.personal.invoke(
            "other",
            "other-chat",
            "cancel_reminder",
            {"reminder_id": "mine", "expected_revision": 0},
            context,
        )


@pytest.mark.parametrize(
    "base,days,expected",
    [
        ("2026-12-20T14:00:00+08:00", 28, "2027-01-17T14:00:00+08:00"),
        ("2028-02-01T14:00:00+08:00", 28, "2028-02-29T14:00:00+08:00"),
    ],
)
def test_calendar_days(base, days, expected):
    assert reminder_due({"after_days": days}, datetime.fromisoformat(base)) == expected


def test_date_only_default_and_override_and_server_timezone():
    base = datetime.fromisoformat("2026-10-21T00:00:00+00:00")
    assert (
        reminder_due({"date_only": "2026-11-18"}, base) == "2026-11-18T09:50:00+08:00"
    )
    assert (
        reminder_due({"date_only": "2026-11-18", "default_time": "14:30"}, base)
        == "2026-11-18T14:30:00+08:00"
    )
    assert message_datetime({"create_time": "1792540800000"}).tzinfo == TZ
    assert screenshot_time("2026-10-21T14:30:00") == "2026-10-21T14:30:00+08:00"
    assert screenshot_time("2026-10-21") == "2026-10-21"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"after_days": 1, "due_at": "2027-01-01T00:00:00+08:00"},
        {"after_days": float("nan")},
        {"after_days": True},
        {"after_days": -1},
    ],
)
def test_ambiguous_or_invalid_due_is_not_silently_interpreted(arguments):
    with pytest.raises(ValueError):
        reminder_due(arguments, datetime.now(TZ))


@pytest.mark.parametrize("operation", ["cancel_reminder", "edit_reminder"])
def test_same_turn_mutation_suppresses_old_immediate_notice(service, operation):
    context = {"id": "mutation-turn", "effective_time": datetime.now(TZ), "answers": []}
    service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        {
            "reminder_id": "immediate-old",
            "due_at": "2020-01-01T09:00:00+08:00",
            "text": "旧提醒",
        },
        context,
    )
    args = {"reminder_id": "immediate-old", "expected_revision": 1}
    if operation == "edit_reminder":
        args["after_days"] = 2
    receipt = service.personal.invoke("owner", "chat", operation, args, context)
    assert not receipt["result"].get("possibly_sent")
    assert service.personal.immediate_results(context) == []
    rendered = service.personal.render_receipts(context)
    assert "本次回复即为到期提醒" not in rendered
    service._audit = Mock()
    service._turn_context.current = context
    assert service._reply("chat", rendered)
    assert "已到期提醒：旧提醒" not in service.send_message.call_args.args[1]
    state = service.personal_store.status("owner")
    assert all(row["state"] == "cancelled" for row in state["outbox"])


def test_past_due_other_route_only_queues_actual_destination(service):
    store = service.personal_store
    store.register_route("owner", "other")
    context = {"id": "other-route", "effective_time": datetime.now(TZ), "answers": []}
    service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        {
            "reminder_id": "routed",
            "due_at": "2020-01-01T09:00:00+08:00",
            "text": "目标提醒",
            "route": "other",
        },
        context,
    )
    assert service.personal.immediate_results(context) == []
    service._audit = Mock()
    service._turn_context.current = context
    service._reply("chat", service.personal.render_receipts(context))
    assert "本次回复即为到期提醒" not in service.send_message.call_args.args[1]
    assert "已到期提醒：" not in service.send_message.call_args.args[1]
    engine = MonitorEngine(store)
    entry = engine.claim_delivery("test-worker")
    assert entry["route"] == "other"
    assert engine.mark_submitted(entry["id"], entry["token"])
    assert engine.finish_delivery(entry["id"], entry["token"], "sent")
    state = store.status("owner")
    assert state["reminders"][0]["state"] == "completed"
    assert len(state["outbox"]) == 1
    assert engine.claim_delivery("test-worker") is None


@pytest.mark.parametrize("operation", ["cancel_reminder", "edit_reminder"])
def test_long_reply_keeps_immediate_cancelable_until_its_message(service, operation):
    context = {"id": "long-reply", "effective_time": datetime.now(TZ), "answers": []}
    service.personal.invoke(
        "owner",
        "chat",
        "create_reminder",
        {
            "reminder_id": "later-payload",
            "due_at": "2020-01-01T09:00:00+08:00",
            "text": "必须过滤的旧提醒",
        },
        context,
    )
    service._personal_send = Mock(return_value={"outcome": "sent"})
    service._audit = Mock()
    service._turn_context.current = context
    delivered = []

    def send(chat, payload):
        delivered.append(payload)
        if len(delivered) == 1:
            args = {"reminder_id": "later-payload", "expected_revision": 1}
            if operation == "edit_reminder":
                args["after_days"] = 3
            result = service.personal.invoke("owner", "chat", operation, args, context)
            assert not result["result"]["possibly_sent"]
        return True, "controlled"

    service.send_message.side_effect = send
    assert service._reply("chat", "管理回执" * 1500)
    assert not service._personal_send.called
    assert all("必须过滤的旧提醒" not in payload for payload in delivered)
    assert service.personal_store.status("owner")["outbox"][0]["state"] == "cancelled"


def test_image_account_declaration_survives_next_message_and_restart(service):
    store = service.personal_store
    service.client.read_position_images.return_value = {
        "account_hint": "broker-display",
        "as_of": datetime.now(TZ).isoformat(),
        "positions": [],
    }
    batch = service.personal.prepare_images(
        "owner", [{"message_id": "m", "image_key": "i"}], "", {}, chat="chat"
    )
    service.personal.invoke(
        "owner",
        "chat",
        "account",
        {"account": "named"},
        {"id": "declaration", "original_text": "账户名是named", "image_batch": batch},
    )
    reopened = PersonalTools(MonitorStore(store.path))
    evidence = reopened.invoke("owner", "chat", "image_evidence", {}, {"id": "next"})
    assert evidence["confirmed_account"] == "named"
    result = reopened.invoke(
        "owner",
        "chat",
        "import_image",
        {"batch_id": batch["batch_id"], "account": "named"},
        {"id": "next", "original_text": "这是当前持仓"},
    )
    assert result["status"] == "needs_information"
    assert reopened._image_account("owner", "other-chat", batch["batch_id"]) is None


def test_unknown_image_id_reports_available_real_id_instead_of_no_images(service):
    service.client.read_position_images.return_value = {
        "account_hint": "display",
        "as_of": None,
        "positions": [],
    }
    batch = service.personal.prepare_images(
        "owner", [{"message_id": "m", "image_key": "i"}], "", {}, chat="chat"
    )
    with pytest.raises(ValueError, match=batch["batch_id"]):
        service.personal.invoke(
            "owner", "chat", "image_evidence", {"batch_id": "invented"}, {"id": "next"}
        )
    assert service.personal.invoke(
        "owner",
        "different",
        "image_evidence",
        {"batch_id": "invented"},
        {"id": "foreign"},
    ) == {"status": "no_images"}


def test_duplicate_image_preserves_evidence_without_second_vision_request(service):
    service.client.read_position_images.return_value = {
        "account_hint": "display",
        "as_of": None,
        "positions": [],
    }
    first = service.personal.prepare_images(
        "owner", [{"message_id": "m", "image_key": "i"}], "", {}, chat="chat"
    )
    second = service.personal.prepare_images(
        "owner", [{"message_id": "retry", "image_key": "same"}], "", {}, chat="chat"
    )
    assert first == second
    service.client.read_position_images.assert_called_once()


def _complementary_a_documents(hint="人民币账户 A 股"):
    return (
        {
            "account_hint": hint,
            "positions": [
                {
                    "code": "601088",
                    "name": "中国神华",
                    "quantity": 200,
                    "quantity_unit": "股",
                    "market_value": 8000,
                    "currency": "CNY",
                    "uncertain_fields": ["price"],
                    "ambiguities": ["price: 未显示"],
                }
            ],
        },
        {
            "account_hint": hint,
            "positions": [
                {
                    "code": None,
                    "name": "中国神华",
                    "quantity": None,
                    "price": 40,
                    "price_decimals": 2,
                    "market_value": 8000,
                    "currency": "CNY",
                    "uncertain_fields": ["code", "quantity"],
                    "ambiguities": ["code: 未显示", "quantity: 未显示"],
                }
            ],
        },
    )


def _prepare_complement(service, document, key, when, text=""):
    service.personal.image_loader = lambda message, image: {"image": image.encode()}
    service.client.read_position_images.return_value = document
    return service.personal.prepare_images(
        "owner",
        [{"message_id": key, "image_key": key}],
        text,
        {"create_time": str(int(when.timestamp() * 1000))},
        chat="chat",
    )


def test_account_unknown_current_image_cannot_borrow_known_other_account_quantity(
    service,
):
    store = service.personal_store
    store.ensure_account("owner", "broker-A")
    store.ensure_account("owner", "broker-B")
    now = datetime.now(TZ)
    store.import_snapshot(
        "owner",
        "broker-A",
        [{"instrument": "601088", "quantity": 300}],
        (now - timedelta(minutes=5)).isoformat(),
        0,
        "manual",
    )
    donor, current = _complementary_a_documents()
    previous = _prepare_complement(
        service, donor, "other-account-quantity", now - timedelta(minutes=2), "broker-B"
    )
    service.personal._bind_image_account("owner", "chat", previous, "broker-B")
    batch = _prepare_complement(service, current, "account-unknown-price", now)
    assert batch["document"]["positions"][0]["quantity"] is None
    assert batch["document"]["composition"]["matched_rows"] == []
    result = service.personal.invoke(
        "owner",
        "chat",
        "import_image",
        {
            "account": "broker-A",
            "batch_id": batch["batch_id"],
            "use_message_time": True,
        },
        {
            "id": "declare-A",
            "original_text": "这张是broker-A当前持仓",
            "effective_time": now,
        },
    )
    assert result["result"]["applied"] == []
    assert (
        store.status("owner", "broker-A")["accounts"][0]["positions"][0]["quantity"]
        == 300
    )


def test_donor_account_bound_after_composition_is_checked_again_at_commit(service):
    store = service.personal_store
    store.ensure_account("owner", "broker-A")
    store.ensure_account("owner", "broker-B")
    now = datetime.now(TZ)
    donor, current = _complementary_a_documents()
    previous = _prepare_complement(
        service, donor, "initially-unbound-quantity", now - timedelta(minutes=2)
    )
    batch = _prepare_complement(service, current, "initially-unbound-price", now)
    assert batch["document"]["positions"][0]["quantity"] == 200
    assert previous["batch_id"] in batch["document"]["composition"]["source_batch_ids"]
    service.personal._bind_image_account("owner", "chat", previous, "broker-B")
    with pytest.raises(ValueError, match="互补截图属于其他账户"):
        service.personal.invoke(
            "owner",
            "chat",
            "import_image",
            {
                "account": "broker-A",
                "batch_id": batch["batch_id"],
                "use_message_time": True,
            },
            {
                "id": "declare-A",
                "original_text": "这张是broker-A当前持仓",
                "effective_time": now,
            },
        )
    assert store.status("owner", "broker-A")["accounts"][0]["positions"] == []


def test_old_quantity_source_is_filtered_after_newer_manual_holding_update(service):
    store = service.personal_store
    store.ensure_account("owner", "broker-A")
    now = datetime.now(TZ)
    donor, current = _complementary_a_documents()
    previous = _prepare_complement(
        service, donor, "before-manual-quantity", now - timedelta(minutes=2), "broker-A"
    )
    store.import_snapshot(
        "owner",
        "broker-A",
        [{"instrument": "601088", "quantity": 300}],
        (now - timedelta(minutes=1)).isoformat(),
        0,
        "manual",
    )
    batch = _prepare_complement(service, current, "after-manual-price", now, "broker-A")
    assert previous["revisions"]["broker-A"] == 0
    assert batch["revisions"]["broker-A"] == 1
    assert batch["document"]["positions"][0]["quantity"] is None
    assert batch["document"]["composition"]["matched_rows"] == []
    result = service.personal.invoke(
        "owner",
        "chat",
        "import_image",
        {
            "account": "broker-A",
            "batch_id": batch["batch_id"],
            "use_message_time": True,
        },
        {
            "id": "later-current",
            "original_text": "这是broker-A当前价格图",
            "effective_time": now,
        },
    )
    assert result["result"]["applied"] == []
    assert (
        store.status("owner", "broker-A")["accounts"][0]["positions"][0]["quantity"]
        == 300
    )


def test_legacy_composed_batch_cannot_promote_source_revision_with_new_frame(service):
    from src.interactive.assistant.image_composition import compose_position_document

    store = service.personal_store
    store.ensure_account("owner", "broker-A")
    now = datetime.now(TZ)
    donor, current = _complementary_a_documents()
    previous = _prepare_complement(
        service, donor, "legacy-old-quantity", now - timedelta(minutes=2), "broker-A"
    )
    store.import_snapshot(
        "owner",
        "broker-A",
        [{"instrument": "601088", "quantity": 300}],
        (now - timedelta(minutes=1)).isoformat(),
        0,
        "manual",
    )
    batch = _prepare_complement(service, current, "legacy-new-price", now, "broker-A")
    # Simulate an already persisted document from the implementation before
    # source-version filtering. Its own CAS is current; its borrowed field isn't.
    batch["document"] = compose_position_document(
        batch["document"],
        batch["batch_id"],
        [previous],
        current_image_hashes=batch["source_image_hashes"],
    )
    assert batch["document"]["positions"][0]["quantity"] == 200
    with pytest.raises(ValueError, match="互补截图早于持仓修改"):
        service.personal.invoke(
            "owner",
            "chat",
            "import_image",
            {
                "account": "broker-A",
                "batch_id": batch["batch_id"],
                "use_message_time": True,
            },
            {
                "id": "legacy",
                "original_text": "这是broker-A当前持仓",
                "effective_time": now,
                "image_batch": batch,
            },
        )
    assert (
        store.status("owner", "broker-A")["accounts"][0]["positions"][0]["quantity"]
        == 300
    )


def test_same_turn_account_binding_cache_includes_selected_image_batch(service):
    now = datetime.now(TZ)
    first = _prepare_complement(
        service,
        {"account_hint": "display", "positions": []},
        "bind-first",
        now - timedelta(seconds=1),
    )
    second = _prepare_complement(
        service, {"account_hint": "display", "positions": []}, "bind-second", now
    )
    context = {
        "id": "bind-two",
        "original_text": "这两张账户名都是合成AH",
        "image_batch": first,
    }
    service.personal.invoke("owner", "chat", "account", {"account": "合成AH"}, context)
    context["image_batch"] = second
    service.personal.invoke("owner", "chat", "account", {"account": "合成AH"}, context)
    assert (
        service.personal._image_account("owner", "chat", first["batch_id"]) == "合成AH"
    )
    assert (
        service.personal._image_account("owner", "chat", second["batch_id"]) == "合成AH"
    )
    assert len(service.personal_store.status("owner")["accounts"]) == 1


@pytest.mark.parametrize("external_manual_update", [False, True])
def test_user_unit_confirmation_retries_own_bad_row_revision_but_not_external_change(
    service, external_manual_update
):
    store = service.personal_store
    store.ensure_account("owner", "broker-A")
    now = datetime.now(TZ)
    document = {
        "account_hint": "broker-A",
        "as_of": now.isoformat(),
        "positions": [
            {
                "code": "601088",
                "name": "中国神华",
                "asset_type": "equity",
                "quantity": 200,
                "quantity_unit": None,
                "price": 40,
                "price_decimals": 2,
                "market_value": 8000,
                "currency": "CNY",
                "uncertain_fields": ["quantity_unit"],
                "ambiguities": ["quantity_unit: 图中未标明股/万股"],
            }
        ],
    }
    batch = _prepare_complement(service, document, "unit-missing", now, "broker-A")
    first = service.personal.invoke(
        "owner",
        "chat",
        "import_image",
        {"account": "broker-A", "batch_id": batch["batch_id"]},
        {"id": "missing-unit", "original_text": "这是broker-A当前持仓"},
    )
    assert first["result"]["applied"] == []
    assert first["result"]["revision"] == 1
    assert batch["revisions"]["broker-A"] == 0
    if external_manual_update:
        store.import_snapshot(
            "owner",
            "broker-A",
            [{"instrument": "601088", "quantity": 300}],
            now.isoformat(),
            1,
            "manual",
        )
    arguments = {
        "account": "broker-A",
        "batch_id": batch["batch_id"],
        "confirm_unscaled_quantity_units": True,
    }
    context = {
        "id": "unit-confirmation",
        "message_id": "platform-unit-confirmation",
        "original_text": "确认broker-A截图里的股票数量都是股，ETF和REIT都是份，没有万倍缩放",
    }
    if external_manual_update:
        with pytest.raises(ValueError, match="account_revision_changed"):
            service.personal.invoke("owner", "chat", "import_image", arguments, context)
        account = store.status("owner", "broker-A")["accounts"][0]
        assert account["positions"][0]["quantity"] == 300
        assert account["revision"] == 2
    else:
        confirmed = service.personal.invoke(
            "owner", "chat", "import_image", arguments, context
        )
        assert confirmed["result"]["applied"] == ["601088"]
        account = store.status("owner", "broker-A")["accounts"][0]
        position = account["positions"][0]
        assert position["quantity"] == 200
        assert position["quantity_unit"] == "股"
        assert position["market_value"] == 8000
        assert (
            position["quantity_unit_declaration"]["source"]
            == "authenticated_user_confirmation"
        )
        assert (
            position["quantity_unit_declaration"]["user_text"]
            == context["original_text"]
        )
        assert account["pending_issues"] == []
        # A model repeats this exact successful action in the same turn: no
        # second mutation, no extra revision and no duplicated user declaration.
        replay = service.personal.invoke(
            "owner", "chat", "import_image", arguments, context
        )
        assert replay == confirmed
        assert store.status("owner", "broker-A")["accounts"][0]["revision"] == 2


def test_partial_image_keeps_unresolved_facts_discoverable_after_restart(service):
    stamp = datetime.now(TZ).isoformat()
    good = [
        {
            "code": code,
            "quantity": 10,
            "price": 40,
            "market_value": 400,
            "currency": "CNY",
            "asset_type": "equity",
            "ambiguities": [],
        }
        for code in (
            "601088",
            "600036",
            "000958",
            "512810",
            "508091",
            "588510",
            "520550",
            "510500",
        )
    ]
    repo = [
        {
            "code": code,
            "name": "逆回购",
            "quantity": 10,
            "price": 100,
            "market_value": 1000,
            "currency": "CNY",
            "asset_type": "reverse_repo",
        }
        for code in ("888880", "131990")
    ]
    hk = [
        {
            "code": None,
            "name": name,
            "quantity": None,
            "price": 3,
            "market_value": 300,
            "currency": "HKD",
            "market_value_currency": "CNY",
        }
        for name in ("港股甲", "港股乙", "港股丙", "港股丁")
    ]
    service.client.read_position_images.return_value = {
        "account_hint": "broker",
        "as_of": stamp,
        "positions": good + repo + hk,
    }
    batch = service.personal.prepare_images(
        "owner", [{"message_id": "partial", "image_key": "i"}], "", {}, chat="chat"
    )
    service.personal.invoke(
        "owner",
        "chat",
        "account",
        {"account": "named"},
        {"id": "account", "original_text": "账户名named", "image_batch": batch},
    )
    args = {"account": "named", "batch_id": batch["batch_id"]}
    first = service.personal.invoke(
        "owner", "chat", "import_image", args, {"id": "first"}
    )
    assert len(first["result"]["applied"]) == 8
    reopened = PersonalTools(
        MonitorStore(service.personal_store.path),
        quote_loader=service.personal.quote_loader,
    )
    state = reopened.invoke("owner", "chat", "status", {}, {"id": "status"})
    assert len(state["pending_images"][0]["rows"]) == 6
    assert {row["name"] for row in state["pending_images"][0]["rows"]} == {
        "逆回购",
        "港股甲",
        "港股乙",
        "港股丙",
        "港股丁",
    }
    evidence = reopened.invoke(
        "owner", "chat", "image_evidence", {}, {"id": "evidence"}
    )
    assert evidence["batch_id"] == batch["batch_id"]
    repeat = reopened.invoke("owner", "chat", "import_image", args, {"id": "repeat"})
    assert repeat["result"]["duplicate"] is True
    assert repeat["result"]["revision"] == first["result"]["revision"]
    assert len(reopened.store.status("owner")["accounts"][0]["positions"]) == 8
    assert (
        reopened.invoke("owner", "other", "status", {}, {"id": "other"}).get(
            "pending_images"
        )
        is None
    )
