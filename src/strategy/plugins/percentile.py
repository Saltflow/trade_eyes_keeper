"""分位评分搜参策略 — 每只标的独立评估自身历史分位。"""

from __future__ import annotations

import numpy as np

from ..registry import register_strategy
from ..api import ArraySignalStrategy, ParamDim, ParamSpace, Params
from ...backtest.engine import (
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


@register_strategy("percentile")
class PercentileStrategy(ArraySignalStrategy):
    """分位评分搜参策略。"""

    name = "percentile"
    label = "分位评分引擎 (标的自比较分位)"
    description = "每只标的对自身252日历史算各指标分位排名, 加权求和打分"
    warmup_rows = PCT_WINDOW

    def __init__(self):
        dims = []
        for lbl in PERCENTILE_LABELS:
            dims.append(ParamDim(f"{lbl}_tau", TAU_LEVELS, 0.1, 0.9))
            dims.append(ParamDim(f"{lbl}_w", 5, 0.0, 1.0))
        dims.append(ParamDim("buy_score_thresh", TAU_LEVELS, 0.1, 0.9))
        dims.append(ParamDim("sell_score_thresh", TAU_LEVELS, 0.1, 0.9))
        self._space = self.with_execution_dims(dims)

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def score_signals(
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

    def signal_arrays(self, params: Params, indicator_matrix: np.ndarray):
        """分数 → 二值信号：与旧 optimizer 公式一致。"""
        scores = self.score_signals(params, indicator_matrix)
        buy_th = params.values.get("buy_score_thresh", 5)
        sell_th = params.values.get("sell_score_thresh", 5)
        # Scanner, optimizer and daily reports share this exact decoder.
        return (
            scores[:, :, 0] > _decode_tau(buy_th),
            scores[:, :, 1] > _decode_tau(sell_th),
        )

    def to_human_readable(self, params: Params) -> str:
        lines = ["分位评分策略 (percentile)"]
        vals = params.values
        for lbl in PERCENTILE_LABELS:
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            lines.append(f"  {PERCENTILE_HUMAN[lbl]}: tau={tau:.2f}, w={w:.2f}")
        buy_th = _decode_tau(vals.get("buy_score_thresh", 5))
        sell_th = _decode_tau(vals.get("sell_score_thresh", 5))
        lines.append(f"  买入阈值 τ_buy={buy_th:.2f}  卖出阈值 τ_sell={sell_th:.2f}")
        execution = self.execution_params(params)
        lines.append(
            "  单笔现金上限: "
            f"买入 {execution['buy_cash_limit']:.0f} / "
            f"卖出 {execution['sell_cash_limit']:.0f} 元"
        )
        return "\n".join(lines)
