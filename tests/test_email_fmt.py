"""测试 _fmt() 函数: None/pd.NA → —，数值 → 格式化，零值 ≠ 缺失。"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from src.notification.email_notifier import _fmt


class TestFmtNoneSafety:
    def test_none_returns_dash(self):
        assert _fmt(None) == "—"

    def test_pd_na_returns_dash(self):
        assert _fmt(pd.NA) == "—"

    def test_nan_returns_dash(self):
        assert _fmt(float("nan")) == "—"

    def test_zero_returns_zero(self):
        """真实零值必须显示为 0.00，不能变成 —"""
        assert _fmt(0) == "0.00"
        assert _fmt(0.0) == "0.00"

    def test_zero_with_unit(self):
        assert _fmt(0, "%") == "0.00%"

    def test_positive_value(self):
        assert _fmt(3.14159) == "3.14"

    def test_negative_value(self):
        assert _fmt(-5.5) == "-5.50"

    def test_with_unit(self):
        assert _fmt(12.5, "%") == "12.50%"

    def test_custom_fmt_spec(self):
        assert _fmt(1.23456, fmt_spec=".3f") == "1.235"

    def test_none_with_unit_still_dash(self):
        assert _fmt(None, "%") == "—"

    def test_pd_na_with_unit_still_dash(self):
        assert _fmt(pd.NA, "%") == "—"
