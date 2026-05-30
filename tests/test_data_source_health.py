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
# 估值缺口 — xfail 跟踪（QQ+Yahoo 双源后应逐步转绿）
# ════════════════════════════════════════════════════════

@pytest.mark.smoke
class TestValuationGaps:
    """非A股估值数据源（QQ+Yahoo 双源后港/美已修通）"""

    def test_valuation_hk_has_data(self):
        """港股 00883 估值非空"""
        crawler = _make_crawler()
        data = crawler.fetch_valuation_data("00883")
        assert data.get("pe_ratio") or data.get("pb_ratio"), \
            "港股估值为空"

    def test_valuation_us_has_data(self):
        """美股 GOOG 估值非空"""
        crawler = _make_crawler()
        data = crawler.fetch_valuation_data("GOOG")
        assert data.get("pe_ratio") or data.get("pb_ratio"), \
            "美股估值为空"

    @pytest.mark.xfail(
        reason="新加坡估值: Yahoo 从国内可能 403, 无 QQ 源",
        strict=False,
    )
    def test_valuation_sg_has_data(self):
        """新加坡 C38U.SI 估值非空"""
        crawler = _make_crawler()
        data = crawler.fetch_valuation_data("C38U.SI")
        assert data.get("pe_ratio") or data.get("pb_ratio"), \
            "新加坡估值为空, Yahoo 可能 403"


# ════════════════════════════════════════════════════════
# 全量负载测试 — 估值源在真实负载下的表现
# ════════════════════════════════════════════════════════

@pytest.mark.smoke
class TestValuationUnderLoad:
    """全量 26 只标的估值请求 — 验证降级链 + 退避 在负载下工作"""

    STOCKS = [
        "601728", "600938", "601985", "601919", "600795", "601398",
        "601088", "512810", "510880", "601818", "601390", "180603",
        "508091", "513910", "588000", "000958", "515180", "508077",
        "GOOG", "VOO", "TQQQ", "UPRO", "00883", "01816", "C38U.SI", "AJBU.SI",
    ]

    def test_full_stock_list_valuation_at_least_half_ok(self):
        """全量请求估值，至少 50% 非空（QQ+Yahoo 降级后覆盖率）"""
        crawler = _make_crawler()
        ok = 0
        failures = []
        for code in self.STOCKS:
            data = crawler.fetch_valuation_data(code)
            if data.get("pe_ratio") or data.get("pb_ratio"):
                ok += 1
            else:
                failures.append(code)
        total = len(self.STOCKS)
        pct = ok / total * 100
        assert ok >= total * 0.5, (
            f"估值覆盖率 {ok}/{total} ({pct:.0f}%) 低于 50%\n"
            f"为空标的: {failures[:8]}{'...' if len(failures) > 8 else ''}\n"
            f"根因: QQ 限流未退避 且 Yahoo 未降级覆盖 A 股"
        )
