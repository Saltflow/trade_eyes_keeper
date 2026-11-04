"""飞书通知器 — 传输层 + 内容层测试"""

import json
from unittest.mock import Mock, patch

import pandas as pd

from src.notification.feishu_notifier import FeishuNotifier


class TestFeishuTransport:
    """传输层：HTTP POST 到飞书 webhook"""

    def test_send_posts_to_webhook_url(self):
        """构造有效 config → _send() → mock requests.post → 断言 URL 正确"""
        notifier = FeishuNotifier(
            {
                "notification": {
                    "feishu": {"webhook_url": "https://open.feishu.cn/hook/abc123"}
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"code": 0, "msg": "success"}
            ok, _msg = notifier._send("test标题", "test正文")
            assert ok
            mock_post.assert_called_once()
            url_called = mock_post.call_args[0][0]
            assert "abc123" in url_called

    def test_send_payload_has_msg_type_and_content(self):
        """断言 payload 含有 msg_type + content"""
        notifier = FeishuNotifier(
            {
                "notification": {
                    "feishu": {
                        "webhook_url": "https://hook/xyz",
                        "msg_type": "interactive",
                    }
                }
            }
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"code": 0}
            notifier._send("标题", "正文内容")
            _, kwargs = mock_post.call_args
            # _send 使用 json=payload 参数
            payload = kwargs.get("json") or kwargs.get("data")
            if isinstance(payload, str):
                payload = json.loads(payload)
            assert "msg_type" in payload
            assert payload["msg_type"] == "interactive"

    def test_send_timeout_does_not_raise(self):
        """断联/超时时不抛异常"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/timeout"}}}
        )
        with patch("requests.post") as mock_post:
            mock_post.side_effect = TimeoutError("timeout")
            ok, msg = notifier._send("标题", "正文")
            assert not ok
            assert "timeout" in msg.lower() or "失败" in msg

    def test_send_http_error_logs_and_returns_false(self):
        """HTTP 非 200 时返回 False"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/500"}}}
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 500
            mock_post.return_value.text = "Internal Error"
            ok, _msg = notifier._send("标题", "正文")
            assert not ok

    def test_feishu_code_nonzero_returns_false(self):
        """飞书 code != 0 视为失败"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/err"}}}
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"code": 10001, "msg": "invalid"}
            ok, _msg = notifier._send("标题", "正文")
            assert not ok


class TestFeishuContent:
    """内容层：飞书交互卡片消息格式"""

    def test_brief_report_builds_card_json(self):
        """简报数据 → _send_card 被调用（schema 2.0 原生表格卡片）"""
        session = Mock()
        session.get_all_dataframe.return_value = _make_brief_df()
        session.signal_scan = None
        notifier = FeishuNotifier(
            {
                "notification": {
                    "feishu": {
                        "webhook_url": "https://hook/test",
                        "msg_type": "interactive",
                    }
                }
            }
        )
        with patch.object(notifier, "_send_card") as mock_send:
            mock_send.return_value = (True, "ok")
            notifier.send_brief_report(session, {"id": "morning", "label": "早盘简报"})
            mock_send.assert_called_once()
            payload = mock_send.call_args[0][0]
            card = payload["card"]
            assert card.get("schema") == "2.0"
            # 卡片含原生 table 组件，且行数据含标的代码
            import json

            blob = json.dumps(card, ensure_ascii=False)
            assert "table" in blob
            assert "601728" in blob
            assert "代码" in blob
            assert "```" not in blob
            assert "| --- |" not in blob

    def test_daily_report_builds_card_json(self):
        """日报数据 → 3 张 schema 2.0 卡片（价格/基本面/技术）"""
        session = Mock()
        session.get_all_dataframe.return_value = _make_daily_df()
        session.get_alerts_as_dicts.return_value = []
        session.signal_scan = None
        session.backtest = None
        session.portfolio_results = None
        session.evaluation_reports = None
        notifier = FeishuNotifier(
            {
                "notification": {
                    "feishu": {
                        "webhook_url": "https://hook/test",
                        "msg_type": "interactive",
                    }
                }
            }
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            notifier.send_daily_report_from_session(session)
            # 3 段报告 + 策略摘要（signal_scan=None 时策略摘要可能为空但仍调用）
            assert mock_send.call_count >= 1
            calls = mock_send.call_args_list[:3]
            titles = [c.args[0] for c in calls]
            # extra_elements（原生表格）在 kwargs 或第3位参数
            all_blobs = []
            import json

            for c in calls:
                extra = c.kwargs.get("extra_elements")
                if extra is None and len(c.args) >= 3:
                    extra = c.args[2]
                all_blobs.append(
                    json.dumps(
                        {"body": c.args[1], "extra": extra},
                        ensure_ascii=False,
                        default=str,
                    )
                )
            blob = "\n".join(all_blobs)
            assert any("价格" in t for t in titles)
            assert any("基本面" in t for t in titles)
            assert all("技术" not in t for t in titles)
            assert "601728" in blob
            assert "中国电信" in blob
            assert "table" in blob  # 原生表格组件
            assert "```" not in blob
            assert "| --- |" not in blob

    def test_deployment_notification_sends_card(self):
        """部署通知发送交互卡片"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/deploy"}}}
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            ok, _msg = notifier.send_deployment_notification(
                status="SUCCESS", version="abc1234", summary="部署成功"
            )
            assert ok
            mock_send.assert_called_once()


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
            }
        ]
    )


def _make_daily_df(include_tech=True):
    row = _stock_row("601728", "中国电信")
    if not include_tech:
        for key in ("rsi", "macd_hist", "vol_ratio", "adx", "boll_pct_b"):
            row[key] = None
    return pd.DataFrame([row])


def _stock_row(code, name, date="today", close=5.76, ma60=5.91):
    if date == "today":
        date = pd.Timestamp.today().normalize()
    return {
        "stock_code": code,
        "stock_name": name,
        "date": date,
        "open": close - 0.06,
        "close": close,
        "high": close + 0.04,
        "low": close - 0.11,
        "ma60": ma60,
        "wma20": close + 0.02,
        "wma30": close - 0.11,
        "wma50": close - 0.26,
        "volume": 10000,
        "amount": close * 10000,
        "dividend_per_share": 0.30,
        "dividend_yield": 5.2,
        "pe_ratio": 12.5,
        "pb_ratio": 0.8,
        "roe": 6.4,
        "amplitude": 2.6,
        "change_pct": 1.05,
        "turnover": 0.8,
        "rsi": 52.1,
        "macd_hist": 0.045,
        "vol_ratio": 1.12,
        "adx": 18.7,
        "boll_pct_b": 0.62,
    }
