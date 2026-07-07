"""飞书通知器 — 传输层 + 内容层测试"""

import json
from unittest.mock import Mock, patch

import pandas as pd

from src.notification.feishu_notifier import (
    FeishuNotifier,
    _build_plain_table,
    _calc_column_widths,
    _collect_report_entries,
    _display_width,
    _escape_cell,
    _extract_alert_codes,
    _fmt_num,
    _has_technical_data,
    _short_text,
    _truncate_display_width,
)


class TestFeishuTransport:
    """传输层：HTTP POST 到飞书 webhook"""

    def test_send_posts_to_webhook_url(self):
        """构造有效 config → _send() → mock requests.post → 断言 URL 正确"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://open.feishu.cn/hook/abc123"}}}
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"code": 0, "msg": "success"}
            ok, msg = notifier._send("test标题", "test正文")
            assert ok
            mock_post.assert_called_once()
            url_called = mock_post.call_args[0][0]
            assert "abc123" in url_called

    def test_send_payload_has_msg_type_and_content(self):
        """断言 payload 含有 msg_type + content"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/xyz", "msg_type": "interactive"}}}
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
            ok, msg = notifier._send("标题", "正文")
            assert not ok

    def test_feishu_code_nonzero_returns_false(self):
        """飞书 code != 0 视为失败"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/err"}}}
        )
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"code": 10001, "msg": "invalid"}
            ok, msg = notifier._send("标题", "正文")
            assert not ok


class TestFeishuContent:
    """内容层：飞书交互卡片消息格式"""

    def test_brief_report_builds_card_json(self):
        """简报数据 → _send 被调用且 body 非空（_send 内部将 body 包装为飞书交互卡片 JSON）"""
        session = Mock()
        session.get_all_dataframe.return_value = _make_brief_df()
        session.signal_scan = None
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/test", "msg_type": "interactive"}}}
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            notifier.send_brief_report(session, {"id": "morning", "label": "早盘简报"})
            mock_send.assert_called_once()
            title, body = mock_send.call_args[0]
            assert "早盘简报" in title
            assert body  # 正文非空
            assert "代码" in body
            assert "收盘" in body
            assert "偏离" in body
            assert "601728" in body
            assert "```" not in body
            assert "| --- |" not in body

    def test_daily_report_builds_card_json(self):
        """日报数据 → 飞书交互卡片结构"""
        session = Mock()
        session.get_all_dataframe.return_value = _make_daily_df()
        session.get_alerts_as_dicts.return_value = []
        # 日报需要这些属性
        session.signal_scan = None
        session.backtest = None
        session.portfolio_results = None
        # session 可能还需要 stock_data、announcements 等
        # 让 send_daily_report_from_session 调用 _build_email_body
        # 先确保不会因缺失字段而崩
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/test", "msg_type": "interactive"}}}
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            notifier.send_daily_report_from_session(session)
            assert mock_send.call_count == 3
            sent = [(call.args[0], call.args[1]) for call in mock_send.call_args_list]
            titles = [title for title, _ in sent]
            bodies = "\n".join(body for _, body in sent)
            assert any("价格" in title for title in titles)
            assert any("基本面" in title for title in titles)
            assert any("技术" in title for title in titles)
            assert "监控日报" in bodies
            assert "代码" in bodies
            assert "收盘" in bodies
            assert "RSI" in bodies
            assert "MACD_H" in bodies
            assert "601728" in bodies
            assert "中国电信" in bodies
            assert "```" not in bodies
            assert "| --- |" not in bodies
            assert "<table" not in bodies

    def test_deployment_notification_sends_card(self):
        """部署通知发送交互卡片"""
        notifier = FeishuNotifier(
            {"notification": {"feishu": {"webhook_url": "https://hook/deploy"}}}
        )
        with patch.object(notifier, "_send") as mock_send:
            mock_send.return_value = (True, "ok")
            ok, msg = notifier.send_deployment_notification(
                status="SUCCESS", version="abc1234", summary="部署成功"
            )
            assert ok
            mock_send.assert_called_once()


class TestFeishuSections:
    """报告分段构建。"""

    def test_build_sections_empty_when_no_active_rows(self):
        old_date = pd.Timestamp.today().normalize() - pd.Timedelta(days=10)
        sections = FeishuNotifier._build_report_sections(pd.DataFrame([
            _stock_row("601728", "中国电信", date=old_date)
        ]))

        assert len(sections) == 1
        assert sections[0][0] == "摘要"
        assert "本次没有可展示的活跃标的" in sections[0][1]

    def test_build_sections_without_tech_sends_two_cards(self):
        sections = FeishuNotifier._build_report_sections(_make_daily_df(include_tech=False))

        assert [label for label, _ in sections] == ["价格", "基本面"]

    def test_build_sections_with_tech_sends_three_cards(self):
        sections = FeishuNotifier._build_report_sections(_make_daily_df(include_tech=True))

        assert [label for label, _ in sections] == ["价格", "基本面", "技术"]
        assert "MACD_H" in sections[2][1]

    def test_build_sections_alert_mode_filters_entries(self):
        sections = FeishuNotifier._build_report_sections(
            _make_two_stock_daily_df(),
            alerts=[{"stock_code": "00883"}],
            alert_only=True,
        )

        bodies = "\n".join(body for _, body in sections)
        assert "00883" in bodies
        assert "中国海洋石油" in bodies
        assert "601728" not in bodies
        assert "中国电信" not in bodies


class TestFeishuEntries:
    """DataFrame → Feishu 行数据。"""

    def test_collect_entries_filters_by_date(self):
        old_date = pd.Timestamp.today().normalize() - pd.Timedelta(days=5)
        df = pd.concat([
            _make_daily_df(),
            pd.DataFrame([_stock_row("000001", "平安银行", date=old_date)]),
        ], ignore_index=True)

        entries = _collect_report_entries(df, pd.Timestamp.today())

        codes = {entry["code"] for entry in entries}
        assert "601728" in codes
        assert "000001" not in codes

    def test_collect_entries_skips_missing_date(self):
        df = pd.DataFrame([
            _stock_row("000001", "平安银行", date=None),
            _stock_row("601728", "中国电信"),
        ])

        entries = _collect_report_entries(df, pd.Timestamp.today())

        assert [entry["code"] for entry in entries] == ["601728"]

    def test_collect_entries_formats_anchor_fundamental_and_tech(self):
        entries = _collect_report_entries(_make_daily_df(), pd.Timestamp.today())

        entry = entries[0]
        assert entry["code"] == "601728"
        assert entry["name"] == "中国电信"
        assert entry["close"] == "5.76"
        assert entry["anchor"] == "ma60"
        assert entry["dev"] == "-2.54%"
        assert entry["div_y"] == "5.20"
        assert entry["pe"] == "12.50"
        assert entry["rsi"] == "52.1"
        assert entry["macd_h"] == "0.045"

    def test_collect_entries_missing_values_to_dash(self):
        df = pd.DataFrame([{
            "stock_code": "510300",
            "stock_name": "沪深300ETF",
            "date": pd.Timestamp.today().normalize(),
            "close": None,
            "ma60": None,
            "dividend_yield": None,
            "pe_ratio": None,
            "pb_ratio": None,
            "roe": None,
        }])

        entry = _collect_report_entries(df, pd.Timestamp.today())[0]

        assert entry["close"] == "-"
        assert entry["anchor"] == "-"
        assert entry["dev"] == "-"
        assert entry["div_y"] == "-"
        assert entry["rsi"] == "-"


class TestFeishuAlertsAndTech:
    """告警码提取与技术数据检测。"""

    def test_extract_alert_codes_standard_and_code_fallback(self):
        alerts = [
            {"stock_code": "601728"},
            {"code": "00883"},
            {"type": "ignored"},
        ]

        assert _extract_alert_codes(alerts) == {"601728", "00883"}

    def test_extract_alert_codes_empty(self):
        assert _extract_alert_codes([]) == set()

    def test_has_technical_data_true(self):
        assert _has_technical_data([{"rsi": "52.1", "macd_h": "-"}]) is True

    def test_has_technical_data_all_dash_or_empty(self):
        assert _has_technical_data([{"rsi": "-", "macd_h": "-", "adx": "-"}]) is False
        assert _has_technical_data([]) is False


class TestFeishuPlainTable:
    """飞书纯文本表格渲染。"""

    def test_plain_table_single_entry(self):
        table = _build_plain_table(
            [{"code": "601728", "name": "中国电信", "close": "5.76"}],
            [("code", "代码"), ("name", "名称"), ("close", "收盘")],
        )

        assert "代码" in table
        assert "中国电信" in table
        assert "601728" in table
        assert "```" not in table
        assert "| --- |" not in table

    def test_plain_table_multiple_cjk_rows(self):
        table = _build_plain_table(
            [
                {"code": "00883", "name": "中国海洋石油", "close": "8.76"},
                {"code": "601728", "name": "中国电信", "close": "5.76"},
            ],
            [("code", "代码"), ("name", "名称"), ("close", "收盘")],
        )

        lines = table.splitlines()
        assert len(lines) == 4
        assert "中国海洋石油" in table
        assert "中国电信" in table

    def test_plain_table_escapes_pipe_and_newline(self):
        table = _build_plain_table(
            [{"code": "A|B", "name": "中国\n电信"}],
            [("code", "代码"), ("name", "名称")],
        )

        assert "A/B" in table
        assert "中国 电信" in table


class TestFeishuFormattingHelpers:
    """格式化、宽度与截断 helper。"""

    def test_fmt_num_none_nan_and_valid(self):
        assert _fmt_num(None) == "-"
        assert _fmt_num(pd.NA) == "-"
        assert _fmt_num(5.2) == "5.20"
        assert _fmt_num(52.123, ".1f") == "52.1"

    def test_short_text_boundary_and_truncation(self):
        assert _short_text("中国电信", 10) == "中国电信"
        assert _short_text("中国海洋石油有限公司", 6) == "中国海洋石…"

    def test_escape_cell(self):
        assert _escape_cell("A|B") == "A/B"
        assert _escape_cell("A\nB") == "A B"

    def test_display_width_ascii_cjk_and_mixed(self):
        assert _display_width("ABC") == 3
        assert _display_width("中国") == 4
        assert _display_width("A股") == 3

    def test_calc_column_widths_min_and_max_clamp(self):
        widths = _calc_column_widths(["代码", "很长很长很长很长的列名"], [["1", "中国海洋石油有限公司"]])

        assert widths[0] == 4
        assert widths[1] == 14

    def test_truncate_display_width(self):
        assert _truncate_display_width("中国海洋石油有限公司", 8) == "中国海…"


def _make_brief_df():
    import pandas as pd
    return pd.DataFrame([
        {
            "stock_code": "601728", "stock_name": "中国电信",
            "date": pd.Timestamp.today().normalize(),
            "open": 5.70, "close": 5.76,
            "ma60": 5.91, "wma20": 5.78, "wma30": 5.65, "wma50": 5.50,
        }
    ])


def _make_daily_df(include_tech=True):
    row = _stock_row("601728", "中国电信")
    if not include_tech:
        for key in ("rsi", "macd_hist", "vol_ratio", "adx", "boll_pct_b"):
            row[key] = None
    return pd.DataFrame([row])


def _make_two_stock_daily_df():
    return pd.DataFrame([
        _stock_row("00883", "中国海洋石油", close=8.76, ma60=9.20),
        _stock_row("601728", "中国电信"),
    ])


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
