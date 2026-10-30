"""Both bot transports expose the same application command handlers."""

import json
import threading
from unittest.mock import Mock, patch

import pytest

from src.interactive import command_parser as commands
from src.interactive.command_dispatcher import CommandExecutor, dispatch_command
from src.interactive.commands import handlers
from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message
from src.interactive.telegram_bot import TelegramBot


def config():
    return {
        "interactive": {
            "feishu": {
                "enabled": True,
                "app_id": "test",
                "app_secret": "test",
                "allowed_chat_ids": ["allowed"],
            },
            "telegram": {
                "enabled": True,
                "bot_token": "test",
                "allowed_chat_ids": ["allowed"],
            },
        }
    }


def event(text, message_id="message-1", chat_id="allowed"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user"},
            "message": {
                "chat_id": chat_id,
                "message_id": message_id,
                "content": json.dumps({"text": text}),
            },
        },
    }


@pytest.mark.parametrize(
    "text,handler,args",
    [
        ("/daily", "handle_daily", ()),
        ("/brief", "handle_brief", ("morning_snapshot",)),
        ("/brief afternoon", "handle_brief", ("afternoon_snapshot",)),
        ("/schedule", "handle_schedule", ("view", "", "")),
        ("/schedule daily 20:30", "handle_schedule", ("set", "daily", "20:30")),
        ("/add 600036,GOOG", "handle_add", (["600036", "GOOG"],)),
        ("/remove 600036,GOOG", "handle_remove", (["600036", "GOOG"],)),
        ("/list", "handle_list", ()),
        ("/optimize a_share", "handle_optimize", ("a_share",)),
    ],
)
def test_both_transports_dispatch_same_commands(text, handler, args):
    app = FeishuApp(config())
    bot = TelegramBot(config())
    with patch.object(handlers, handler, return_value="ok") as call, patch.object(
        app, "send_message"
    ), patch.object(bot, "_send_message"):
        assert handle_feishu_message(app, event(text)) == "accepted"
        bot._process_update(
            {"message": {"chat": {"id": "allowed"}, "text": text, "message_id": 1}}
        )
    assert call.call_count == 2
    assert all(item.args == args for item in call.call_args_list)


@pytest.mark.parametrize(
    "command,handler,args,kwargs",
    [
        (commands.HelpCommand(), "handle_help", (), {}),
        (commands.SaveCommand(), "handle_save", (), {}),
        (
            commands.BacktestCommand("600036", "2023-01-01", "2026-01-01"),
            "handle_backtest",
            ("600036", "2023-01-01", "2026-01-01"),
            {},
        ),
        (
            commands.DailyReportFrequencyCommand("weekly"),
            "handle_daily_report_frequency",
            ("weekly",),
            {},
        ),
        (commands.AlertsCommand(), "handle_alerts", (), {}),
        (commands.ResetAlertsCommand("600036"), "handle_reset_alerts", ("600036",), {}),
        (commands.ModeCommand("frac"), "handle_mode", ("frac",), {}),
        (commands.ConfigCommand("show", "", ""), "handle_config", ("show", "", ""), {}),
        (
            commands.SkipCommand("search", ["600036"], True),
            "handle_skip",
            ("search", ["600036"]),
            {"remove": True},
        ),
        (
            commands.SwitchOptimizerCommand("percentile", "us"),
            "handle_switch_optimizer",
            ("percentile", "us"),
            {},
        ),
        (commands.RefDateCommand("2025-01-01"), "handle_ref_date", ("2025-01-01",), {}),
        (
            commands.RefPositionCommand("set", "a_share", "600036", 100, 10),
            "handle_ref_position",
            ("set", "a_share", "600036", 100, 10),
            {},
        ),
    ],
)
def test_remaining_existing_commands_are_preserved(command, handler, args, kwargs):
    with patch.object(handlers, handler, return_value="ok") as call:
        assert dispatch_command(command) == "ok"
    call.assert_called_once_with(*args, **kwargs)


def test_error_command_has_no_side_effects():
    assert dispatch_command(commands.ErrorCommand("bad input")) == "❌ bad input"


def test_feishu_access_state_survives_multiple_events():
    settings = config()
    settings["interactive"]["feishu"]["rate_limit_per_minute"] = 1
    app = FeishuApp(settings)
    with patch.object(
        handlers, "handle_daily", return_value="ok"
    ) as daily, patch.object(app, "send_message"):
        assert (
            handle_feishu_message(app, event("/daily", "one", "other"))
            == "unauthorized"
        )
        assert handle_feishu_message(app, event("/daily", "one")) == "accepted"
        assert handle_feishu_message(app, event("/daily", "one")) == "duplicate"
        assert handle_feishu_message(app, event("/daily", "two")) == "rate_limited"
    daily.assert_called_once_with()


def test_only_explicit_feishu_wildcard_accepts_other_chats():
    settings = config()
    settings["interactive"]["feishu"]["allowed_chat_ids"] = ["*"]
    settings["interactive"]["feishu"]["rate_limit_per_minute"] = 1
    app = FeishuApp(settings)
    app.validate_config()
    with patch.object(
        handlers, "handle_daily", return_value="ok"
    ) as daily, patch.object(app, "send_message"):
        assert handle_feishu_message(app, event("/daily", "one", "other")) == "accepted"
        assert (
            handle_feishu_message(app, event("/daily", "one", "other")) == "duplicate"
        )
        assert (
            handle_feishu_message(app, event("/daily", "two", "other"))
            == "rate_limited"
        )
    daily.assert_called_once_with()

    settings["interactive"]["feishu"]["allowed_chat_ids"] = []
    empty_app = FeishuApp(settings)
    with pytest.raises(ValueError, match="allowed_chat_ids"):
        empty_app.validate_config()
    assert (
        handle_feishu_message(empty_app, event("/daily", "one", "other")) == "disabled"
    )


@pytest.mark.parametrize(
    "override",
    [{"enabled": False}, {"allowed_chat_ids": []}, {"app_id": "", "app_secret": ""}],
)
def test_feishu_disabled_or_unconfigured_never_dispatches(override, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    settings = config()
    settings["interactive"]["feishu"].update(override)
    app = FeishuApp(settings)
    with patch.object(handlers, "handle_daily") as daily, patch.object(
        app, "send_message"
    ) as send:
        assert handle_feishu_message(app, event("/daily")) == "disabled"
    daily.assert_not_called()
    send.assert_not_called()


def test_backtest_does_not_block_receiver_or_create_unbounded_workers():
    reply = Mock()
    executor = CommandExecutor(reply)
    started = threading.Event()
    finish = threading.Event()
    returned = threading.Event()

    def slow_backtest(*args):
        started.set()
        assert finish.wait(2)
        returned.set()
        return "done"

    command = commands.BacktestCommand("600036", "2023-01-01", "2026-01-01")
    with patch.object(handlers, "handle_backtest", side_effect=slow_backtest) as call:
        executor.execute("allowed", command)
        assert started.wait(1)
        executor.execute("allowed", command)
        assert call.call_count == 1
        assert "已有回测" in reply.call_args.args[1]
        executor.stop()
        finish.set()
        assert returned.wait(1)
    assert all(item.args[1] != "done" for item in reply.call_args_list)
