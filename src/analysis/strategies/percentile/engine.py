"""分位评分搜参策略 — 每只标的独立评估自身历史分位。"""

from __future__ import annotations

import numpy as np

from ...search_interface import SearchStrategy, ParamDim, ParamSpace, Params
from ...backtester import (
    IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT,
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT,
    compute_percentile_scores,
)

# ── 分位列常量 ──
PERCENTILE_COLUMNS = [
    IDX_ADX_PCT, IDX_RSI_PCT, IDX_DEVIATION_PCT,
    IDX_VOL_RATIO_PCT, IDX_MA200_DEV_PCT,
]
PERCENTILE_LABELS = [
    "adx_pct", "rsi_pct", "deviation_pct",
    "vol_ratio_pct", "ma200_dev_pct",
]
PERCENTILE_HUMAN = {
    "adx_pct": "趋势强度分位(ADX)",
    "rsi_pct": "超买超卖分位(RSI)",
    "deviation_pct": "均线偏离分位",
    "vol_ratio_pct": "量能分位",
    "ma200_dev_pct": "长期趋势分位(MA200)",
}
PCT_WINDOW = 252
TAU_LEVELS = 10


def _decode_tau(level: int) -> float:
    return 0.1 + (level / max(TAU_LEVELS - 1, 1)) * 0.8


def _decode_w(level: int) -> float:
    ws = [0.1, 0.3, 0.5, 0.7, 0.9]
    return ws[min(level, len(ws) - 1)]


class PercentileSearchStrategy(SearchStrategy):
    """分位评分搜参策略。"""

    def __init__(self):
        dims = []
        for lbl in PERCENTILE_LABELS:
            dims.append(ParamDim(f"{lbl}_tau", TAU_LEVELS, 0.1, 0.9))
            dims.append(ParamDim(f"{lbl}_w", 5, 0.0, 1.0))
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
        pct_thresholds = []
        weights = []
        for lbl in PERCENTILE_LABELS:
            tau = _decode_tau(params.values.get(f"{lbl}_tau", 5))
            w = _decode_w(params.values.get(f"{lbl}_w", 2))
            pct_thresholds.append(tau)
            weights.append(w)

        buy_scores, sell_scores = compute_percentile_scores(
            indicator_matrix, PERCENTILE_COLUMNS, pct_thresholds, weights,
        )
        return np.stack([buy_scores, sell_scores], axis=-1).astype(np.float32)

    def scan_today(self, params, today: dict, history=None) -> list[dict]:
        from .scanner import scan_percentile_today
        return scan_percentile_today(params, today, history)

    def to_human_readable(self, params: Params) -> str:
        lines = ["分位评分策略 (PercentileSignalFn)"]
        vals = params.values
        for lbl in PERCENTILE_LABELS:
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            lines.append(f"  {PERCENTILE_HUMAN[lbl]}: tau={tau:.2f}, w={w:.2f}")
        buy_th = _decode_tau(vals.get("buy_score_thresh", 5))
        sell_th = _decode_tau(vals.get("sell_score_thresh", 5))
        lines.append(f"  买入阈值 τ_buy={buy_th:.2f}  卖出阈值 τ_sell={sell_th:.2f}")
        return "\n".join(lines)
