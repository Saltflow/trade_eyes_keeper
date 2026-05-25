"""
DataSource 缓存策略 + 复权验证 单元测试（TDD）
目标：
  1. 验证 15:55 bypass 逻辑按标的生效
  2. 验证缓存命中路径不会跳过复权检测
  3. 验证前复权修正检测（>5% 差异）触发全量重拉
"""

import sys
import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytest
import pandas as pd
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from src.data.data_source import DataSource


SHANGHAI = pytz.timezone("Asia/Shanghai")


def _make_df(dates, closes, opens=None, highs=None, lows=None):
    """快速构造股价 DataFrame"""
    data = {
        "date": pd.to_datetime(dates),
        "close": closes,
    }
    if opens is None:
        opens = [c * 0.99 for c in closes]
    if highs is None:
        highs = [c * 1.01 for c in closes]
    if lows is None:
        lows = [c * 0.98 for c in closes]
    data["open"] = opens
    data["high"] = highs
    data["low"] = lows
    return pd.DataFrame(data)


class TestShouldBypassCache:
    """测试 _should_bypass_cache 时间判断逻辑"""

    def _build_ds(self, cutoff="15:55"):
        config = {"scheduler": {"cache_bypass_cutoff": cutoff}, "storage": {}}
        return DataSource(config)

    @patch("src.data.data_source.datetime")
    def test_after_cutoff_and_cache_not_today_bypasses(self, mock_dt):
        """过 cutoff，缓存是昨天 → 必须 bypass"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 56, 0)
        ds = self._build_ds("15:55")
        assert ds._should_bypass_cache(
            cache_end_date=datetime(2026, 5, 24).date()
        ) is True

    @patch("src.data.data_source.datetime")
    def test_after_cutoff_and_cache_is_today_no_bypass(self, mock_dt):
        """过 cutoff，缓存是今天 → 不 bypass"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 56, 0)
        ds = self._build_ds("15:55")
        assert ds._should_bypass_cache(
            cache_end_date=datetime(2026, 5, 25).date()
        ) is False

    @patch("src.data.data_source.datetime")
    def test_before_cutoff_and_cache_not_today_no_bypass(self, mock_dt):
        """没过 cutoff，缓存是昨天 → 不 bypass"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 54, 0)
        ds = self._build_ds("15:55")
        assert ds._should_bypass_cache(
            cache_end_date=datetime(2026, 5, 24).date()
        ) is False

    @patch("src.data.data_source.datetime")
    def test_no_cutoff_config_uses_default(self, mock_dt):
        """无 cutoff 配置时默认 15:55"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 56, 0)
        ds = DataSource({"scheduler": {}, "storage": {}})
        assert ds._should_bypass_cache(
            cache_end_date=datetime(2026, 5, 24).date()
        ) is True


class TestCheckForwardAdjustment:
    """测试 _check_forward_adjustment 除权修正检测"""

    def test_detects_when_price_diff_exceeds_5pct(self):
        """重叠日期收盘价差异 >5% → 检测到修正"""
        ds = DataSource({"storage": {}})
        dates = pd.date_range("2026-05-20", periods=5)
        cached = _make_df(dates, [10.0, 10.1, 10.2, 10.3, 10.4])
        # 除权修正后，历史收盘价被下调（6% 差异，严格 >5%）
        new_data = _make_df(dates, [9.4, 9.5, 9.6, 9.7, 9.8])
        assert ds._check_forward_adjustment("000001", cached, new_data) is True

    def test_no_detection_when_price_diff_within_5pct(self):
        """重叠日期收盘价差异 <=5% → 不触发"""
        ds = DataSource({"storage": {}})
        dates = pd.date_range("2026-05-20", periods=5)
        cached = _make_df(dates, [10.0, 10.1, 10.2, 10.3, 10.4])
        # 差异仅 2%
        new_data = _make_df(dates, [10.0, 10.1, 10.2, 10.3, 10.4])
        new_data.loc[0, "close"] = 9.9  # 1% diff
        assert ds._check_forward_adjustment("000001", cached, new_data) is False

    def test_no_detection_when_overlap_less_than_3_days(self):
        """重叠日期 <3 → 无法判断，返回 False"""
        ds = DataSource({"storage": {}})
        dates_cached = pd.date_range("2026-05-20", periods=2)
        dates_new = pd.date_range("2026-05-21", periods=2)
        cached = _make_df(dates_cached, [10.0, 10.1])
        new_data = _make_df(dates_new, [9.0, 9.1])
        assert ds._check_forward_adjustment("000001", cached, new_data) is False

    def test_no_detection_with_empty_data(self):
        """空数据 → False"""
        ds = DataSource({"storage": {}})
        dates = pd.date_range("2026-05-20", periods=5)
        cached = _make_df(dates, [10.0] * 5)
        assert ds._check_forward_adjustment("000001", cached, pd.DataFrame()) is False
        assert ds._check_forward_adjustment("000001", pd.DataFrame(), cached) is False


