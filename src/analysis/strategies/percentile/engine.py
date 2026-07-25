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

    name = "percentile"
    label = "分位评分引擎 (标的自比较分位, 推荐)"
    description = "每只标的对自身252日历史算各指标分位排名, 加权求和打分, 13维参数"

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

    def make_signals(self, params: Params, indicator_matrix: np.ndarray):
        """分数 → 二值信号：与旧 optimizer 公式一致。"""
        scores = self.evaluate(params, indicator_matrix)
        buy_th = params.values.get("buy_score_thresh", 5)
        sell_th = params.values.get("sell_score_thresh", 5)
        # 精确匹配旧 optimizer.py 的阈值公式: level/10 + 0.1
        return (
            scores[:, :, 0] > (buy_th / 10.0 + 0.1),
            scores[:, :, 1] > (sell_th / 10.0 + 0.1),
        )

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

    # ── 兼容旧 PercentileSignalFn API ──

    def score_timeseries(self, params, hist_df):
        """整段历史的每日净买卖评分 (T,)，供日报组合回测用。"""
        import pandas as _pd
        vals = params.values if hasattr(params, 'values') else params
        T = len(hist_df)
        buy = np.zeros(T, dtype=np.float32)
        sell = np.zeros(T, dtype=np.float32)
        total_w = 0.0
        for lbl in PERCENTILE_LABELS:
            tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
            w = _decode_w(vals.get(f"{lbl}_w", 2))
            if w <= 0:
                continue
            src_col = {"adx_pct": "adx", "rsi_pct": "rsi",
                       "deviation_pct": "deviation", "vol_ratio_pct": "vol_ratio",
                       "ma200_dev_pct": "ma200_dev"}.get(lbl, lbl)
            if src_col not in hist_df.columns:
                continue
            s = hist_df[src_col].values.astype(float)
            pct = np.full(T, np.nan, dtype=np.float32)
            for t in range(T):
                lo = max(0, t - PCT_WINDOW + 1)
                win = s[lo:t+1]
                win = win[~np.isnan(win)]
                if len(win) < 20:
                    continue
                pct[t] = (win <= s[t]).sum() / max(len(win), 1)
            buy += w * (pct > tau).astype(np.float32)
            sell += w * (pct < tau).astype(np.float32)
            total_w += w
        if total_w > 0:
            buy /= total_w
            sell /= total_w
        return buy, sell

    def scan_signals(self, params, today: dict, history=None) -> list[dict]:
        """用分位评分逻辑判断今日买卖信号（兼容旧 PercentileSignalFn API）。"""
        from .scanner import scan_percentile_today
        vals = params.values if hasattr(params, 'values') else params
        return scan_percentile_today(Params(values=dict(vals)), today, history)

    _POS_FRACS = [0.05, 0.15, 0.25, 0.35, 0.45]

    def _decode_pos_frac(self, level):
        return self._POS_FRACS[min(int(level), len(self._POS_FRACS) - 1)]

    def execution_params(self, params) -> dict:
        vals = params.values if hasattr(params, 'values') else params
        return {
            "buy_threshold": _decode_tau(vals.get("buy_score_thresh", 5)),
            "sell_threshold": _decode_tau(vals.get("sell_score_thresh", 5)),
            "position_frac": self._decode_pos_frac(vals.get("position_frac", 2)),
        }

    # Expose PERCENTILE_HUMAN for external callers
    PERCENTILE_HUMAN = PERCENTILE_HUMAN
