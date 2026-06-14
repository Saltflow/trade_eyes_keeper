"""指标列名一致性测试 — 应在 master 上 FAIL（boll_pb vs boll_pct_b 不一致）

修复方向:
  portfolio_strategy.py:609-614 将内联计算的 boll_pb 改为 boll_pct_b,
  与 indicator_library.py 和 signal_scanner.py 统一列名.
"""

import pytest


@pytest.mark.integration
class TestIndicatorColumnNames:
    """验证各处使用的指标列名一致"""

    def test_bollinger_column_name_consistent(self):
        """portfolio_strategy 内联计算和 indicator_library 都用 boll_pct_b"""
        import inspect
        from src.analysis import portfolio_strategy

        # 检查 portfolio_strategy.py 是否硬编码了 boll_pb
        src = inspect.getsource(portfolio_strategy.PortfolioEvaluator.evaluate)

        has_boll_pb = '"boll_pb"' in src or "'boll_pb'" in src
        has_boll_pct_b = '"boll_pct_b"' in src or "'boll_pct_b'" in src

        if has_boll_pb and not has_boll_pct_b:
            pytest.fail(
                "P1 BUG 确认: portfolio_strategy.py 内联计算布林带时使用列名 'boll_pb',\n"
                "但 indicator_library.py 和 signal_scanner.py 使用 'boll_pct_b'.\n"
                "这导致优化器产出的 bollinger_signal 条件 (boll_pct_b < 0.35) 在\n"
                "回测引擎中找不到对应列, 信号永远不触发, 或触发 NameError.\n"
                "修复: 将 portfolio_strategy.py:609 的 boll_pb 改为 boll_pct_b."
            )

    def test_rsi_column_present_in_evaluate(self):
        """portfolio_strategy 在 evaluate 中确保 rsi 列存在"""
        import inspect
        from src.analysis import portfolio_strategy

        src = inspect.getsource(portfolio_strategy.PortfolioEvaluator.evaluate)
        rsi_computed = 'rsi' in src and ('ewm' in src or 'diff' in src)
        rsi_checked = 'rsi" not in' in src or "rsi' not in" in src

        if not rsi_checked and not rsi_computed:
            pytest.fail(
                "P1 BUG 确认: portfolio_strategy 的 evaluate 方法未确保 rsi 列存在.\n"
                "当数据源未提供 rsi 时, 条件表达式 'rsi < 30' 会抛 NameError.\n"
                "修复: 在指标列传递逻辑中增加 rsi 缺失时的兜底计算或报错."
            )
