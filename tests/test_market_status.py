"""休市跳过逻辑测试 — 数据指纹比较。"""

import pandas as pd
from datetime import datetime
from pathlib import Path

from src.utils.market_status import is_market_closed, mark_pushed


def _make_df(latest_date: str) -> pd.DataFrame:
    """构造最新日期为 latest_date 的 DataFrame。"""
    return pd.DataFrame([
        {"stock_code": "601728", "date": pd.Timestamp(latest_date), "close": 5.76},
        {"stock_code": "GOOG", "date": pd.Timestamp(latest_date), "close": 370.0},
    ])


def _make_df_str_date(latest_date: str) -> pd.DataFrame:
    """构造 date 列为字符串类型的 DataFrame（模拟 session.get_all_dataframe 的实际行为）。"""
    return pd.DataFrame([
        {"stock_code": "601728", "date": latest_date, "close": 5.76},
        {"stock_code": "GOOG", "date": latest_date, "close": 370.0},
    ])


class TestIsMarketClosed:
    def test_normal_trading_day_pushes(self, tmp_path):
        """交易日：数据日期 != 上次推送 → 不跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-16")
        df = _make_df("2026-06-17")
        assert is_market_closed(df, f) is False

    def test_weekend_skip(self, tmp_path):
        """周末：数据日期 == 上次推送 → 跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-12")  # 上周五
        df = _make_df("2026-06-12")  # 还是周五的数据
        assert is_market_closed(df, f) is True

    def test_holiday_skip(self, tmp_path):
        """节假日：数据日期 == 上次推送 → 跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-02-06")  # 节前最后一个交易日
        df = _make_df("2026-02-06")  # 假期间数据没更新
        assert is_market_closed(df, f) is True

    def test_first_run_no_file(self, tmp_path):
        """首次运行：文件不存在 → 不跳过（推送）。"""
        f = tmp_path / "last_pushed.txt"
        df = _make_df("2026-06-17")
        assert is_market_closed(df, f) is False

    def test_empty_data_skip(self, tmp_path):
        """数据为空 → 跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-16")
        assert is_market_closed(pd.DataFrame(), f) is True

    def test_none_data_skip(self, tmp_path):
        """数据为 None → 跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-16")
        assert is_market_closed(None, f) is True


class TestMarkPushed:
    def test_writes_latest_date(self, tmp_path):
        """推送后写入最新数据日期。"""
        f = tmp_path / "last_pushed.txt"
        df = _make_df("2026-06-17")
        mark_pushed(f, df)
        assert f.read_text().strip() == "2026-06-17"

    def test_overwrites_previous(self, tmp_path):
        """覆盖上次记录。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-16")
        df = _make_df("2026-06-17")
        mark_pushed(f, df)
        assert f.read_text().strip() == "2026-06-17"

    def test_creates_file_if_not_exists(self, tmp_path):
        """文件不存在时创建。"""
        f = tmp_path / "last_pushed.txt"
        df = _make_df("2026-06-17")
        mark_pushed(f, df)
        assert f.exists()


class TestStringDateColumn:
    """date 列为字符串类型时的兼容性测试。

    session.get_all_dataframe() 可能返回 date 列为字符串而非 Timestamp，
    is_market_closed / mark_pushed 必须兼容这种情况。
    """

    def test_str_date_normal_trading_day(self, tmp_path):
        """字符串日期：交易日 → 不跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-16")
        df = _make_df_str_date("2026-06-17")
        assert is_market_closed(df, f) is False

    def test_str_date_weekend_skip(self, tmp_path):
        """字符串日期：休市 → 跳过。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-12")
        df = _make_df_str_date("2026-06-12")
        assert is_market_closed(df, f) is True

    def test_str_date_first_run(self, tmp_path):
        """字符串日期：首次运行 → 不跳过。"""
        f = tmp_path / "last_pushed.txt"
        df = _make_df_str_date("2026-06-17")
        assert is_market_closed(df, f) is False

    def test_str_date_mark_pushed(self, tmp_path):
        """字符串日期：mark_pushed 正确写入。"""
        f = tmp_path / "last_pushed.txt"
        df = _make_df_str_date("2026-06-17")
        mark_pushed(f, df)
        assert f.read_text().strip() == "2026-06-17"

    def test_mixed_date_types(self, tmp_path):
        """混合类型：一行 Timestamp 一行字符串。"""
        f = tmp_path / "last_pushed.txt"
        f.write_text("2026-06-16")
        df = pd.DataFrame([
            {"stock_code": "601728", "date": pd.Timestamp("2026-06-17"), "close": 5.76},
            {"stock_code": "GOOG", "date": "2026-06-15", "close": 370.0},
        ])
        # max() 会取较大的值，Timestamp > str 在 pandas 里可能出问题
        # is_market_closed 应该兼容处理
        assert is_market_closed(df, f) is False