class TestFetchStockDataCacheBehavior:
    """测试 fetch_stock_data 的缓存命中 / bypass / 复权修正整合路径"""

    def _build_ds(self, cutoff="15:55"):
        config = {
            "scheduler": {"cache_bypass_cutoff": cutoff},
            "storage": {"cache_dir": "./test_cache"},
        }
        return DataSource(config)

    @patch("src.data.data_source.datetime")
    def test_cache_hit_without_bypass_returns_cached(self, mock_dt):
        """缓存命中且 bypass=False → 直接返回缓存子集"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 10, 0, 0)
        ds = self._build_ds("15:55")
        dates = pd.date_range("2026-04-25", periods=31)
        cached = _make_df(dates, [10.0 + i * 0.1 for i in range(31)])

        with patch.object(ds, "_read_cache", return_value=(cached, dates[0], dates[-1])):
            result = ds.fetch_stock_data("000001", days=30)

        # 缓存 31 天（2026-04-25 ~ 2026-05-25），请求 30 天，应返回 31 天
        assert len(result) == 31
        assert result["close"].iloc[-1] == cached["close"].iloc[-1]

    @patch("src.data.data_source.datetime")
    def test_cache_hit_with_bypass_fetches_fresh(self, mock_dt):
        """缓存命中但 bypass=True → 不直接返回，走拉取流程"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 56, 0)
        ds = self._build_ds("15:55")
        # 缓存截止到昨天，范围覆盖请求，触发 bypass
        dates = pd.date_range("2026-04-24", periods=31)
        cached = _make_df(dates, [10.0 + i * 0.1 for i in range(31)])
        fresh = _make_df(
            pd.date_range("2026-05-20", periods=6),
            [11.0] * 6,
        )

        with patch.object(
            ds, "_read_cache", return_value=(cached, dates[0], dates[-1])
        ), patch.object(
            ds, "_fetch_with_verify", return_value=fresh
        ) as mock_fetch, patch.object(
            ds, "_check_forward_adjustment", return_value=False
        ), patch.object(
            ds, "_write_cache", return_value=None
        ):
            result = ds.fetch_stock_data("000001", days=30)
            # 应合并缓存+新数据，而不是直接返回缓存
            assert len(result) >= 6
            mock_fetch.assert_called_once()

    @patch("src.data.data_source.datetime")
    def test_forward_adjustment_triggers_full_refetch(self, mock_dt):
        """检测到前复权修正 → 全量重拉并覆盖缓存"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 10, 0, 0)
        ds = self._build_ds("15:55")
        # 缓存截止到昨天，进入增量拉取路径以触发复权检测
        dates = pd.date_range("2026-04-24", periods=31)
        cached = _make_df(dates, [10.0 + i * 0.1 for i in range(31)])
        # 第一次拉取的数据触发修正检测（重叠日期 2026-05-20~2026-05-24）
        first_fetch = _make_df(
            pd.date_range("2026-05-20", periods=6),
            [5.0] * 6,  # 价格腰斩，触发 >5%
        )
        # 全量重拉的数据
        full_fetch = _make_df(
            pd.date_range("2026-04-24", periods=31),
            [5.0 + i * 0.05 for i in range(31)],
        )

        with patch.object(
            ds, "_read_cache", return_value=(cached, dates[0], dates[-1])
        ), patch.object(
            ds, "_fetch_with_verify", side_effect=[first_fetch, full_fetch]
        ) as mock_fetch, patch.object(
            ds, "_write_cache", return_value=None
        ) as mock_write:
            result = ds.fetch_stock_data("000001", days=30)
            # 应调用两次 _fetch_with_verify：增量 + 全量重拉
            assert mock_fetch.call_count == 2
            # 最终应写入全量数据
            written_df = mock_write.call_args[0][1]
            assert len(written_df) == len(full_fetch)

    @patch("src.data.data_source.datetime")
    def test_new_fetch_fails_fallback_to_cache(self, mock_dt):
        """拉取新数据失败 → 返回缓存子集"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 56, 0)
        ds = self._build_ds("15:55")
        # 缓存截止到昨天，触发 bypass，拉取失败后回退缓存
        dates = pd.date_range("2026-04-24", periods=31)
        cached = _make_df(dates, [10.0 + i * 0.1 for i in range(31)])

        with patch.object(
            ds, "_read_cache", return_value=(cached, dates[0], dates[-1])
        ), patch.object(
            ds, "_fetch_with_verify", return_value=pd.DataFrame()
        ):
            result = ds.fetch_stock_data("000001", days=30)

        assert not result.empty
        assert len(result) == 30
        assert result["close"].iloc[-1] == cached["close"].iloc[-1]

    @patch("src.data.data_source.datetime")
    def test_etf_also_subject_to_bypass(self, mock_dt):
        """ETF（如红利 ETF）也受 bypass 规则约束"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 56, 0)
        ds = self._build_ds("15:55")
        # 缓存截止到昨天，范围覆盖请求，触发 bypass
        dates = pd.date_range("2026-04-24", periods=31)
        cached = _make_df(dates, [3.0 + i * 0.01 for i in range(31)])
        fresh = _make_df(
            pd.date_range("2026-05-20", periods=6),
            [3.5] * 6,
        )

        with patch.object(
            ds, "_read_cache", return_value=(cached, dates[0], dates[-1])
        ), patch.object(
            ds, "_fetch_with_verify", return_value=fresh
        ) as mock_fetch, patch.object(
            ds, "_check_forward_adjustment", return_value=False
        ), patch.object(
            ds, "_write_cache", return_value=None
        ):
            result = ds.fetch_stock_data("510880", days=30)
            mock_fetch.assert_called_once()
            assert len(result) >= 6

    @patch("src.data.data_source.datetime")
    def test_cache_hit_but_no_bypass_before_cutoff(self, mock_dt):
        """15:55 前缓存命中 → 直接返回，不触发 bypass"""
        mock_dt.now.return_value = datetime(2026, 5, 25, 15, 54, 0)
        ds = self._build_ds("15:55")
        dates = pd.date_range("2026-04-25", periods=31)
        cached = _make_df(dates, [10.0 + i * 0.1 for i in range(31)])

        with patch.object(
            ds, "_read_cache", return_value=(cached, dates[0], dates[-1])
        ), patch.object(
            ds, "_fetch_with_verify", return_value=pd.DataFrame()
        ) as mock_fetch:
            result = ds.fetch_stock_data("000001", days=30)

        mock_fetch.assert_not_called()
        assert len(result) == 31
