"""测试 _fmt() 函数: None/pd.NA → —，数值 → 格式化，零值 ≠ 缺失。"""
import pytest
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from notification.email_notifier import _fmt


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


class TestNoOrZeroInRenderPaths:
    """确保 _fmt() 替代了所有 'or 0' 模式。"""

    def test_no_vals_get_or_zero_in_table_cells(self):
        """HTML 和 LaTeX 表格单元格不再使用 vals.get(ind, 0) or 0"""
        path = Path(__file__).parent.parent / "src" / "notification" / "email_notifier.py"
        src = path.read_text(encoding="utf-8")
        # 关键模式: 在渲染路径中的 or 0
        count = src.count("vals.get(ind, 0) or 0")
        assert count == 0, (
            f"email_notifier.py 中仍有 {count} 处 vals.get(ind, 0) or 0 模式，"
            f"请替换为 vals.get(ind) + _fmt() None 检测"
        )

    def test_no_bt_get_or_zero_in_kpi(self):
        """KPI 值不再使用 bt.get('key', 0) or 0"""
        path = Path(__file__).parent.parent / "src" / "notification" / "email_notifier.py"
        src = path.read_text(encoding="utf-8")
        count = src.count('get("total_return", 0) or 0')
        assert count == 0, (
            f"still has {count} bt.get(total_return, 0) or 0 patterns"
        )
