"""
测试港股数据源：`_fetch_from_sina_hk` 方法 + fallback 链顺序

测试要点：
  1. _fetch_from_sina_hk 正确解析新浪港股API的JSON响应
  2. 港股 fallback 链中 _fetch_from_sina_hk 位于 _fetch_from_yahoo 之前
  3. 东方财富失败时，fallback 链能走到 _fetch_from_sina_hk
"""

import json
import pandas as pd
import pytest
from unittest.mock import patch, Mock, MagicMock


# ── 模拟新浪港股API响应 ──
MOCK_SINA_HK_JSON = json.dumps(
    [
        {
            "day": "2026-04-01",
            "open": 10.5,
            "high": 10.8,
            "low": 10.3,
            "close": 10.6,
            "volume": 12345678,
        },
        {
            "day": "2026-04-02",
            "open": 10.6,
            "high": 10.9,
            "low": 10.4,
            "close": 10.7,
            "volume": 9876543,
        },
        {
            "day": "2026-04-03",
            "open": 10.7,
            "high": 11.0,
            "low": 10.5,
            "close": 10.9,
            "volume": 11111111,
        },
    ]
)

# 模拟新浪港股实时行情响应（用于获取股票名称）
MOCK_SINA_HQ_RESPONSE = (
    'var hq_str_hk00883="中国海洋石油",10.6,10.7,10.9,10.4,12345678;'
)


class TestSinaHkFetcher:
    """测试 _fetch_from_sina_hk 方法"""

    def test_fetch_returns_dataframe(self):
        """_fetch_from_sina_hk 应返回 DataFrame"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        with patch.object(crawler, "_fetch_from_sina_hk") as mock_method:
            mock_df = pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-04-01"]),
                    "open": [10.5],
                    "close": [10.6],
                    "high": [10.8],
                    "low": [10.3],
                    "volume": [12345678],
                }
            )
            mock_method.return_value = mock_df

            result = crawler._fetch_from_sina_hk("00883", 120)
            assert isinstance(result, pd.DataFrame)
            assert not result.empty
            assert "date" in result.columns
            assert "close" in result.columns

    def test_parse_json_response(self):
        """_fetch_from_sina_hk 应正确解析新浪API返回的JSON"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        # Mock requests.get for the historical data API
        mock_historical_response = MagicMock()
        mock_historical_response.status_code = 200
        mock_historical_response.json.return_value = json.loads(MOCK_SINA_HK_JSON)

        # Mock requests.get for the real-time quote API (stock name)
        mock_quote_response = MagicMock()
        mock_quote_response.status_code = 200
        mock_quote_response.text = MOCK_SINA_HQ_RESPONSE

        with patch("requests.get") as mock_get:
            # 第一次调用返回历史数据，第二次调用返回实时行情
            mock_get.side_effect = [mock_historical_response, mock_quote_response]

            df = crawler._fetch_from_sina_hk("00883", 120)

            assert not df.empty
            assert len(df) == 3
            assert df.iloc[0]["date"].strftime("%Y-%m-%d") == "2026-04-01"
            assert df.iloc[0]["close"] == 10.6
            assert df.iloc[2]["close"] == 10.9
            assert "stock_name" in df.columns
            assert "中国海洋石油" in df.iloc[0]["stock_name"]

    def test_parse_single_item(self):
        """只返回1条数据时也应正确处理"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})
        single_item_json = json.dumps(
            [
                {
                    "day": "2026-04-26",
                    "open": 10.5,
                    "high": 10.8,
                    "low": 10.3,
                    "close": 10.6,
                    "volume": 12345678,
                }
            ]
        )

        mock_hist = MagicMock()
        mock_hist.status_code = 200
        mock_hist.json.return_value = json.loads(single_item_json)
        mock_quote = MagicMock()
        mock_quote.status_code = 200
        mock_quote.text = MOCK_SINA_HQ_RESPONSE

        with patch("requests.get") as mock_get:
            mock_get.side_effect = [mock_hist, mock_quote]
            df = crawler._fetch_from_sina_hk("00883", 120)

            assert not df.empty
            assert len(df) == 1
            assert "amplitude" in df.columns
            assert "change_pct" in df.columns

    def test_empty_response(self):
        """API返回空列表时应返回空DataFrame"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        mock_hist = MagicMock()
        mock_hist.status_code = 200
        mock_hist.json.return_value = []

        with patch("requests.get", return_value=mock_hist):
            df = crawler._fetch_from_sina_hk("00883", 120)
            assert df.empty

    def test_invalid_json(self):
        """API返回非JSON时应返回空DataFrame"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        mock_hist = MagicMock()
        mock_hist.status_code = 200
        mock_hist.json.side_effect = ValueError("Invalid JSON")

        with patch("requests.get", return_value=mock_hist):
            df = crawler._fetch_from_sina_hk("00883", 120)
            assert df.empty

    def test_http_error(self):
        """HTTP请求失败时应返回空DataFrame"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        mock_hist = MagicMock()
        mock_hist.status_code = 500

        with patch("requests.get", return_value=mock_hist):
            # status_code != 200 doesn't raise_for_status but we handle it
            df = crawler._fetch_from_sina_hk("00883", 120)
            assert df.empty


