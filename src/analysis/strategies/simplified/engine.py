"""简化限额搜参策略 — 固定总资金 100k + 最低持仓 30 天，每条规则独立限额。"""

from __future__ import annotations

import numpy as np

from ...search_interface import SearchStrategy, ParamDim, ParamSpace, Params

BUY_LIMIT_LEVELS = [5000.0, 10000.0, 20000.0, 30000.0, 50000.0]
SELL_LIMIT_LEVELS = [5000.0, 10000.0, 20000.0, 30000.0, 50000.0]
THRESHOLD_LEVELS_SIMP = 10
NUM_BUY_RULES = 5
NUM_SELL_RULES = 3
BUY_BUILDERS_SIMP = [
    "deviation_cross", "rsi_signal", "bollinger_signal",
    "volume_spike", "deviation_absolute",
]
SELL_BUILDERS_SIMP = [
    "sell_deviation_cross", "sell_rsi_signal", "sell_absolute",
]


class SimplifiedSearchStrategy(SearchStrategy):
    """简化限额搜参策略 — 2 个固定参数 + 每条规则独立买卖限额。"""

    def __init__(self):
        dims = []
        for i in range(NUM_BUY_RULES):
            dims.append(ParamDim(
                f"buy_{i+1}_name", len(BUY_BUILDERS_SIMP), 0, 1
            ))
            dims.append(ParamDim(
                f"buy_{i+1}_threshold", THRESHOLD_LEVELS_SIMP, 0, 1
            ))
            dims.append(ParamDim(
                f"buy_{i+1}_limit", len(BUY_LIMIT_LEVELS), 0, 1
            ))
        for i in range(NUM_SELL_RULES):
            dims.append(ParamDim(
                f"sell_{i+1}_name", len(SELL_BUILDERS_SIMP), 0, 1
            ))
            dims.append(ParamDim(
                f"sell_{i+1}_threshold", THRESHOLD_LEVELS_SIMP, 0, 1
            ))
            dims.append(ParamDim(
                f"sell_{i+1}_limit", len(SELL_LIMIT_LEVELS), 0, 1
            ))
        self._space = ParamSpace(dims)

    @property
    def name(self) -> str:
        return "simplified"

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        """简化策略返回空评分——实际由 FastEvaluator.evaluate() 直接使用 limits 参数。"""
        T, N = indicator_matrix.shape[:2]
        return np.zeros((T, N, 2), dtype=np.float32)

    def scan_today(self, params, today: dict, history=None) -> list[dict]:
        from .scanner import scan_simplified_today
        return scan_simplified_today(params, today, history)

    def to_human_readable(self, params: Params) -> str:
        vals = params.values
        lines = ["简化限额策略 (simplified)"]
        lines.append("  total_capital=100000, min_holding_days=30")
        for i in range(NUM_BUY_RULES):
            n_idx = vals.get(f"buy_{i+1}_name", 0) % len(BUY_BUILDERS_SIMP)
            limit = BUY_LIMIT_LEVELS[
                vals.get(f"buy_{i+1}_limit", 1) % len(BUY_LIMIT_LEVELS)
            ]
            th = vals.get(f"buy_{i+1}_threshold", 5)
            lines.append(
                f"  买入#{i+1}: {BUY_BUILDERS_SIMP[n_idx]} "
                f"th_lv={th} limit={limit:.0f}元"
            )
        for i in range(NUM_SELL_RULES):
            n_idx = vals.get(f"sell_{i+1}_name", 0) % len(SELL_BUILDERS_SIMP)
            limit = SELL_LIMIT_LEVELS[
                vals.get(f"sell_{i+1}_limit", 1) % len(SELL_LIMIT_LEVELS)
            ]
            th = vals.get(f"sell_{i+1}_threshold", 5)
            lines.append(
                f"  卖出#{i+1}: {SELL_BUILDERS_SIMP[n_idx]} "
                f"th_lv={th} limit={limit:.0f}元"
            )
        return "\n".join(lines)
