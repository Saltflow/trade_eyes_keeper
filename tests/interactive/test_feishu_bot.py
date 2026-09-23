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


def test_sdk_child_uses_public_dispatcher_and_its_own_event_loop():
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
        _run_sdk("test-id", "test-secret", events)
    fake_sdk.EventDispatcherHandler.builder.assert_called_once_with("", "")
    callback = builder.register_p2_im_message_receive_v1.call_args.args[0]
    callback({"header": {"event_type": "im.message.receive_v1"}})
    assert events.get_nowait()[0] == "message"
    fake_sdk.ws.Client.return_value.start.assert_called_once_with()
    assert loop.is_closed()
    asyncio.set_event_loop(None)


def test_missing_sdk_is_reported_before_start():
    bot = FeishuBot(_config())
    with patch("importlib.util.find_spec", return_value=None), pytest.raises(
        RuntimeError, match="requires lark-oapi"
    ):
        bot.validate_config()


@pytest.mark.parametrize("startup_fails", [False, True])
def test_sdk_cache_tasks_are_drained_before_closing_loop(startup_fails):
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
                _run_sdk("test-id", "test-secret", queue.Queue())
        else:
            _run_sdk("test-id", "test-secret", queue.Queue())
    assert tasks and all(task.cancelled() for task in tasks)
    assert all(task.get_loop().is_closed() for task in tasks)


def test_parent_dispatches_events_and_reaps_receiver_on_stop():
    bot = FeishuBot(_config())
    context = Mock()
    bot._context = context
    events = context.Queue.return_value
    events.get.return_value = ("message", {"event": "mocked"})
    process = context.Process.return_value
    process.is_alive.side_effect = [True, False]
    with patch.object(bot, "validate_config"), patch(
        "src.interactive.feishu_bot.handle_feishu_message",
        side_effect=lambda app, payload: bot.stop(),
    ) as dispatch:
        bot.run()
    dispatch.assert_called_once_with(bot.app, {"event": "mocked"})
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
        handle_feishu_message(app, payload)
        dispatched.set()
        bot.stop()

    def receive():
        try:
            bot.run()
        except BaseException as exc:  # noqa: BLE001 - return worker failures to pytest
            errors.append(exc)

    with patch("src.interactive.feishu_bot.handle_feishu_message", handle_and_stop), patch(
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
