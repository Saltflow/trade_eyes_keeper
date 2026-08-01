"""简化条件策略 — 共用每笔买卖现金档位。"""

from __future__ import annotations

import numpy as np

from ..registry import register_strategy
from ..api import ArraySignalStrategy, ParamDim, ParamSpace, Params

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


@register_strategy("simplified")
class SimplifiedStrategy(ArraySignalStrategy):
    """简化条件搜参策略；执行金额由基类的现金档位统一管理。"""

    name = "simplified"
    label = "简化现金档位引擎"
    description = "固定条件信号 + 统一单笔买卖现金档位"
    warmup_rows = 200

    def __init__(self):
        dims = []
        for i in range(NUM_BUY_RULES):
            dims.append(ParamDim(
                f"buy_{i+1}_name", len(BUY_BUILDERS_SIMP), 0, 1
            ))
            dims.append(ParamDim(
                f"buy_{i+1}_threshold", THRESHOLD_LEVELS_SIMP, 0, 1
            ))
        for i in range(NUM_SELL_RULES):
            dims.append(ParamDim(
                f"sell_{i+1}_name", len(SELL_BUILDERS_SIMP), 0, 1
            ))
            dims.append(ParamDim(
                f"sell_{i+1}_threshold", THRESHOLD_LEVELS_SIMP, 0, 1
            ))
        self._space = self.with_execution_dims(dims)

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def score_signals(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        """Return neutral priorities; decisions come from ``_make_signal_arrays``."""
        T, N = indicator_matrix.shape[:2]
        return np.zeros((T, N, 2), dtype=np.float32)

    def signal_arrays(self, params: Params, indicator_matrix: np.ndarray):
        """Params → builder名/阈值 → CONDITION_BUILDERS_FAST → lock/reset/confirm → bool信号。
        与 builder 策略共用相同的信号生成管道，区别仅在于限额 vs 比例。
        """
        import numpy as np
        from .builder import CONDITION_BUILDERS_FAST
        from ...backtest.engine import (
            _apply_confirmation,
            _apply_lock_reset,
            _apply_lock_reset_numba,
        )

        try:
            import numba  # noqa: F401
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

    def to_human_readable(self, params: Params) -> str:
        vals = params.values
        lines = ["简化限额策略 (simplified)"]
        lines.append("  使用统一单笔现金上限")
        for i in range(NUM_BUY_RULES):
            n_idx = vals.get(f"buy_{i+1}_name", 0) % len(BUY_BUILDERS_SIMP)
            th = vals.get(f"buy_{i+1}_threshold", 5)
            lines.append(
                f"  买入#{i+1}: {BUY_BUILDERS_SIMP[n_idx]} "
                f"th_lv={th}"
            )
        for i in range(NUM_SELL_RULES):
            n_idx = vals.get(f"sell_{i+1}_name", 0) % len(SELL_BUILDERS_SIMP)
            th = vals.get(f"sell_{i+1}_threshold", 5)
            lines.append(
                f"  卖出#{i+1}: {SELL_BUILDERS_SIMP[n_idx]} "
                f"th_lv={th}"
            )
        execution = self.execution_params(params)
        lines.append(
            "  单笔现金上限: "
            f"买入 {execution['buy_cash_limit']:.0f} / "
            f"卖出 {execution['sell_cash_limit']:.0f} 元"
        )
        return "\n".join(lines)
