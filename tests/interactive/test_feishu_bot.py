"""Official SDK wiring and owned receiver lifecycle without network access."""

import asyncio
import json
import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.interactive.feishu_bot import FeishuBot, _run_sdk
from src.interactive.feishu_handler import handle_feishu_message


@pytest.fixture(autouse=True)
def isolated_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.core.report_execution.runtime_path",
        lambda config: tmp_path / "inbox.sqlite3",
    )


@pytest.fixture
def inbox_settings(tmp_path):
    return {
        "path": str(tmp_path / "inbox.sqlite3"),
        "app_id": "test-id",
        "allowed_chat_ids": ["allowed"],
    }


def _config():
    return {
        "interactive": {
            "feishu": {
                "enabled": True,
                "app_id": "test-id",
                "app_secret": "test-secret",
                "allowed_chat_ids": ["allowed"],
            }
        }
    }


def test_sdk_child_uses_public_dispatcher_and_its_own_event_loop(inbox_settings):
    events = queue.Queue()
    builder = Mock()
    builder.register_p2_im_message_receive_v1.return_value = builder
    fake_sdk = SimpleNamespace(
        EventDispatcherHandler=SimpleNamespace(builder=Mock(return_value=builder)),
        JSON=SimpleNamespace(marshal=json.dumps),
        LogLevel=SimpleNamespace(WARNING="warning"),
        ws=SimpleNamespace(Client=Mock()),
    )
    loop = asyncio.new_event_loop()
    with patch.dict(sys.modules, {"lark_oapi": fake_sdk}), patch(
        "src.interactive.feishu_bot.asyncio.new_event_loop", return_value=loop
    ):
        _run_sdk("test-id", "test-secret", events, inbox_settings)
    fake_sdk.EventDispatcherHandler.builder.assert_called_once_with("", "")
    callback = builder.register_p2_im_message_receive_v1.call_args.args[0]
    callback(
        {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"message": {"chat_id": "allowed", "message_id": "test-message"}},
        }
    )
    assert events.get_nowait()[0] == "wake"
    from src.interactive.ingress import DurableIngress

    assert len(DurableIngress(**inbox_settings).pending()) == 1
    fake_sdk.ws.Client.return_value.start.assert_called_once_with()
    assert loop.is_closed()
    asyncio.set_event_loop(None)


def test_missing_sdk_is_reported_before_start():
    bot = FeishuBot(_config())
    with patch("importlib.util.find_spec", return_value=None), pytest.raises(
        RuntimeError, match="requires lark-oapi"
    ):
        bot.validate_config()


def test_pause_preserves_new_durable_raw_for_the_next_receiver(
    inbox_settings, monkeypatch
):
    from src.interactive.ingress import DurableIngress

    bot = FeishuBot(_config())
    inbox = DurableIngress(**inbox_settings)
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"chat_id": "allowed", "message_id": "paused-raw"}},
    }
    bot.pause_admission()
    assert inbox.accept(payload)
    dispatch = Mock(return_value="accepted")
    monkeypatch.setattr("src.interactive.feishu_bot.handle_feishu_message", dispatch)
    bot._dispatch_pending(inbox)
    dispatch.assert_not_called()
    assert len(inbox.pending()) == 1
    restarted = FeishuBot(_config())
    restarted._dispatch_pending(inbox)
    dispatch.assert_called_once_with(restarted.app, payload)
    assert not inbox.pending()
    bot.stop()
    restarted.stop()


def test_pause_waits_for_in_progress_admission_without_closing_app(monkeypatch):
    bot = FeishuBot(_config())
    entered, release, paused = threading.Event(), threading.Event(), threading.Event()
    inbox = SimpleNamespace(dispatch=lambda *_args: (entered.set(), release.wait(3)))
    app_stop = Mock()
    monkeypatch.setattr(bot.app, "stop", app_stop)
    worker = threading.Thread(target=lambda: bot._dispatch_pending(inbox))
    worker.start()
    assert entered.wait(1)
    pause = threading.Thread(target=lambda: (bot.pause_admission(), paused.set()))
    pause.start()
    assert not paused.wait(0.05)
    app_stop.assert_not_called()
    release.set()
    worker.join(2)
    pause.join(2)
    assert paused.is_set()
    app_stop.assert_not_called()


def test_public_health_does_not_claim_websocket_connectivity_or_fail_after_close():
    bot = FeishuBot(_config())
    process = Mock()
    process.is_alive.side_effect = ValueError("process object is closed")
    bot._process = process
    bot._last_sdk_pid = 1234
    result = bot.health()
    assert result["connected"] == "unknown" and result["receiver_liveness"] == "exited"
    assert result["sdk_pid"] == 1234


