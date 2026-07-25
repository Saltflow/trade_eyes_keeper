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

    name = "simplified"
    label = "简化限额引擎 (固定100k+30天+独立限额)"
    description = "固定总资金100k, 最低持仓30天, 每条规则独立买卖限额"

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
    def param_space(self) -> ParamSpace:
        return self._space

    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        """简化策略返回空评分——实际由 FastEvaluator.evaluate() 直接使用 limits 参数。"""
        T, N = indicator_matrix.shape[:2]
        return np.zeros((T, N, 2), dtype=np.float32)

    def make_signals(self, params: Params, indicator_matrix: np.ndarray):
        """Params → builder名/阈值 → CONDITION_BUILDERS_FAST → lock/reset/confirm → bool信号。
        与 builder 策略共用相同的信号生成管道，区别仅在于限额 vs 比例。
        """
        import numpy as np
        from ..builder.engine import CONDITION_BUILDERS_FAST
        from ...backtester import _apply_lock_reset_numba, _apply_lock_reset, _apply_confirmation

        try:
            from numba import jit as _  # noqa
            HAS_NUMBA = True
        except ImportError:
            HAS_NUMBA = False

        T, N = indicator_matrix.shape[:2]

        # Decode buy params
        buy_builders, buy_thresholds = [], []
        for i in range(NUM_BUY_RULES):
            n = params.values.get(f"buy_{i+1}_name", 0) % len(BUY_BUILDERS_SIMP)
            buy_builders.append(BUY_BUILDERS_SIMP[n])
            buy_thresholds.append(
                params.values.get(f"buy_{i+1}_threshold", 5)
                / (THRESHOLD_LEVELS_SIMP - 1)
            )
        # Decode sell params
        sell_builders, sell_thresholds = [], []
        for i in range(NUM_SELL_RULES):
            n = params.values.get(f"sell_{i+1}_name", 0) % len(SELL_BUILDERS_SIMP)
            sell_builders.append(SELL_BUILDERS_SIMP[n])
            sell_thresholds.append(
                params.values.get(f"sell_{i+1}_threshold", 5)
                / (THRESHOLD_LEVELS_SIMP - 1)
            )

        R = len(buy_builders)
        buy_conds = np.zeros((R, T, N), dtype=bool)
        buy_resets_arr = np.zeros((R, T, N), dtype=float)
        for r in range(R):
            fn = CONDITION_BUILDERS_FAST.get(buy_builders[r])
            if fn is None:
                continue
            th = buy_thresholds[r] if r < len(buy_thresholds) else 0.5
            c, rs = fn(indicator_matrix, th)
            buy_conds[r] = c
            buy_resets_arr[r] = rs
        if HAS_NUMBA:
            buy_signals, _ = _apply_lock_reset_numba(buy_conds, buy_resets_arr)
        else:
            buy_signals, _ = _apply_lock_reset(buy_conds, buy_resets_arr)
        buy_signals = _apply_confirmation(buy_conds.any(axis=0), 3)

        S = len(sell_builders)
        sell_conds = np.zeros((S, T, N), dtype=bool)
        sell_resets_arr2 = np.zeros((S, T, N), dtype=float)
        for r in range(S):
            fn = CONDITION_BUILDERS_FAST.get(sell_builders[r])
            if fn is None:
                continue
            th = sell_thresholds[r] if r < len(sell_thresholds) else 0.5
            c, rs = fn(indicator_matrix, th)
            sell_conds[r] = c
            sell_resets_arr2[r] = rs
        if HAS_NUMBA:
            sell_signals, _ = _apply_lock_reset_numba(sell_conds, sell_resets_arr2)
        else:
            sell_signals, _ = _apply_lock_reset(sell_conds, sell_resets_arr2)
        sell_signals = _apply_confirmation(sell_conds.any(axis=0), 1)

        return buy_signals, sell_signals

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