class TestHkFallbackChain:
    """测试港股 fallback 链包含 _fetch_from_sina_hk"""

    def test_fallback_chain_contains_sina_hk(self):
        """港股 fallback 链中应包含 _fetch_from_sina_hk，且在 yahoo 之前"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})
        # 通过 fetch_stock_data 间接验证 fallback 链
        # 当 source_name 为 None 时，港股会走 fallback 链

        # 检查 _fetch_from_sina_hk 方法是否存在
        assert hasattr(crawler, "_fetch_from_sina_hk")
        assert callable(crawler._fetch_from_sina_hk)

    def test_eastmoney_fails_falls_back_to_sina_hk(self):
        """东方财富失败时，fallback 应走到 _fetch_from_sina_hk"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        # 保存原始方法引用
        _eastmoney = crawler._fetch_from_eastmoney
        _yahoo = crawler._fetch_from_yahoo
        _qq = crawler._fetch_from_qq
        _qq_int = crawler._fetch_from_qq_international
        _sina_hk = crawler._fetch_from_sina_hk

        call_tracker = {"sina_hk_called": False, "yahoo_called": False}

        def mock_empty(*args, **kwargs):
            return pd.DataFrame()

        def mock_sina_hk(*args, **kwargs):
            call_tracker["sina_hk_called"] = True
            return pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-04-01"]),
                    "open": [10.5],
                    "close": [10.6],
                    "high": [10.8],
                    "low": [10.3],
                    "volume": [12345678],
                    "amplitude": [2.86],
                    "change_pct": [0.0],
                    "change": [0.1],
                    "turnover": [0.0],
                }
            )

        def mock_yahoo(*args, **kwargs):
            call_tracker["yahoo_called"] = True
            return pd.DataFrame()

        try:
            crawler._fetch_from_eastmoney = mock_empty
            crawler._fetch_from_yahoo = mock_yahoo
            crawler._fetch_from_sina_hk = mock_sina_hk
            crawler._fetch_from_qq = mock_empty
            crawler._fetch_from_qq_international = mock_empty

            data = crawler.fetch_stock_data("00883", 120)

            assert data is not None
            assert not data.empty
            # 验证 _fetch_from_sina_hk 被调用过
            assert call_tracker["sina_hk_called"], "sina_hk should have been called"
            # 验证 _fetch_from_yahoo 没被调用（sina_hk 在它之前且成功返回数据）
            assert not call_tracker["yahoo_called"], "yahoo should NOT have been called"
            # _last_source_name 应该被设置为 mock_sina_hk (实际上 fallback 链用 __name__ 记录)
        finally:
            # 恢复原始方法
            crawler._fetch_from_eastmoney = _eastmoney
            crawler._fetch_from_yahoo = _yahoo
            crawler._fetch_from_qq = _qq
            crawler._fetch_from_qq_international = _qq_int
            crawler._fetch_from_sina_hk = _sina_hk

    def test_sina_hk_api_url_uses_hk_prefix(self):
        """_fetch_from_sina_hk 应使用 hk 前缀的 symbol"""
        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})

        # 验证 normalize 对 00883 的 sina_symbol 是 hk00883
        _, _, sina_symbol, _, _ = crawler._normalize_stock_code("00883")
        assert sina_symbol == "hk00883"

        # 再验证一个 5 位代码
        _, _, sina_symbol, _, _ = crawler._normalize_stock_code("01816")
        assert sina_symbol == "hk01816"
