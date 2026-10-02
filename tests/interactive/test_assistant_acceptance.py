"""Independent acceptance for cancellation and shutdown at dispatch boundaries."""

import json
import threading
from unittest.mock import Mock, patch

import pytest

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message


@pytest.fixture
def assistant(tmp_path):
    configuration = Mock()
    configuration.apply.return_value = {"status": "applied", "effect": "下次任务生效"}
    service = FeishuAssistant(
        {"interactive": {"feishu": {"assistant": {"enabled": True}}}},
        Mock(return_value=(True, "ok")),
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=configuration,
        research=Mock(),
        client=Mock(),
        store=ProposalStore(tmp_path / "assistant.sqlite3"),
        start_workers=False,
    )
    yield service
    service.stop()


def _pending_config(service):
    item = service.store.create(
        "config", "chat", "owner", {"diff": [], "effect": "下次任务生效"}
    )
    service.store.activate(item["id"])
    return service.store.get(item["id"], "chat", "owner")


def test_cancel_between_config_claim_and_dispatch_prevents_write(
    assistant, monkeypatch
):
    item = _pending_config(assistant)
    claim = assistant.store.claim

    def claim_then_cancel(*args):
        claimed = claim(*args)
        # submit cancellation is intentionally a concurrent fast path in production.
        assistant._control("chat", "owner", "取消", item["id"])
        return claimed

    monkeypatch.setattr(assistant.store, "claim", claim_then_cancel)
    assistant.handle("chat", "owner", "确认 " + item["id"])
    assistant.configuration.apply.assert_not_called()
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "cancelled"


def test_stop_between_config_claim_and_dispatch_prevents_write(assistant, monkeypatch):
    item = _pending_config(assistant)
    claim = assistant.store.claim

    def claim_then_stop(*args):
        claimed = claim(*args)
        assistant.stop()
        return claimed

    monkeypatch.setattr(assistant.store, "claim", claim_then_stop)
    assistant.handle("chat", "owner", "确认 " + item["id"])
    assistant.configuration.apply.assert_not_called()
    assert assistant.store.get(item["id"], "chat", "owner")["status"] in {
        "cancelled",
        "interrupted",
    }


def test_preview_delivery_failure_does_not_resurrect_cancelled_action(assistant):
    item = assistant.store.create(
        "config", "chat", "owner", {"diff": [], "effect": "下次任务生效"}
    )

    def cancelled_while_sending(_chat, text):
        if text.startswith("待确认提案"):
            assistant._control("chat", "owner", "取消", item["id"])
            return False, "network lost after cancellation arrived"
        return True, "ok"

    assistant.send_message.side_effect = cancelled_while_sending
    assert not assistant._preview(item)
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "cancelled"
    assistant.handle("chat", "owner", "获取 " + item["id"])
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "cancelled"


def test_stop_between_research_claim_and_queue_does_not_leave_queued_job(
    assistant, monkeypatch, tmp_path
):
    item = assistant.store.create(
        "research", "chat", "owner", {"job_dir": str(tmp_path)}
    )
    assistant.store.activate(item["id"])
    claim = assistant.store.claim

    def claim_then_stop(*args):
        claimed = claim(*args)
        assistant.stop()
        return claimed

    monkeypatch.setattr(assistant.store, "claim", claim_then_stop)
    assistant.handle("chat", "owner", "确认 " + item["id"])
    assert assistant._jobs.empty()
    assert assistant.store.get(item["id"], "chat", "owner")["status"] in {
        "cancelled",
        "interrupted",
    }


def test_recovery_retries_after_crash_between_marking_and_reading_receipt(tmp_path):
    store = ProposalStore(tmp_path / "assistant.sqlite3")
    item = store.create("research", "chat", "owner", {"job_id": "123456abcdef"})
    store.activate(item["id"])
    store.claim(item["id"], "chat", "owner")
    store.update(item["id"], "running")
    # The first recovering process exits after this durable state transition,
    # before checking the trusted runner's already written result receipt.
    store.recover()
    runner = Mock()
    runner.recover_result.return_value = {
        "status": "completed",
        "summary": "已核验落盘结果",
        "artifacts": [],
    }
    restarted = FeishuAssistant(
        {},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=Mock(),
        research=runner,
        client=Mock(),
        store=store,
        start_workers=False,
    )
    try:
        runner.recover_result.assert_called_once_with(item["payload"])
        restored = store.get(item["id"], "chat", "owner")
        assert restored["status"] == "succeeded"
        assert restored["result"]["status"] == "succeeded"
        runner.run.assert_not_called()
        assert restarted._jobs.empty()
    finally:
        restarted.stop()


def _feishu_app():
    return FeishuApp(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": "test-app",
                    "app_secret": "test-secret",
                    "allowed_chat_ids": ["chat"],
                    "assistant": {"enabled": True},
                }
            }
        }
    )


def test_feishu_stop_owns_assistant_created_during_initialization():
    app = _feishu_app()
    constructor_entered = threading.Event()
    release_constructor = threading.Event()
    created = Mock()

    def construct(*_args, **_kwargs):
        constructor_entered.set()
        assert release_constructor.wait(3)
        return created

    with patch(
        "src.interactive.assistant.service.FeishuAssistant", side_effect=construct
    ):
        initializer = threading.Thread(target=app.get_assistant, daemon=True)
        stopper = threading.Thread(target=app.stop, daemon=True)
        initializer.start()
        try:
            assert constructor_entered.wait(2)
            stopper.start()
            # stop() has set its flag while construction is still outstanding.
            # A locking implementation may wait until construction completes.
            app._stopped.wait(1)
        finally:
            release_constructor.set()
            initializer.join(3)
            if stopper.ident is not None:
                stopper.join(3)
    assert not initializer.is_alive()
    assert not stopper.is_alive()
    created.stop.assert_called()
    assert app.get_assistant() is None


def test_legacy_command_remains_available_after_natural_language_failure():
    app = _feishu_app()
    app.send_message = Mock(return_value=(True, "ok"))
    app.get_assistant = Mock(side_effect=RuntimeError("assistant storage unavailable"))
    app.command_executor.execute = Mock()

    def event(text, message_id):
        return {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
                "message": {
                    "chat_id": "chat",
                    "message_id": message_id,
                    "chat_type": "p2p",
                    "content": json.dumps({"text": text}),
                },
            },
        }

    try:
        app.validate_config()
        assert handle_feishu_message(app, event("分析最近的策略", "nl-1")) == "accepted"
        assert handle_feishu_message(app, event("/help", "command-1")) == "accepted"
        app.command_executor.execute.assert_called_once()
        assert handle_feishu_message(app, event("/help", "command-1")) == "duplicate"
        app.command_executor.execute.assert_called_once()
    finally:
        app.stop()
