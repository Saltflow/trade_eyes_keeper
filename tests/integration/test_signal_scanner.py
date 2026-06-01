"""信号扫描器集成测试 — 应在 master 上 FAIL（指标列缺失导致条件求值崩溃）

修复方向:
  优化器 portfolio_evaluator 内联计算 boll_pb 但与信号扫描器的 boll_pct_b 列名不匹配.
  需统一列名并在信号扫描前确保所有需要的指标列存在.
"""

import pytest
import pandas as pd
from datetime import datetime


@pytest.mark.integration
class TestSignalScannerWithIndicators:
    """验证信号扫描器在有真实指标列的 DataFrame 上不崩"""

    def _make_df_with_indicators(self, close_prices, extra_cols=None):
        """构造带基础列 + 可选指标列的 DataFrame"""
        n = len(close_prices)
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": [p * 0.99 for p in close_prices],
            "high": [p * 1.02 for p in close_prices],
            "low": [p * 0.98 for p in close_prices],
            "close": close_prices,
            "volume": [1000000] * n,
            "ma60": [sum(close_prices[-60:]) / min(60, len(close_prices[-60:]))] * n,
        })
        if extra_cols:
            for col, values in extra_cols.items():
                df[col] = values
        return df

    def test_signal_scan_with_all_indicators(self):
        """构造含 rsi/macd_hist/boll_pct_b/adx/vol_ratio 的 Session → scan() 不崩"""
        try:
            from src.analysis.signal_scanner import SignalScanner
        except ImportError as e:
            pytest.skip(f"SignalScanner 导入失败: {e}")

        prices = [100.0 + i * 0.5 + (i % 20) * 2 for i in range(200)]
        df = self._make_df_with_indicators(prices, extra_cols={
            "rsi": [55.0] * 200,
            "macd_hist": [0.1] * 200,
            "boll_pct_b": [0.5] * 200,  # ← 信号扫描器用这个列名
            "adx": [25.0] * 200,
            "vol_ratio": [1.2] * 200,
            "macd": [0.5] * 200,
            "macd_signal": [0.4] * 200,
        })

        from unittest.mock import Mock
        session = Mock()
        session.get_all_dataframe.return_value = df
        # scan() 需要 _historical 属性
        historical = {"601728": df}
        session._historical = historical

        scanner = SignalScanner()
        try:
            scanner.scan(session, "a_share", top_n=5)
        except Exception as e:
            err = str(e)
            # 常见错误: name 'rsi' is not defined / 列不存在
            if "not defined" in err or "column" in err.lower() or "not found" in err.lower():
                pytest.fail(
                    f"P1 BUG 确认: 信号扫描器在评估优化器产出条件时崩溃.\n"
                    f"错误: {err[:300]}.\n"
                    f"根因: 条件表达式引用的列名在 DataFrame 中缺失, "
                    f"或 portfolio_evaluator 用了 boll_pb 但信号扫描器用 boll_pct_b."
                )
            else:
                pytest.fail(f"信号扫描器崩溃(非预期): {err[:300]}")