@pytest.mark.parametrize("startup_fails", [False, True])
def test_sdk_cache_tasks_are_drained_before_closing_loop(startup_fails, inbox_settings):
    """The real SDK creates a cache cleanup task while constructing its client."""
    tasks = []
    builder = Mock()
    builder.register_p2_im_message_receive_v1.return_value = builder

    def start():
        loop = asyncio.get_event_loop()
        tasks.append(loop.create_task(asyncio.sleep(3600)))
        if startup_fails:
            raise ValueError("test startup failure")

    client = Mock()
    client.start.side_effect = start
    fake_sdk = SimpleNamespace(
        EventDispatcherHandler=SimpleNamespace(builder=Mock(return_value=builder)),
        LogLevel=SimpleNamespace(WARNING="warning"),
        ws=SimpleNamespace(Client=Mock(return_value=client)),
    )
    with patch.dict(sys.modules, {"lark_oapi": fake_sdk}):
        if startup_fails:
            with pytest.raises(RuntimeError, match="receiver failed"):
                _run_sdk("test-id", "test-secret", queue.Queue(), inbox_settings)
        else:
            _run_sdk("test-id", "test-secret", queue.Queue(), inbox_settings)
    assert tasks and all(task.cancelled() for task in tasks)
    assert all(task.get_loop().is_closed() for task in tasks)


def test_parent_dispatches_events_and_reaps_receiver_on_stop(inbox_settings):
    bot = FeishuBot(_config())
    context = Mock()
    bot._context = context
    events = context.Queue.return_value
    from src.interactive.ingress import DurableIngress

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"chat_id": "allowed", "message_id": "parent-message"}},
    }
    DurableIngress(**inbox_settings).accept(payload)
    events.get.return_value = ("wake", "hint")
    process = context.Process.return_value
    process.is_alive.side_effect = [True, False]
    with patch.object(bot, "validate_config"), patch(
        "src.interactive.feishu_bot.handle_feishu_message",
        side_effect=lambda app, payload: (bot.stop(), "accepted")[1],
    ) as dispatch:
        bot.run()
    dispatch.assert_called_once_with(bot.app, payload)
    process.terminate.assert_called_once_with()
    process.join.assert_called_once_with(timeout=3)
    process.close.assert_called_once_with()
    events.close.assert_called_once_with()
    bot.stop()


def test_unexpected_sdk_exit_is_fatal_and_reaped():
    bot = FeishuBot(_config())
    bot._context = Mock()
    bot._context.Queue.return_value.get.side_effect = queue.Empty
    process = bot._context.Process.return_value
    process.is_alive.return_value = False
    process.exitcode = 1
    with patch.object(bot, "validate_config"), pytest.raises(
        RuntimeError, match="receiver exited"
    ):
        bot.run()
    process.join.assert_called_once_with(timeout=3)
    process.close.assert_called_once_with()


def test_stop_before_run_never_starts_receiver():
    bot = FeishuBot(_config())
    bot._context = Mock()
    bot.stop()
    bot.stop()
    with patch.object(bot, "validate_config"):
        bot.run()
    bot._context.Process.assert_not_called()


def test_unresponsive_receiver_gets_bounded_kill():
    bot = FeishuBot(_config())
    bot._context = Mock()
    bot._context.Queue.return_value.get.return_value = ("error", "MockSDKError")
    process = bot._context.Process.return_value
    process.is_alive.return_value = True
    with patch.object(bot, "validate_config"), pytest.raises(
        RuntimeError, match="MockSDKError"
    ):
        bot.run()
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert [call.kwargs["timeout"] for call in process.join.call_args_list] == [3, 2]


def test_real_spawn_receiver_delivers_locally_and_is_reaped(tmp_path, monkeypatch):
    """Exercise Windows spawn/IPC/terminate using a local SDK stub, never a socket."""
    (tmp_path / "lark_oapi.py").write_text(
        """
import json
import time
from types import SimpleNamespace

class EventDispatcherHandler:
    @staticmethod
    def builder(*args):
        return EventDispatcherHandler()

    def register_p2_im_message_receive_v1(self, callback):
        self.callback = callback
        return self

    def build(self):
        return self

class Client:
    def __init__(self, *args, event_handler, **kwargs):
        self.handler = event_handler

    def start(self):
        self.handler.callback({
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"message": {
                "chat_id": "allowed", "message_id": "spawn-message",
                "content": json.dumps({"text": "/daily"}),
            }},
        })
        while True:
            time.sleep(0.05)

ws = SimpleNamespace(Client=Client)
LogLevel = SimpleNamespace(WARNING="warning")
JSON = SimpleNamespace(marshal=json.dumps)
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    bot = FeishuBot(_config())
    dispatched = threading.Event()
    errors = []

    def handle_and_stop(app, payload):
        status = handle_feishu_message(app, payload)
        dispatched.set()
        bot.stop()
        return status

    def receive():
        try:
            bot.run()
        except BaseException as exc:  # noqa: BLE001 - return worker failures to pytest
            errors.append(exc)

    with patch(
        "src.interactive.feishu_bot.handle_feishu_message", handle_and_stop
    ), patch(
        "src.interactive.commands.handlers.handle_daily", return_value="ok"
    ) as daily, patch.object(bot.app, "send_message"):
        worker = threading.Thread(target=receive)
        worker.start()
        try:
            assert dispatched.wait(10), errors
        finally:
            bot.stop()
            worker.join(6)
        assert not worker.is_alive()
        assert not errors
        assert bot._process is None
        daily.assert_called_once_with()
