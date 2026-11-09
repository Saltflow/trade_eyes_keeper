"""飞书真实数据简报链路测试。

原则：
- 可以使用最小 config 只放 1 个标的，减少网络与通知开销。
- 不 mock 行情/基本面/技术指标数据；数据必须由 StockDataFetcher 真实链路写入 Session。
- 默认不真实发送飞书，只 patch `_post_card`；通过公共简报入口验证原生卡片与逐渠道回执。
- 如需手动真实发送，可设置 FEISHU_E2E_SEND=1 且配置 FEISHU_WEBHOOK_URL。
"""

import json
import os
from unittest.mock import patch

import pytest

from src.core.data_fetcher import StockDataFetcher
from src.notification.manager import NotifierManager
from src.session.session_manager import SessionManager

# This module acquires live quotes and can explicitly send a real Feishu card.
# Keep it out of offline runs even when a source happens to have cached data.
pytestmark = pytest.mark.e2e


def test_feishu_real_data_brief_card_pipeline():
    """单标的真实数据 → Session → 公共简报入口 → 一张原生卡片及回执。"""
    config = _single_stock_feishu_config("601728")
    session_manager = SessionManager(config)
    session = session_manager.create_session(config)

    fetcher = StockDataFetcher(config)
    fetcher.fetch_to_session(session, session_manager)

    if not session.stocks_data:
        pytest.skip("真实数据源未返回 601728 数据，跳过飞书真实链路测试")

    manager = NotifierManager(config)
    assert manager.email is None
    assert manager.telegram is None
    assert manager.feishu is not None

    stock_data = session.get_all_dataframe()
    assert not stock_data.empty
    assert "601728" in set(stock_data["stock_code"].astype(str))

    title = "飞书真实链路验证 · 简报"
    real_send = os.getenv("FEISHU_E2E_SEND") == "1" and config["notification"][
        "feishu"
    ].get("webhook_url")
    transport = (
        {"wraps": manager.feishu._post_card}
        if real_send
        else {"return_value": {"status": "sent", "reason": "offline_transport_ack"}}
    )
    with patch.object(manager.feishu, "_post_card", **transport) as post:
        result = manager.send_brief_report(session, {"label": title})

    assert result["status"] == "succeeded", result
    assert set(result["deliveries"]) == {"feishu"}
    receipt = result["deliveries"]["feishu"]
    assert receipt["status"] == "sent"
    assert len(receipt["pieces"]) == 1
    assert receipt["pieces"][0]["status"] == "sent"
    assert receipt["pieces"][0]["label"] == "brief"
    assert receipt["pieces"][0]["ordinal"] == 0
    post.assert_called_once()

    payload = post.call_args.args[0]
    assert payload["msg_type"] == "interactive"
    card = payload["card"]
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == title
    elements = card["body"]["elements"]
    tables = [element for element in elements if element["tag"] == "table"]
    assert len(tables) == 1
    assert len(tables[0]["rows"]) == len(stock_data)
    assert "601728" in json.dumps(tables[0], ensure_ascii=False)
    body = "\n".join(
        element["content"] for element in elements if element["tag"] == "markdown"
    )
    assert "<table" not in body
    assert "| --- |" not in body
    assert "```" not in body


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
