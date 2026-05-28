"""NotifierManager — 编排层测试"""

import pytest
from unittest.mock import Mock, patch

from src.notification.manager import NotifierManager


class TestNotifierManagerCreation:
    """channel 创建逻辑"""

    def test_only_enabled_channels_created(self):
        """config 只启 email + feishu → 创建 2 个"""
        config = {
            "notification": {
                "email": {"enabled": True, "smtp_server": "smtp.test.com"},
                "feishu": {"enabled": True, "webhook_url": "https://hook"},
                "telegram": {"enabled": False, "bot_token": "x", "chat_id": "y"},
            }
        }
        manager = NotifierManager(config)
        assert manager.email is not None
        assert manager.feishu is not None
        assert manager.telegram is None  # disabled

    def test_all_disabled_returns_no_notifiers(self):
        """全禁用 → 所有 channel 为 None"""
        config = {
            "notification": {
                "email": {"enabled": False},
                "feishu": {"enabled": False, "webhook_url": ""},
                "telegram": {"enabled": False},
            }
        }
        manager = NotifierManager(config)
        assert manager.email is None
        assert manager.feishu is None
        assert manager.telegram is None

    def test_missing_config_does_not_crash(self):
        """无 notification section 不崩"""
        manager = NotifierManager({})
        assert manager.email is None


class TestNotifierManagerDispatch:
    """分发逻辑"""

    def test_notify_all_calls_each_channel(self):
        """3 channel 全部启用 → 3 次 _send 各被调用"""
        with patch("src.notification.email_notifier.EmailNotifier") as MockEmail, \
             patch("src.notification.feishu_notifier.FeishuNotifier") as MockFeishu, \
             patch("src.notification.telegram_notifier.TelegramNotifier") as MockTelegram:
            mock_email = Mock()
            mock_feishu = Mock()
            mock_telegram = Mock()
            mock_feishu._send.return_value = (True, "ok")
            mock_telegram._send.return_value = (True, "ok")
            MockEmail.return_value = mock_email
            MockFeishu.return_value = mock_feishu
            MockTelegram.return_value = mock_telegram

            config = {
                "notification": {
                    "email": {"enabled": True, "smtp_server": "s"},
                    "feishu": {"enabled": True, "webhook_url": "h"},
                    "telegram": {"enabled": True, "bot_token": "t", "chat_id": "c"},
                }
            }
            manager = NotifierManager(config)
            manager.send_deployment_notification("OK", "v1", "summary")

            mock_email.send_deployment_notification.assert_called_once_with("OK", "v1", "summary")
            mock_feishu.send_deployment_notification.assert_called_once()
            mock_telegram.send_deployment_notification.assert_called_once()

    def test_one_channel_fails_others_still_called(self):
        """1 channel 抛异常 → 其余 2 个正常完成"""
        with patch("src.notification.email_notifier.EmailNotifier") as MockEmail, \
             patch("src.notification.feishu_notifier.FeishuNotifier") as MockFeishu, \
             patch("src.notification.telegram_notifier.TelegramNotifier") as MockTelegram:
            mock_email = Mock()
            mock_telegram = Mock()
            mock_feishu = Mock()
            mock_feishu.send_deployment_notification.side_effect = RuntimeError("feishu down")
            mock_telegram.send_deployment_notification.return_value = (True, "ok")
            MockEmail.return_value = mock_email
            MockFeishu.return_value = mock_feishu
            MockTelegram.return_value = mock_telegram

            config = {
                "notification": {
                    "email": {"enabled": True, "smtp_server": "s"},
                    "feishu": {"enabled": True, "webhook_url": "h"},
                    "telegram": {"enabled": True, "bot_token": "t", "chat_id": "c"},
                }
            }
            manager = NotifierManager(config)
            manager.send_deployment_notification("OK", "v1", "summary")

            # Email 被调用，Telegram 也被调用（feishu 异常被捕获）
            mock_email.send_deployment_notification.assert_called_once()
            mock_telegram.send_deployment_notification.assert_called_once()

    def test_send_from_session_dispatches_all(self):
        """send_from_session 分发给所有 channel"""
        with patch("src.notification.email_notifier.EmailNotifier") as MockEmail, \
             patch("src.notification.feishu_notifier.FeishuNotifier") as MockFeishu:
            mock_email = Mock()
            mock_feishu = Mock()
            MockEmail.return_value = mock_email
            MockFeishu.return_value = mock_feishu

            config = {
                "notification": {
                    "email": {"enabled": True, "smtp_server": "s"},
                    "feishu": {"enabled": True, "webhook_url": "h"},
                }
            }
            manager = NotifierManager(config)
            session = Mock()
            manager.send_from_session(session)

            mock_email.send_from_session.assert_called_once_with(session)
            mock_feishu.send_from_session.assert_called_once_with(session)
