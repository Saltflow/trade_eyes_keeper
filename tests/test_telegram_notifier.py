"""Telegram 通知器 — 传输层 + 内容层测试"""

import pytest
from unittest.mock import Mock, patch, ANY

from src.notification.telegram_notifier import TelegramNotifier


class TestTelegramTransport:
    """传输层：HTTP POST 到 Telegram Bot API"""

    def test_send_posts_to_telegram_api(self):
        """断言 URL 含 bot token + chat_id"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "123456:ABC-DEF",
                        "chat_id": "-100123",
                        "parse_mode": "HTML",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"ok": True}
            ok, msg = notifier._send("test标题", "<b>test正文</b>")
            assert ok
            mock_post.assert_called_once()
            url = mock_post.call_args[0][0]
            assert "bot123456:ABC-DEF" in url
            assert "sendMessage" in url
            data = mock_post.call_args[1]["data"]
            assert data["chat_id"] == "-100123"
            assert data["parse_mode"] == "HTML"

    def test_send_timeout_does_not_raise(self):
        """断联/超时时不抛异常"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx", "chat_id": "yyy",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.side_effect = TimeoutError("timeout")
            ok, msg = notifier._send("标题", "正文")
            assert not ok

    def test_send_telegram_not_ok_returns_false(self):
        """Telegram ok=False 返回失败"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx", "chat_id": "yyy",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"ok": False, "description": "chat not found"}
            ok, msg = notifier._send("标题", "正文")
            assert not ok

    def test_http_error_returns_false(self):
        """非 200 返回失败"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx", "chat_id": "yyy",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 403
            ok, msg = notifier._send("标题", "正文")
            assert not ok

    def test_message_too_long_splits(self):
        """消息超过 4096 字符时分片发送"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx", "chat_id": "yyy",
                    }
                }
            }
        )
        long_body = "A" * 5000
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"ok": True}
            ok, msg = notifier._send("标题", long_body)
            assert ok
            assert mock_post.call_count >= 2  # 至少分2片


class TestTelegramContent:
    """内容层：简报/日报消息格式"""

    def test_brief_report_sends_html(self):
        """简报按偏离率排序并包含 HTML 格式"""
        session = Mock()
        session.get_all_dataframe.return_value = _make_brief_df()
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx", "chat_id": "yyy",
                        "parse_mode": "HTML",
                    }
                }
            }
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            notifier.send_brief_report(session, {"id": "morning", "label": "早盘简报"})
            mock_send.assert_called_once()
            title, body = mock_send.call_args[0]
            assert "早盘简报" in title
            # 应包含 HTML 标签
            assert "<code>" in body or "<b>" in body or "<pre>" in body

    def test_deployment_notification_sends_text(self):
        """部署通知发送成功"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx", "chat_id": "yyy",
                    }
                }
            }
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            ok, msg = notifier.send_deployment_notification(
                status="SUCCESS", version="abc1234", summary="部署完成"
            )
            assert ok
            mock_send.assert_called_once()


def _make_brief_df():
    import pandas as pd
    return pd.DataFrame([
        {
            "stock_code": "601728", "stock_name": "中国电信",
            "date": pd.Timestamp.today().normalize(),
            "open": 5.70, "close": 5.76,
            "ma60": 5.91, "wma20": 5.78, "wma30": 5.65, "wma50": 5.50,
        },
        {
            "stock_code": "00883", "stock_name": "中海油",
            "date": pd.Timestamp.today().normalize(),
            "open": 18.50, "close": 18.62,
            "ma60": 18.00, "wma20": 18.40, "wma30": 17.80, "wma50": 17.50,
        },
    ])
