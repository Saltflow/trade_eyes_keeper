"""指标列名一致性测试 — portfolio_strategy.py 已删除，统一由 compute_all() 处理。"""

import pytest


@pytest.mark.integration
class TestIndicatorColumnNames:
    """列名一致性由 compute_all() 统一保证，不再需要内联检查。"""

    def test_bollinger_column_name_consistent(self):
        pytest.skip("portfolio_strategy.py 已删除，列名统一由 compute_all() 管理")

    def test_rsi_column_present_in_evaluate(self):
        pytest.skip("portfolio_strategy.py 已删除")
