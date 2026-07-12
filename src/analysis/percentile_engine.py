"""PercentileSignalFn（§8 新参数化）。

每只标的独立评估自身历史分位，加权求和打分，分数够高则触发买卖。
松弛 H1-H3：标的自比较分位、历史分布窗口、参数空间缩小 3 个数量级。
"""
from __future__ import annotations

import random as _random
import numpy as np
from typing import TYPE_CHECKING

from .signal_functions import SignalFn, ParamDim, ParamSpace, Params
from .fast_evaluator import IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT, \
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT

if TYPE_CHECKING:
    from .genetic_searcher import StrategyEncoding
    from .optimizer_constraints import DiscreteSearchConfig

# 分位列索引
PERCENTILE_COLUMNS = [
    IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT,
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT,
]
PERCENTILE_LABELS = [
    "adx_pct", "rsi_pct", "deviation_pct",
    "vol_ratio_pct", "ma200_dev_pct",
]

N_SIGNALS = len(PERCENTILE_COLUMNS)  # 5
TAU_LEVELS = 10
W_LEVELS = 5  # [0.1, 0.3, 0.5, 0.7, 0.9]


def _decode_tau(level: int) -> float:
    return 0.1 + (level / max(TAU_LEVELS - 1, 1)) * 0.8


def _decode_w(level: int) -> float:
    ws = [0.1, 0.3, 0.5, 0.7, 0.9]
    return ws[min(level, len(ws) - 1)]


class PercentileSignalFn(SignalFn):
    """分位评分引擎 — 新参数化, 松弛 H1/H2/H3。

    参数空间: 5 个分位信号 × (τ, w) + τ_buy + τ_sell + pos_frac。
    信号输出: 评分矩阵由各加权分位数计算, 每个标的用自己的历史分位。
    """

    def __init__(self):
        dims = []
        for lbl in PERCENTILE_LABELS:
            dims.append(ParamDim(f"{lbl}_tau", TAU_LEVELS, 0.1, 0.9))
            dims.append(ParamDim(f"{lbl}_w", W_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("buy_score_thresh", TAU_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("sell_score_thresh", TAU_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("position_frac", 5, 0.05, 0.45))
        self._space = ParamSpace(dims)

    @property
    def name(self) -> str:
        return "percentile"

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        T, N, K = indicator_matrix.shape
        buy_scores = np.zeros((T, N), dtype=np.float32)
        sell_scores = np.zeros((T, N), dtype=np.float32)
        total_w = 0.0

        for ci, col in enumerate(PERCENTILE_COLUMNS):
            lbl = PERCENTILE_LABELS[ci]
            tau = _decode_tau(params.values.get(f"{lbl}_tau", 5))
            w = _decode_w(params.values.get(f"{lbl}_w", 2))
            if w <= 0 or col >= K:
                continue
            col_data = indicator_matrix[:, :, col]
            valid = ~np.isnan(col_data)
            above = (valid & (col_data > tau)).astype(np.float32)
            below = (valid & (col_data < tau)).astype(np.float32)
            buy_scores += w * above
            sell_scores += w * below
            total_w += w

        if total_w > 0:
            buy_scores /= total_w
            sell_scores /= total_w

        return np.stack([buy_scores, sell_scores], axis=-1)

    def to_human_readable(self, params: Params) -> str:
        lines = ["分位评分策略 (PercentileSignalFn)"]
        for ci, lbl in enumerate(PERCENTILE_LABELS):
            tau = _decode_tau(params.values.get(f"{lbl}_tau", 5))
            w = _decode_w(params.values.get(f"{lbl}_w", 2))
            lines.append(f"  {lbl}: tau={tau:.2f}, w={w:.2f}")
        buy_th = _decode_tau(params.values.get("buy_score_thresh", 5))
        sell_th = _decode_tau(params.values.get("sell_score_thresh", 5))
        pos_frac = [0.05, 0.15, 0.25, 0.35, 0.45][min(params.values.get("position_frac", 2), 4)]
        lines.append(f"  买入阈值 τ_buy={buy_th:.2f}  卖出阈值 τ_sell={sell_th:.2f}")
        lines.append(f"  仓位比例 frac={pos_frac:.2f}")
        return "\n".join(lines)
