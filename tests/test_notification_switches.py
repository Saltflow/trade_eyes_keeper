"""No transport may bypass notification switches, including direct sends."""

from unittest.mock import Mock

import pytest

from src.notification.email_notifier import EmailNotifier
from src.notification.feishu_notifier import FeishuNotifier
from src.notification.manager import NotifierManager
from src.notification.settings import env_flag
from src.notification.telegram_notifier import TelegramNotifier


@pytest.fixture(autouse=True)
def clean_skip_flags(monkeypatch):
    for name in ("SKIP_NOTIFICATIONS", "SKIP_EMAIL", "SKIP_FEISHU", "SKIP_TELEGRAM"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", " yes ", "on"])
def test_boolean_skip_spellings(value, monkeypatch):
    monkeypatch.setenv("SKIP_NOTIFICATIONS", value)
    assert env_flag("SKIP_NOTIFICATIONS")
    manager = NotifierManager({"notification": {"email": {"enabled": True}}})
    assert manager.send_deployment_notification("SUCCESS") == {}


@pytest.mark.parametrize("channel", ["email", "feishu", "telegram"])
@pytest.mark.parametrize("switch", ["global", "channel", "disabled"])
def test_direct_sends_cannot_bypass_switches(channel, switch, tmp_path, monkeypatch):
    config = {
        "email": {"smtp_server": "smtp.invalid"},
        "storage": {"data_dir": str(tmp_path)},
        "notification": {
            "email": {"enabled": True},
            "feishu": {"enabled": True, "webhook_url": "https://hook.invalid/test"},
            "telegram": {"enabled": True, "bot_token": "test-token", "chat_id": "42"},
        },
    }
    transports = [Mock(side_effect=AssertionError("transport must not run")) for _ in range(3)]
    monkeypatch.setattr("requests.post", transports[0])
    monkeypatch.setattr("smtplib.SMTP", transports[1])
    monkeypatch.setattr("smtplib.SMTP_SSL", transports[2])
    if switch == "disabled":
        config["notification"][channel]["enabled"] = False
    else:
        name = "SKIP_NOTIFICATIONS" if switch == "global" else f"SKIP_{channel.upper()}"
        monkeypatch.setenv(name, "YES")
    if channel == "email":
        notifier = EmailNotifier(config)
        archive = Mock(return_value=tmp_path / "report.html")
        monkeypatch.setattr(notifier, "_save_email_copy", archive)
        notifier._send_email("test", "<p>test</p>")
        archive.assert_called_once()
    elif channel == "feishu":
        notifier = FeishuNotifier(config)
        notifier._send("test", "test")
        notifier._send_card({"test": "card"})
    else:
        TelegramNotifier(config)._send("test", "test")
    for transport in transports:
        transport.assert_not_called()


def test_deployment_results_distinguish_failed_and_delivered_channels():
    manager = NotifierManager({})
    manager.email = Mock()
    manager.email.send_deployment_notification.return_value = (False, "not delivered")
    manager.feishu = Mock()
    manager.feishu.send_deployment_notification.return_value = (True, "delivered")
    assert manager.send_deployment_notification("SUCCESS") == {
        "email": False,
        "feishu": True,
    }
