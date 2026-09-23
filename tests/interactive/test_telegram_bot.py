"""Telegram receiver-to-handler tests with file writes and API traffic mocked."""

import threading
from unittest.mock import patch

import pytest
import yaml

from src.interactive.commands import handlers
from src.interactive.telegram_bot import TelegramBot


def _make_bot_config(extra=None):
    return {
        "interactive": {
            "telegram": {
                "enabled": True,
                "bot_token": "test-token",
                "allowed_chat_ids": ["123"],
                "polling_interval": 2,
                "rate_limit_per_minute": 10,
                **(extra or {}),
            }
        }
    }


def _update(text, message_id=1, chat_id="123"):
    return {
        "update_id": message_id,
        "message": {"chat": {"id": chat_id}, "text": text, "message_id": message_id},
    }


@pytest.fixture(autouse=True)
def no_delivery_environment(monkeypatch):
    for name in (
        "SKIP_NOTIFICATIONS",
        "SKIP_TELEGRAM",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "extra",
    [
        {"enabled": False},
        {"bot_token": ""},
        {"allowed_chat_ids": []},
        {"allowed_chat_ids": ["*"]},
        {"allowed_chat_ids": "123"},
    ],
)
def test_run_requires_enabled_credentials_and_allowlist(extra):
    bot = TelegramBot(_make_bot_config(extra))
    with patch("requests.post") as post, pytest.raises(ValueError):
        bot.run()
    post.assert_not_called()


def test_empty_allowlist_does_not_inherit_notification_recipient(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    bot = TelegramBot(_make_bot_config({"allowed_chat_ids": []}))
    with pytest.raises(ValueError):
        bot.validate_config()


def test_add_keeps_whole_codes_through_real_dispatch(tmp_path, monkeypatch):
    bot = TelegramBot(_make_bot_config())
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"stocks": []}), encoding="utf-8")
    monkeypatch.setattr(handlers, "CONFIG_PATH", config_path)
    with patch.object(bot, "_send_message"):
        bot._process_update(_update("/add 600036,GOOG"))
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["stocks"] == [
        "600036",
        "GOOG",
    ]


def test_remove_keeps_whole_codes_through_real_dispatch(tmp_path, monkeypatch):
    bot = TelegramBot(_make_bot_config())
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"stocks": ["600036", "GOOG"]}), encoding="utf-8"
    )
    monkeypatch.setattr(handlers, "CONFIG_PATH", config_path)
    with patch.object(bot, "_send_message"):
        bot._process_update(_update("/remove 600036,GOOG"))
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["stocks"] == []


def test_unauthorized_disabled_and_duplicate_updates_do_not_dispatch():
    bot = TelegramBot(_make_bot_config())
    with patch.object(
        handlers, "handle_daily", return_value="ok"
    ) as daily, patch.object(bot, "_send_message"):
        bot._process_update(_update("/daily", chat_id="other"))
        bot._process_update(_update("/daily"))
        bot._process_update(_update("/daily"))
        bot.stop()
        bot._process_update(_update("/daily", message_id=2))
    daily.assert_called_once_with()


@pytest.mark.parametrize("flag", ["SKIP_NOTIFICATIONS", "SKIP_TELEGRAM"])
def test_skip_flag_blocks_low_level_message_send(monkeypatch, flag):
    monkeypatch.setenv(flag, "YES")
    bot = TelegramBot(_make_bot_config())
    with patch("requests.post") as post:
        assert not bot._send_message("123", "hello")
    post.assert_not_called()


def test_stop_interrupts_idle_polling_wait():
    bot = TelegramBot(_make_bot_config({"polling_interval": 3600}))
    polled = threading.Event()
    with patch.object(
        bot, "_get_updates", side_effect=lambda offset: polled.set() or []
    ):
        worker = threading.Thread(target=bot.run)
        worker.start()
        assert polled.wait(1)
        bot.stop()
        bot.stop()
        worker.join(1)
        assert not worker.is_alive()


def test_long_poll_transport_has_bounded_timeouts():
    bot = TelegramBot(_make_bot_config())
    with patch("requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"ok": True, "result": []}
        assert bot._get_updates(0) == []
        assert post.call_args.kwargs["timeout"] == (5, 10)
        assert post.call_args.kwargs["data"]["timeout"] == 5
