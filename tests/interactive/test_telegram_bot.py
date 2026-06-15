"""Telegram Bot 测试 — Mock API 层验证。"""

from unittest.mock import Mock, patch

from src.interactive.telegram_bot import TelegramBot


def _make_bot_config(allowed_ids=None):
    return {
        "interactive": {
            "telegram": {
                "bot_token": "test-token",
                "allowed_chat_ids": allowed_ids or ["123"],
                "polling_interval": 2,
                "rate_limit_per_minute": 10,
            }
        }
    }


class TestTelegramBot:
    def test_run_starts_without_token_does_not_crash(self):
        bot = TelegramBot({"interactive": {"telegram": {}}})
        bot.run()  # should log error and return immediately

    def test_bot_dispatches_help(self):
        bot = TelegramBot(_make_bot_config())
        with patch.object(bot, "_api", return_value=True) as mock_api:
            bot._send_message("123", "test")
            mock_api.assert_called_once()

    def test_bot_rejects_unauthorized_chat(self):
        bot = TelegramBot(_make_bot_config(allowed_ids=["999"]))
        assert bot.gate.is_allowed("123") is False
        assert bot.gate.is_allowed("999") is True

    def test_bot_rate_limits(self):
        bot = TelegramBot(_make_bot_config())
        bot.rate_limiter = __import__(
            "src.interactive.security", fromlist=["RateLimiter"]
        ).RateLimiter(max_per_minute=3)
        for _ in range(3):
            assert bot.rate_limiter.check("user") is True
        assert bot.rate_limiter.check("user") is False
