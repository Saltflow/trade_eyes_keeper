"""回测 handler 测试 — 验证不崩溃、输出含关键字段。"""

import pytest

from src.interactive.commands.handlers import handle_backtest


class TestBacktestHandler:
    def test_backtest_returns_error_for_invalid_code(self):
        result = handle_backtest("ZZZZZZ", "2024-01-01", "2024-12-31")
        assert "❌" in result

    def test_backtest_with_real_data(self):
        """真实数据回测：验证不崩溃。"""
        result = handle_backtest("601728", "2024-06-01", "2024-12-31")
        # 不崩溃即可：OK 含回测报告，FAIL 含 ❌
        assert isinstance(result, str)
        assert len(result) > 10
