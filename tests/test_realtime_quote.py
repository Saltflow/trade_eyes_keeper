"""QQ 实时行情 + 数据源可用性测试"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import pandas as pd
from datetime import datetime


class TestQQRealtimeQuote:
    """fetch_realtime_quote 全市场测试"""

    def _mock_qq_response(self, code="601728", price="6.05", name="中国电信",
                          open_p="5.96", high="6.07", low="5.94", time="20260529161419"):
        """模拟腾讯实时行情 API 返回字符串"""
        fields = [""] * 50
        fields[1] = name
        fields[3] = price          # 现价
        fields[4] = "5.97"         # 昨收
        fields[5] = open_p         # 今开
        fields[6] = "178950"       # 成交量
        fields[30] = time          # 时间戳
        fields[32] = "0.08"        # 涨跌幅
        fields[33] = high          # 最高
        fields[34] = low           # 最低
        fields[37] = "107740"      # 成交额
        fields[38] = "0.23"        # 换手率
        return "v_sh{}=".format(code) + "~".join(fields)

    @patch("requests.get")
    def test_returns_dict_with_today_date(self, mock_get):
        """Mock QQ API → 返回 dict，date 字段为今天"""
        mock_get.return_value.text = self._mock_qq_response()
        mock_get.return_value.raise_for_status = lambda: None

        from src.data.web_crawler import StockWebCrawler
        crawler = StockWebCrawler({})
        result = crawler.fetch_realtime_quote("601728")

        assert result is not None
        assert result["date"] == datetime.now().strftime("%Y-%m-%d")
        assert result["close"] == 6.05
        assert result["open"] == 5.96
        assert result["high"] == 6.07
        assert result["low"] == 5.94
        assert result["name"] == "中国电信"

    @patch("requests.get")
    def test_returns_none_on_api_error(self, mock_get):
        """API 异常 → 返回 None"""
        mock_get.side_effect = ConnectionError("no network")

        from src.data.web_crawler import StockWebCrawler
        crawler = StockWebCrawler({})
        result = crawler.fetch_realtime_quote("601728")
        assert result is None

    @patch("requests.get")
    def test_returns_none_on_zero_close(self, mock_get):
        """close=0 → 返回 None"""
        mock_get.return_value.text = self._mock_qq_response(price="0.00")
        mock_get.return_value.raise_for_status = lambda: None

        from src.data.web_crawler import StockWebCrawler
        crawler = StockWebCrawler({})
        result = crawler.fetch_realtime_quote("601728")
        assert result is None

    @patch("requests.get")
    def test_short_response_returns_none(self, mock_get):
        """响应字段不足 → 返回 None"""
        mock_get.return_value.text = "short~response"
        mock_get.return_value.raise_for_status = lambda: None

        from src.data.web_crawler import StockWebCrawler
        crawler = StockWebCrawler({})
        result = crawler.fetch_realtime_quote("601728")
        assert result is None


class TestRealtimeModeInSession:
    """realtime_mode=True 时价格被补充进 Session"""

    @staticmethod
    def _make_fetcher(stocks=None):
        """构造 mock 好的 StockDataFetcher，DataSource + 指标计算均已 stub"""
        from src.core.data_fetcher import StockDataFetcher
        from src.data.data_source import DataSource
        config = {"stocks": stocks or ["601088"], "data_source": {"type": "web_crawler"}}
        fetcher = StockDataFetcher(config)
        fetcher._data_source = MagicMock(spec=DataSource)
        fetcher.technical_indicators.calculate_indicators = lambda df, **kw: df
        return fetcher

    @patch("src.data.web_crawler.StockWebCrawler.fetch_realtime_quote")
    def test_stale_data_gets_price_update(self, mock_qq):
        """历史数据日期为昨天 → realtime_mode 补充当天价格"""
        mock_qq.return_value = {
            "date": "2026-05-29", "close": 6.15, "open": 6.00,
            "high": 6.20, "low": 5.95, "volume": 10000, "amount": 61500,
            "name": "中国电信"
        }
        fetcher = self._make_fetcher(stocks=["601728"])

        # Mock DataSource returns YESTERDAY
        yesterday = pd.Timestamp("2026-05-28")
        fetcher._data_source.fetch_stock_data.return_value = pd.DataFrame([{
            "date": yesterday, "open": 5.96, "close": 5.97,
            "high": 6.07, "low": 5.94, "volume": 10000, "amount": 59700
        }])

        session = MagicMock()
        session._historical = {}
        session_manager = MagicMock()

        fetcher.fetch_to_session(session, session_manager, realtime_mode=True)

        mock_qq.assert_called_once_with("601728")
        assert session_manager.update_stock_from_dataframe.called

    @patch("src.core.data_fetcher.datetime")
    @patch("src.data.web_crawler.StockWebCrawler.fetch_realtime_quote")
    def test_same_day_cache_still_fetches_qq_realtime(self, mock_qq, mock_dt):
        """缓存已有今天（早盘旧价）→ realtime_mode 仍需拉 QQ 实时（修复午盘 stale bug）"""
        mock_dt.now.return_value = datetime(2026, 6, 29, 14, 30, 0)  # 强制 today = 2026-06-29
        mock_qq.return_value = {
            "date": "2026-06-29", "close": 39.98, "open": 39.59,
            "high": 40.30, "low": 38.85, "volume": 41868426,
            "amount": 1673899671, "name": "中国神华"
        }
        fetcher = self._make_fetcher(stocks=["601088"])

        # 关键：DataSource 返回 date=今天 但价格是早盘旧价
        today = pd.Timestamp("2026-06-29")
        fetcher._data_source.fetch_stock_data.return_value = pd.DataFrame([{
            "date": today,
            "open": 39.59,
            "close": 38.98,   # 早上 9:50 的价格，下午应该被 QQ 覆盖
            "high": 39.75, "low": 38.86,
            "volume": 40000000, "amount": 1560000000,
        }])

        session = MagicMock()
        session._historical = {}
        session_manager = MagicMock()

        fetcher.fetch_to_session(session, session_manager, realtime_mode=True)

        mock_qq.assert_called_once_with("601088")

    @patch("src.data.web_crawler.StockWebCrawler.fetch_realtime_quote")
    def test_non_realtime_skips_when_cache_has_today(self, mock_qq):
        """非简报模式 + 缓存已有今天 → 不拉 QQ（避免不必要的网络请求）"""
        fetcher = self._make_fetcher(stocks=["601088"])

        today = pd.Timestamp("2026-06-29")
        fetcher._data_source.fetch_stock_data.return_value = pd.DataFrame([{
            "date": today,
            "open": 39.59, "close": 39.98,
            "high": 40.30, "low": 38.85,
            "volume": 41868426, "amount": 1673899671,
        }])

        session = MagicMock()
        session._historical = {}
        session_manager = MagicMock()

        fetcher.fetch_to_session(session, session_manager, realtime_mode=False)
        mock_qq.assert_not_called()


class TestEastmoneyRemoved:
    """Eastmoney 已从所有降级链中移除"""

    def test_no_eastmoney_in_any_chain(self):
        """验证所有市场的 data_sources 列表中不含 _fetch_from_eastmoney"""
        from src.data.web_crawler import StockWebCrawler
        import inspect

        # 读取 fetch_stock_data 方法的源码来提取 data_sources 列表
        src = inspect.getsource(StockWebCrawler.fetch_stock_data)

        # Eastmoney 方法名不应该出现在任何 data_sources 赋值中
        # 但可以出现在注释中 — 我们只关心 data_sources 列表里的引用
        # data_sources 格式: data_sources = [\n self._fetch_from_xxx,\n ...]
        # 我们检查是否有 data_sources 行包含 _fetch_from_eastmoney

        import re
        # 匹配 data_sources 定义块，直到下一个变量定义或空行
        blocks = re.findall(r'(data_sources = \[.*?\])', src, re.DOTALL)
        for block in blocks:
            assert "_fetch_from_eastmoney" not in block, (
                f"Eastmoney still in fallback chain:\n{block[:200]}"
            )
