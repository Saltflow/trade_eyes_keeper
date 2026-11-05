"""Removed empty adapters must not replace the active source routes."""

from datetime import datetime
from unittest.mock import Mock

import pytest
from zoneinfo import ZoneInfo

from src.data.announcement_fetcher import AnnouncementFetcher
from src.data.web_crawler import StockWebCrawler


@pytest.mark.parametrize("code,exchange", [("601088", "sse"), ("000333", "szse")])
def test_public_announcement_route_still_fetches_and_parses_sina(
    monkeypatch, code, exchange
):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    response = Mock(status_code=200)
    response.text = (
        f'<div class="datelist"><ul>{today}'
        '<a href="/unit/report.phtml">董事会决议公告</a></ul></div>'
    )
    get = Mock(return_value=response)
    monkeypatch.setattr("src.data.announcement_fetcher.requests.get", get)
    (item,) = AnnouncementFetcher({}).fetch_announcements([code], days=7)[code]
    assert (item["stock_code"], item["exchange"], item["date"]) == (
        code,
        exchange,
        today,
    )
    assert item["url"] == "http://vip.stock.finance.sina.com.cn/unit/report.phtml"
    get.assert_called_once()
    assert f"stockid/{code}.phtml" in get.call_args.args[0]
    prefix = "sh" if exchange == "sse" else "sz"
    assert f"/{prefix}{code}/" in get.call_args.kwargs["headers"]["Referer"]


def test_empty_retired_source_methods_are_no_longer_exposed():
    for name in (
        "_fetch_from_sse",
        "_parse_sse_api_response",
        "_parse_sse_html",
        "_fetch_from_szse",
        "_parse_szse_api_response",
        "_parse_szse_html",
    ):
        assert not hasattr(AnnouncementFetcher, name)
    assert not hasattr(StockWebCrawler, "_fetch_realtime_from_qq")
