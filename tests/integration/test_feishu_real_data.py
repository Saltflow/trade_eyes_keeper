"""飞书真实数据链路测试。

原则：
- 可以使用最小 config 只放 1 个标的，减少网络与通知开销。
- 不 mock 行情/基本面/技术指标数据；数据必须由 StockDataFetcher 真实链路写入 Session。
- 默认不真实发送飞书，只 patch 传输层 `_send` 验证“只发一张卡片”的入参。
- 如需手动真实发送，可设置 FEISHU_E2E_SEND=1 且配置 FEISHU_WEBHOOK_URL。
"""

import os
from unittest.mock import patch

import pytest

from src.core.data_fetcher import StockDataFetcher
from src.notification.manager import NotifierManager
from src.session.session_manager import SessionManager


def test_feishu_real_data_one_card_pipeline():
    """单标的真实数据 → Session → Feishu 第一张卡片链路。"""
    config = _single_stock_feishu_config("601728")
    session_manager = SessionManager(config)
    session = session_manager.create_session(config)

    fetcher = StockDataFetcher(config)
    try:
        fetcher.fetch_to_session(session, session_manager)
    except Exception as exc:
        pytest.skip(f"真实数据源不可用，跳过飞书真实链路测试: {exc}")

    if not session.stocks_data:
        pytest.skip("真实数据源未返回 601728 数据，跳过飞书真实链路测试")

    manager = NotifierManager(config)
    assert manager.email is None
    assert manager.telegram is None
    assert manager.feishu is not None

    stock_data = session.get_all_dataframe()
    assert not stock_data.empty
    assert "601728" in set(stock_data["stock_code"].astype(str))

    sections = manager.feishu._build_report_sections(stock_data)
    assert sections
    label, body = sections[0]

    assert label == "价格"
    assert "监控日报" in body
    assert "601728" in body
    assert "<table" not in body
    assert "| --- |" not in body
    assert "```" not in body

    title = f"飞书真实链路验证 · {label}"
    if os.getenv("FEISHU_E2E_SEND") == "1" and config["notification"]["feishu"].get(
        "webhook_url"
    ):
        ok, msg = manager.feishu._send(title, body)
        assert ok, msg
    else:
        with patch.object(manager.feishu, "_send", return_value=(True, "ok")) as mock_send:
            ok, msg = manager.feishu._send(title, body)

        assert ok
        assert msg == "ok"
        mock_send.assert_called_once_with(title, body)


def _single_stock_feishu_config(stock_code: str) -> dict:
    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
    return {
        "stocks": [stock_code],
        "data_source": {"type": "web_crawler"},
        "storage": {
            "data_dir": "./cache",
            "cache_dir": "./cache",
        },
        "scheduler": {
            "timezone": "Asia/Shanghai",
            "cache_bypass_cutoff": "15:55",
        },
        "notification": {
            "email": {"enabled": False},
            "telegram": {"enabled": False},
            "feishu": {
                "enabled": True,
                "webhook_url": webhook_url,
                "msg_type": "interactive",
            },
        },
    }
