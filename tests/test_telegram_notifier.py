"""Telegram 通知器 — 传输层 + 内容层测试"""

from unittest.mock import Mock, patch

from src.search.artifacts import OptimizerGroupSummary, OptimizerRunSummary
from src.notification.email_notifier import build_optimizer_summary
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
                        "bot_token": "xxx",
                        "chat_id": "yyy",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.side_effect = TimeoutError("timeout")
            ok, msg = notifier._send("标题", "正文")
            assert not ok

    def test_transport_error_redacts_bot_token(self, caplog):
        """第三方异常中的请求 URL 不能把 bot token 写入日志或返回值。"""
        token = "123456:SECRET-TOKEN"
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": token,
                        "chat_id": "yyy",
                    }
                }
            }
        )
        error = RuntimeError(
            f"connection failed: https://api.telegram.org/bot{token}/sendMessage"
        )

        with patch("requests.post", side_effect=error):
            ok, msg = notifier._send("标题", "正文")

        assert not ok
        assert token not in msg
        assert token not in caplog.text
        assert "<redacted>" in msg

    def test_send_telegram_not_ok_returns_false(self):
        """Telegram ok=False 返回失败"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx",
                        "chat_id": "yyy",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "ok": False,
                "description": "chat not found",
            }
            ok, msg = notifier._send("标题", "正文")
            assert not ok

    def test_http_error_returns_false(self):
        """非 200 返回失败"""
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx",
                        "chat_id": "yyy",
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
                        "bot_token": "xxx",
                        "chat_id": "yyy",
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
        session.ref_portfolio_status = None
        notifier = TelegramNotifier(
            {
                "notification": {
                    "telegram": {
                        "bot_token": "xxx",
                        "chat_id": "yyy",
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
                        "bot_token": "xxx",
                        "chat_id": "yyy",
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

    def test_optimizer_summary_replaces_unsupported_br_tags(self):
        """Optimizer summary keeps supported HTML but never sends <br> to Telegram."""
        notifier = TelegramNotifier(
            {"notification": {"telegram": {"bot_token": "xxx", "chat_id": "yyy"}}}
        )
        report = OptimizerRunSummary(
            strategy_name="percentile",
            strategy_label="Percentile",
            timestamp="2026-07-26T10:12:40",
            elapsed_seconds=12,
            groups={
                "a_share": OptimizerGroupSummary(
                    group="a_share", candidate_count=10, wf_score=1.2, status="completed"
                )
            },
            activated=True,
        )
        with patch.object(notifier, "_send", return_value=(True, "ok")) as mock_send:
            notifier.send_optimizer_notification(report, "Percentile")
            _, body = mock_send.call_args[0]
            assert "<br" not in body.lower()
            assert "\n" in body
            assert "<b>" in body

    def test_optimizer_summary_keeps_validation_and_search_budget(self):
        """Unified three-market payload must not collapse to the survivor count."""
        report = OptimizerRunSummary(
            strategy_name="percentile",
            strategy_label="Percentile",
            timestamp="2026-07-26T10:12:40",
            elapsed_seconds=12,
            groups={
                "a_share": OptimizerGroupSummary(
                    group="a_share",
                    candidate_count=5000,
                    evaluated_count=155000,
                    survivor_count=5000,
                    wf_score=1.2,
                    status="completed",
                    ranking_diagnostics={
                        "weighted_strategy_return": 3.5,
                        "positive_return_windows": 9,
                        "ranking_window_count": 13,
                    },
                    sensitivity={"selection_score": 0.8},
                    validation={
                        "total_return": 12.3,
                        "excess_return": 7.8,
                        "max_drawdown": -5.0,
                        "sharpe_ratio": 1.1,
                        "trade_count": 8,
                        "avg_cash_pct": 30.0,
                        "benchmark_returns": {"510300": 4.0, "risk_free": 1.0},
                        "benchmark_win_rates": {"510300": 55.0, "risk_free": 70.0},
                        "latest_holdings": {
                            "quarter": 3,
                            "nav": 112300.0,
                            "positions": [{"code": "601728", "shares": 100.0}],
                        },
                        "weekly_ohlc": {
                            "labels": ["2026-W25"], "close": [112300.0]
                        },
                    },
                )
            },
            activated=True,
        )

        body = build_optimizer_summary(report)
        assert "Ranking absolute return: weighted +3.50% | positive windows 9/13" in body
        assert "Robust selection score: +0.800" in body

        assert "配置搜索评估 155,000 次" in body
        assert "最终入围 5,000 个" in body
        assert "验证期回测" in body
        assert "期末持仓" in body
        assert "验证期 NAV 周线" in body


def _make_brief_df():
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "stock_code": "601728",
                "stock_name": "中国电信",
                "date": pd.Timestamp.today().normalize(),
                "open": 5.70,
                "close": 5.76,
                "ma60": 5.91,
                "wma20": 5.78,
                "wma30": 5.65,
                "wma50": 5.50,
            },
            {
                "stock_code": "00883",
                "stock_name": "中海油",
                "date": pd.Timestamp.today().normalize(),
                "open": 18.50,
                "close": 18.62,
                "ma60": 18.00,
                "wma20": 18.40,
                "wma30": 17.80,
                "wma50": 17.50,
            },
        ]
    )
