"""优化器产出保存测试 — 应在 master 上 FAIL (best_params 类型错误)

修复方向:
  OptimizationReport.best_params: dict[str, float] → dict[str, Any]
"""

import pytest
from pydantic import ValidationError


@pytest.mark.integration
class TestOptimizerSaveWithStringParams:
    """验证优化器产出包含非 float 参数时不崩"""

    def test_realistic_params_from_optimizer(self):
        """模拟优化器真实产出的 params（含 builder 名+股票代码）→ 不应抛异常"""
        from src.analysis.strategy_optimizer import OptimizationReport

        realistic = {
            "buy_1_signal": "rsi_signal",
            "buy_1_threshold": 0.31,
            "buy_2_signal": "bollinger_signal",
            "buy_2_threshold": 0.35,
            "buy_1_action_fraction": 0.20,
            "buy_2_action_fraction": 0.21,
            "sell_1_signal": "none",
            "sell_2_signal": "none",
            "sell_3_signal": "none",
            "_stocks": "600938,601919,600795",
        }

        try:
            OptimizationReport(
                report_id="test_realistic",
                group="a_share",
                timestamp="2026-05-15T00:00:00",
                iterations=150,
                best_params=realistic,
            )
        except ValidationError as e:
            pytest.fail(
                f"P0 BUG 确认: OptimizationReport.best_params 类型过严 "
                f"(声明为 dict[str,float] 但优化器产出含 builder 名称和股票代码等字符串).\n"
                f"服务器上优化器因此连续 16 天零产出.\n"
                f"修复: best_params: dict[str,float] → dict[str,Any].\n"
                f"原始错误: {str(e)[:400]}"
            )
