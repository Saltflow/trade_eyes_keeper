"""条件构建器搜参策略 — 15 个信号构建函数 + 全局阈值引擎。"""

from __future__ import annotations

import numpy as np

from ...search_interface import SearchStrategy, ParamDim, ParamSpace, Params
from ...backtester import (
    IDX_CLOSE, IDX_MA60, IDX_DEVIATION, IDX_RSI, IDX_MACD,
    IDX_MACD_HIST, IDX_VOL_RATIO, IDX_BOLL_PCT_B, IDX_ADX,
)


# ═══════════════════════════════════════════════════════════════
# 15 个信号构建函数
# ═══════════════════════════════════════════════════════════════

def _build_none(indicator, threshold_norm):
    T, N = indicator.shape[:2]
    return np.zeros((T, N), dtype=bool), np.zeros((T, N), dtype=float)

def _build_deviation_cross(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    dev_prev = np.roll(dev, 1, axis=0)
    dev_prev[0] = dev[0]
    t = -0.005 + threshold_norm * (-0.30 + 0.005)
    condition = (dev_prev > t) & (dev <= t)
    condition[0] = False
    reset = np.abs(dev) < 0.01
    return condition, reset.astype(float)

def _build_rsi_signal(indicator, threshold_norm):
    rsi = indicator[:, :, IDX_RSI]
    t = 80.0 - threshold_norm * 50.0
    c = rsi < t
    rsi_center = np.abs(rsi - (100.0 - t))
    reset = rsi_center < 5.0
    return c, reset.astype(float)

def _build_bollinger_signal(indicator, threshold_norm):
    boll = indicator[:, :, IDX_BOLL_PCT_B]
    t = 0.05 + threshold_norm * 0.45
    c = boll < t
    reset = np.abs(boll - 0.5) < 0.10
    return c, reset.astype(float)

def _build_volume_spike(indicator, threshold_norm):
    vol = indicator[:, :, IDX_VOL_RATIO]
    t = 1.2 + threshold_norm * 2.8
    c = vol > t
    reset = vol < 1.0
    return c, reset.astype(float)

def _build_deviation_absolute(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    t = -0.05 - threshold_norm * 0.45
    c = dev < t
    reset = np.abs(dev) < 0.01
    return c, reset.astype(float)

def _build_trend_follow(indicator, threshold_norm):
    adx = indicator[:, :, IDX_ADX]
    macd_hist = indicator[:, :, IDX_MACD_HIST]
    adx_t = 20.0 + threshold_norm * 40.0
    c = (adx > adx_t) & (macd_hist > 0)
    reset = adx < 15.0
    return c, reset.astype(float)

def _build_absolute_discount(indicator, threshold_norm):
    close = indicator[:, :, IDX_CLOSE]
    T, N = close.shape
    ath = np.maximum.accumulate(close[::-1], axis=0)[::-1]
    discount = (close - ath) / np.maximum(ath, 1e-6)
    t = -0.20 - threshold_norm * 0.30
    c = discount < t
    reset = discount > -0.03
    return c, reset.astype(float)

def _build_deep_value(indicator, threshold_norm):
    close = indicator[:, :, IDX_CLOSE]
    ma60 = indicator[:, :, IDX_MA60]
    ma200 = np.zeros_like(close)
    T = close.shape[0]
    for t in range(T):
        lo = max(0, t - 199)
        ma200[t] = np.mean(close[lo:t + 1], axis=0) if t >= 199 else ma60[t]
    slope = (ma200 - np.roll(ma200, 20, axis=0)) / np.maximum(np.roll(ma200, 20, axis=0), 1e-6)
    slope[:20] = 0
    c = (close < ma200 * 0.8) & (slope > -0.05)
    reset = close > ma200 * 0.95
    return c, reset.astype(float)

def _build_sell_deviation_cross(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    dev_prev = np.roll(dev, 1, axis=0)
    dev_prev[0] = dev[0]
    t = 0.005 + threshold_norm * 0.295
    condition = (dev_prev < t) & (dev >= t)
    condition[0] = False
    reset = np.abs(dev) < 0.01
    return condition, reset.astype(float)

def _build_sell_rsi_signal(indicator, threshold_norm):
    rsi = indicator[:, :, IDX_RSI]
    t = 20.0 + threshold_norm * 50.0
    c = rsi > t
    reset = np.abs(rsi - (100.0 - t)) < 5.0
    return c, reset.astype(float)

def _build_sell_bollinger_signal(indicator, threshold_norm):
    boll = indicator[:, :, IDX_BOLL_PCT_B]
    t = 0.95 - threshold_norm * 0.45
    c = boll > t
    reset = np.abs(boll - 0.5) < 0.10
    return c, reset.astype(float)

def _build_sell_absolute(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    t = 0.05 + threshold_norm * 0.45
    c = dev > t
    reset = np.abs(dev) < 0.01
    return c, reset.astype(float)

def _build_sell_trend_reverse(indicator, threshold_norm):
    adx = indicator[:, :, IDX_ADX]
    macd_hist = indicator[:, :, IDX_MACD_HIST]
    adx_t = 20.0 + threshold_norm * 40.0
    c = (adx > adx_t) & (macd_hist < 0)
    reset = adx < 15.0
    return c, reset.astype(float)

def _build_sell_profit_taking(indicator, threshold_norm):
    close = indicator[:, :, IDX_CLOSE]
    ma60 = indicator[:, :, IDX_MA60]
    dev = (close - ma60) / np.maximum(ma60, 1e-6)
    t = 0.10 + threshold_norm * 0.40
    c = dev > t
    reset = dev < 0.02
    return c, reset.astype(float)


CONDITION_BUILDERS_FAST: dict[str, callable] = {
    "none": _build_none,
    "deviation_cross": _build_deviation_cross,
    "rsi_signal": _build_rsi_signal,
    "bollinger_signal": _build_bollinger_signal,
    "volume_spike": _build_volume_spike,
    "deviation_absolute": _build_deviation_absolute,
    "trend_follow": _build_trend_follow,
    "absolute_discount": _build_absolute_discount,
    "deep_value": _build_deep_value,
    "sell_deviation_cross": _build_sell_deviation_cross,
    "sell_rsi_signal": _build_sell_rsi_signal,
    "sell_bollinger_signal": _build_sell_bollinger_signal,
    "sell_absolute": _build_sell_absolute,
    "sell_trend_reverse": _build_sell_trend_reverse,
    "sell_profit_taking": _build_sell_profit_taking,
}

BUILDER_COUNT = 8  # 买入 builder 数量（前8个）
SELL_BUILDER_COUNT = 6  # 卖出 builder 数量
THRESHOLD_LEVELS_BUILDER = 10
FRAC_LEVELS_BUILDER = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]


# ═══════════════════════════════════════════════════════════════
# BuilderSearchStrategy
# ═══════════════════════════════════════════════════════════════

class BuilderSearchStrategy(SearchStrategy):
    """条件构建器搜参策略。"""

    def __init__(self):
        dims = []
        buy_names = list(CONDITION_BUILDERS_FAST.keys())[:BUILDER_COUNT]
        for i in range(5):
            dims.append(ParamDim(f"buy_{i+1}_name", BUILDER_COUNT, 0, 1))
            dims.append(ParamDim(f"buy_{i+1}_threshold", THRESHOLD_LEVELS_BUILDER, 0, 1))
            dims.append(ParamDim(f"buy_{i+1}_frac", len(FRAC_LEVELS_BUILDER), 0, 1))
        sell_names = list(CONDITION_BUILDERS_FAST.keys())[BUILDER_COUNT:BUILDER_COUNT + SELL_BUILDER_COUNT]
        for i in range(3):
            dims.append(ParamDim(f"sell_{i+1}_name", SELL_BUILDER_COUNT, 0, 1))
            dims.append(ParamDim(f"sell_{i+1}_threshold", THRESHOLD_LEVELS_BUILDER, 0, 1))
            dims.append(ParamDim(f"sell_{i+1}_frac", len(FRAC_LEVELS_BUILDER), 0, 1))
        self._space = ParamSpace(dims)

    @property
    def name(self) -> str:
        return "builder"

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        """Builder 策略不产生连续评分——先返回占位，实际由 FastEvaluator.evaluate() 直接调用 builder。"""
        T, N = indicator_matrix.shape[:2]
        return np.zeros((T, N, 2), dtype=np.float32)

    def scan_today(self, params, today: dict, history=None) -> list[dict]:
        from .scanner import scan_builder_today
        return scan_builder_today(params, today, history)

    def to_human_readable(self, params: Params) -> str:
        buy_names = list(CONDITION_BUILDERS_FAST.keys())[:BUILDER_COUNT]
        sell_names = list(CONDITION_BUILDERS_FAST.keys())[BUILDER_COUNT:BUILDER_COUNT + SELL_BUILDER_COUNT]
        vals = params.values
        lines = ["条件构建器策略 (BuilderSignalFn)"]
        for i in range(5):
            n_idx = vals.get(f"buy_{i+1}_name", 0) % len(buy_names)
            th = vals.get(f"buy_{i+1}_threshold", 5)
            fr = FRAC_LEVELS_BUILDER[vals.get(f"buy_{i+1}_frac", 0) % len(FRAC_LEVELS_BUILDER)]
            if buy_names[n_idx] != "none":
                lines.append(f"  买入#{i+1}: {buy_names[n_idx]} th_lv={th} frac={fr:.0%}")
        for i in range(3):
            n_idx = vals.get(f"sell_{i+1}_name", 0) % len(sell_names)
            th = vals.get(f"sell_{i+1}_threshold", 5)
            fr = FRAC_LEVELS_BUILDER[vals.get(f"sell_{i+1}_frac", 0) % len(FRAC_LEVELS_BUILDER)]
            if sell_names[n_idx] != "none":
                lines.append(f"  卖出#{i+1}: {sell_names[n_idx]} th_lv={th} frac={fr:.0%}")
        return "\n".join(lines)
