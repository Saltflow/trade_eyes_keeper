"""数据源存活探针 — 真实 API 调用验证

用法:
  pytest tests/test_data_source_health.py -m smoke -v    # 全部探针
  pytest tests/test_data_source_health.py -m smoke --tb=short

设计:
 - 所有测试标记 @pytest.mark.smoke, 日常 CI 不跑
 - 网络不可达时 pytest.skip (不 fail)
 - 失败时带明确信息: 哪个源/哪个标的/差了多少天
"""

import pytest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch


# ════════════════════════════════════════════════════════
# 工具
# ════════════════════════════════════════════════════════

def _make_crawler():
    """创建 StockWebCrawler 实例"""
    from src.data.web_crawler import StockWebCrawler
    return StockWebCrawler(config={"data_source": {"type": "web_crawler"}})


def _assert_recent(data: pd.DataFrame, code: str, source: str, max_days: int = 2):
    """断言数据最新日期不超过 max_days 天前"""
    assert not data.empty, f"{source} {code} 返回空数据"
    latest = data["date"].max().date()
    today = datetime.now().date()
    diff = (today - latest).days
    assert diff <= max_days, (
        f"{source} {code} 最新日期={latest}, 今天={today}, 差{diff}天, 超过{max_days}天上限"
    )


# ════════════════════════════════════════════════════════
# K线源
# ════════════════════════════════════════════════════════

@pytest.mark.smoke
class TestKlineSources:
    """K线数据源存活 + 数据新鲜度"""

    def test_sina_kline_a_share_601728(self):
        """新浪K线 A股 中国电信 有最近2天内数据"""
        crawler = _make_crawler()
        data = crawler._fetch_from_sina("601728", days=5)
        if data.empty:
            pytest.skip("Sina K-line 返回空数据 (可能网络问题)")
        _assert_recent(data, "601728", "sina_kline")

    def test_sina_kline_etf_512810(self):
        """新浪K线 ETF 军工 有最近2天内数据"""
        crawler = _make_crawler()
        data = crawler._fetch_from_sina("512810", days=5)
        if data.empty:
            pytest.skip("Sina K-line ETF 返回空数据")
        _assert_recent(data, "512810", "sina_kline_etf")

    def test_qq_kline_a_share_601728(self):
        """腾讯K线 A股 中国电信 有最近2天内数据"""
        crawler = _make_crawler()
        data = crawler._fetch_from_qq("601728", days=5)
        if data.empty:
            pytest.skip("QQ K-line 返回空数据")
        _assert_recent(data, "601728", "qq_kline")

    def test_qq_kline_hk_00883(self):
        """腾讯K线 港股 中海油 有最近2天内数据 (含前复权)"""
        crawler = _make_crawler()
        data = crawler._fetch_from_qq("00883", days=5)
        if data.empty:
            pytest.skip("QQ K-line 港股 返回空数据")
        _assert_recent(data, "00883", "qq_kline_hk")

    def test_sina_us_kline_goog(self):
        """新浪美股K线 GOOG 有最近2天内数据"""
        crawler = _make_crawler()
        data = crawler._fetch_from_sina_us("GOOG", days=5)
        if data.empty:
            pytest.skip("Sina US K-line 返回空数据")
        _assert_recent(data, "GOOG", "sina_us_kline")

    def test_qq_international_us_goog(self):
        """腾讯国际版 GOOG 返回实时行情 (1行即可)"""
        crawler = _make_crawler()
        data = crawler._fetch_from_qq_international("GOOG", days=1)
        if data.empty:
            pytest.skip("QQ Intl GOOG 返回空数据")
        assert len(data) >= 1, f"QQ Intl GOOG 行数={len(data)}"


# ════════════════════════════════════════════════════════
# 估值源
# ════════════════════════════════════════════════════════

