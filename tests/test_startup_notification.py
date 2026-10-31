"""A quiet release must keep normal Bot and scheduled delivery settings intact."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import main


def test_quiet_start_does_not_initialize_outbound_notifiers(monkeypatch):
    monkeypatch.setenv("SKIP_STARTUP_NOTIFICATION", "true")
    factory = Mock()
    monkeypatch.setattr(main, "NotifierManager", factory)
    config = {"notification": {"feishu": {"enabled": True}}}

    main._send_restart_notification(config, SimpleNamespace(scheduler=None))

    factory.assert_not_called()
    assert config["notification"]["feishu"]["enabled"] is True
    assert "SKIP_NOTIFICATIONS" not in os.environ


def test_normal_start_keeps_existing_notification_behavior(monkeypatch):
    monkeypatch.delenv("SKIP_STARTUP_NOTIFICATION", raising=False)
    notifier = Mock()
    factory = Mock(return_value=notifier)
    monkeypatch.setattr(main, "NotifierManager", factory)
    config = {}

    main._send_restart_notification(config, SimpleNamespace(scheduler=None))

    factory.assert_called_once_with(config)
    notifier.send_deployment_notification.assert_called_once_with(
        "SUCCESS", summary="服务已启动；交互 Bot 模式"
    )
