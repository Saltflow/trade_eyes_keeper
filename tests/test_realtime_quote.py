"""QQ 实时行情 + 数据源可用性测试"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


class TestQQRealtimeQuote:
    """fetch_realtime_quote 全市场测试"""

    def _mock_qq_response(
        self,
        code="601728",
        price="6.05",
        name="中国电信",
        open_p="5.96",
        high="6.07",
        low="5.94",
        time="20260529161419",
    ):
        """模拟腾讯实时行情 API 返回字符串"""
        fields = [""] * 50
        fields[1] = name
        fields[3] = price  # 现价
        fields[4] = "5.97"  # 昨收
        fields[5] = open_p  # 今开
        fields[6] = "178950"  # 成交量
        fields[30] = time  # 时间戳
        fields[32] = "0.08"  # 涨跌幅
        fields[33] = high  # 最高
        fields[34] = low  # 最低
        fields[37] = "107740"  # 成交额
        fields[38] = "0.23"  # 换手率
        return f"v_sh{code}=" + "~".join(fields)

    @patch("requests.get")
    def test_returns_source_date_and_previous_close(self, mock_get):
        """抓取旧行情时保留源日期，不把旧报价改成今天。"""
        mock_get.return_value.text = self._mock_qq_response()
        mock_get.return_value.raise_for_status = lambda: None

        from src.data.web_crawler import StockWebCrawler

        crawler = StockWebCrawler({})
        result = crawler.fetch_realtime_quote("601728")

        assert result is not None
        assert result["date"] == "2026-05-29"
        assert result["quote_time"] == "2026-05-29T16:14:19+08:00"
        assert result["prev_close"] == 5.97
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


@pytest.mark.parametrize(
    "returned, accepted", [("BRK.B.N", True), ("BRK.A.N", False), ("", False)]
)
def test_dotted_us_quote_checks_provider_identity(monkeypatch, returned, accepted):
    from types import SimpleNamespace

    from src.data.web_crawler import StockWebCrawler

    fields = [""] * 50
    fields[1], fields[2], fields[3], fields[4], fields[5] = (
        "Berkshire",
        returned,
        "490",
        "490",
        "490",
    )
    fields[30], fields[33], fields[34] = "20261103100000", "490", "490"
    response = MagicMock()
    fetch = MagicMock(return_value=response)
    monkeypatch.setattr("src.data_hub.quotes.fetch_tencent", fetch)
    monkeypatch.setattr(
        "src.data_hub.quotes.require_tencent_quote",
        lambda *a, **k: SimpleNamespace(fields=fields),
    )
    result = StockWebCrawler({}).fetch_realtime_quote("BRK.B")
    assert (result is not None) is accepted
    assert fetch.call_args.args[1].endswith("q=usBRK.B")
    if accepted:
        assert result["stock_code"] == "BRK.B" and result["close"] == 490


@pytest.mark.parametrize(
    "rows,expected",
    [
        (
            [{"date": "2026-06-26", "close": 9.7}, {"date": "2026-06-29", "close": 10}],
            9.7,
        ),
        (
            [
                {"date": "2026-06-26", "close": 9.7},
                {"date": "2026-06-29", "close": 10, "prev_close": 9.8},
            ],
            9.8,
        ),
        ([{"date": "2026-06-29", "close": 10}], None),
        (
            [{"date": "2026-06-29", "close": 9.9}, {"date": "2026-06-29", "close": 10}],
            None,
        ),
        (
            [
                {"date": "2026-06-26", "close": float("nan")},
                {"date": "2026-06-29", "close": 10},
            ],
            None,
        ),
    ],
)
def test_previous_close_uses_prior_trading_bar_without_guessing(rows, expected):
    from src.core.data_fetcher import StockDataFetcher

    assert StockDataFetcher._previous_close(pd.DataFrame(rows)) == expected


def test_daily_fetch_keeps_previous_close_from_existing_history(monkeypatch):
    from src.core.data_fetcher import StockDataFetcher
    from src.data.data_source import DataSource

    fetcher = StockDataFetcher({"stocks": ["601088"]})
    fetcher._data_source = MagicMock(spec=DataSource)
    fetcher.technical_indicators.calculate_indicators = lambda frame, **kwargs: frame
    fetcher._data_source.fetch_stock_data.return_value = pd.DataFrame(
        [
            {"date": pd.Timestamp("2026-06-26"), "close": 9.7},
            {"date": pd.Timestamp("2026-06-29"), "open": 9.8, "close": 10},
        ]
    )
    monkeypatch.setattr(fetcher, "_add_extended_indicators", lambda frame: frame)
    monkeypatch.setattr(
        fetcher,
        "_fetch_fundamental_data",
        lambda code: dict.fromkeys(
            ["dividend_per_share", "pe_ratio", "pb_ratio", "roe"]
        ),
    )
    monkeypatch.setattr(fetcher, "_save_to_csv", lambda *args: None)
    quote = MagicMock(
        side_effect=AssertionError("history acquisition must not fetch report quotes")
    )
    monkeypatch.setattr(fetcher.web_crawler, "fetch_realtime_quote", quote)
    manager, session = MagicMock(), MagicMock()
    session._historical = {}
    fetcher.fetch_to_session(session, manager)
    frame = manager.update_stock_from_dataframe.call_args.args[2]
    assert frame.iloc[-1]["prev_close"] == 9.7
    assert frame.iloc[-1]["open"] == 9.8
    assert frame.iloc[-1]["close"] == 10
    quote.assert_not_called()


class TestEastmoneyRemoved:
    """Eastmoney 已从所有降级链中移除"""

    def test_no_eastmoney_in_any_chain(self):
        """验证所有市场的 data_sources 列表中不含 _fetch_from_eastmoney"""
        import inspect

        from src.data.web_crawler import StockWebCrawler

        # 读取 fetch_stock_data 方法的源码来提取 data_sources 列表
        src = inspect.getsource(StockWebCrawler.fetch_stock_data)

        # Eastmoney 方法名不应该出现在任何 data_sources 赋值中
        # 但可以出现在注释中 — 我们只关心 data_sources 列表里的引用
        # data_sources 格式: data_sources = [\n self._fetch_from_xxx,\n ...]
        # 我们检查是否有 data_sources 行包含 _fetch_from_eastmoney

        import re

        # 匹配 data_sources 定义块，直到下一个变量定义或空行
        blocks = re.findall(r"(data_sources = \[.*?\])", src, re.DOTALL)
        for block in blocks:
            assert "_fetch_from_eastmoney" not in block, (
                f"Eastmoney still in fallback chain:\n{block[:200]}"
            )


@pytest.fixture(autouse=True)
def isolated_source_authority(tmp_path, monkeypatch):
    """Each simulated provider gets durable shared state isolated from other tests."""
    monkeypatch.setenv("TRADE_EYES_AUTHORITY_ROOT", str(tmp_path / "authority"))
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda *args, **kwargs: pytest.fail("test attempted live HTTP"),
    )