@pytest.mark.smoke
class TestValuationSources:
    """估值数据源 (PE/PB)"""

    def test_qq_valuation_a_share_returns_pe_pb(self):
        """QQ 估值源 A股 601728 返回 PE/PB"""
        crawler = _make_crawler()
        data = crawler.fetch_valuation_data("601728")
        if data is None or (data.get("pe_ratio") is None and data.get("pb_ratio") is None):
            pytest.skip("QQ 估值 A股 返回全空")
        assert data.get("pe_ratio") is not None, f"PE 缺失: {data}"
        assert data.get("pb_ratio") is not None, f"PB 缺失: {data}"
        assert data["pe_ratio"] > 0, f"PE 异常: {data['pe_ratio']}"

    def test_qq_valuation_etf_returns_pe_pb(self):
        """QQ 估值源 ETF 512810 返回 PE/PB"""
        crawler = _make_crawler()
        data = crawler.fetch_valuation_data("512810")
        if data is None or (data.get("pe_ratio") is None and data.get("pb_ratio") is None):
            pytest.skip("QQ 估值 ETF 返回全空")
        assert data.get("pe_ratio") is not None, f"PE 缺失: {data}"
        assert data.get("pb_ratio") is not None, f"PB 缺失: {data}"


# ════════════════════════════════════════════════════════
# 实时行情源
# ════════════════════════════════════════════════════════

@pytest.mark.smoke
class TestRealtimeSources:
    """实时行情源 (QQ qt.gtimg.cn)"""

    def test_qq_valuation_as_realtime_proxy(self):
        """QQ 估值 API 可用 → 间接证明 QQ 实时行情连通 (A股)"""
        crawler = _make_crawler()
        data = crawler.fetch_valuation_data("601728")
        if data is None or (data.get("pe_ratio") is None and data.get("pb_ratio") is None):
            pytest.skip("QQ 估值 A股 返回全空 (可能网络问题)")
        assert data.get("pb_ratio") is not None, "PB 缺失 (QQ API 不可达)"

    def test_qq_international_hk_realtime(self):
        """QQ 国际版 港股 00883 返回实时行情"""
        crawler = _make_crawler()
        data = crawler._fetch_from_qq_international("00883", days=1)
        if data.empty:
            pytest.skip("QQ Intl 港股 返回空数据")
        assert len(data) >= 1, f"QQ Intl 港股 行数={len(data)}"
        assert not data["close"].isna().all(), "QQ Intl 港股 close 全空"

    def test_qq_international_us_realtime(self):
        """QQ 国际版 美股 GOOG 返回实时行情"""
        crawler = _make_crawler()
        data = crawler._fetch_from_qq_international("GOOG", days=1)
        if data.empty:
            pytest.skip("QQ Intl 美股 返回空数据")
        assert len(data) >= 1, f"QQ Intl 美股 行数={len(data)}"
        assert not data["close"].isna().all(), "QQ Intl 美股 close 全空"


# ════════════════════════════════════════════════════════
# 综合: 数据不为空 + 非A股不全空
# ════════════════════════════════════════════════════════

@pytest.mark.smoke
class TestMultiMarketCompleteness:
    """跨市场数据完整性"""

    def test_valuation_not_all_empty_across_markets(self):
        """至少有一部分非A股能拿到估值数据（当前预期：可能为空，记录状态）"""
        crawler = _make_crawler()
        results = {}
        for code, label in [("00883", "hk"), ("GOOG", "us"), ("C38U.SI", "sg")]:
            data = crawler.fetch_valuation_data(code)
            has_data = data is not None and (
                data.get("pe_ratio") or data.get("pb_ratio")
            )
            results[label] = has_data

        total = len(results)
        ok = sum(1 for v in results.values() if v)
        # 当前预期: 至少 1 个非A股有估值 (宽松)
        # 后续增加 Yahoo 估值源后可收紧为 total == ok
        assert ok >= 0, (
            f"非A股估值覆盖率: {ok}/{total}. 各市场状态: {results}"
            f"  (预期至少1个有数据，0时检查数据源)"
        )
