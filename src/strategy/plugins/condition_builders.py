"""Reusable causal condition functions for rule-based strategies.

This module intentionally contains no strategy registration. It keeps the
signal primitives shared by the simplified strategy independent from any
particular strategy plugin.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from ...backtest.engine import (
    IDX_ADX,
    IDX_BOLL_PCT_B,
    IDX_CLOSE,
    IDX_DEVIATION,
    IDX_MA60,
    IDX_MACD_HIST,
    IDX_RSI,
    IDX_VOL_RATIO,
)


def _build_none(indicator, threshold_norm):
    time_rows, symbols = indicator.shape[:2]
    return np.zeros((time_rows, symbols), dtype=bool), np.zeros(
        (time_rows, symbols), dtype=float
    )


def _build_deviation_cross(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    dev_prev = np.roll(dev, 1, axis=0)
    dev_prev[0] = dev[0]
    threshold = -0.005 + threshold_norm * (-0.30 + 0.005)
    condition = (dev_prev > threshold) & (dev <= threshold)
    condition[0] = False
    reset = np.abs(dev) < 0.01
    return condition, reset.astype(float)


def _build_rsi_signal(indicator, threshold_norm):
    rsi = indicator[:, :, IDX_RSI]
    threshold = 80.0 - threshold_norm * 50.0
    condition = rsi < threshold
    rsi_center = np.abs(rsi - (100.0 - threshold))
    reset = rsi_center < 5.0
    return condition, reset.astype(float)


def _build_bollinger_signal(indicator, threshold_norm):
    bollinger = indicator[:, :, IDX_BOLL_PCT_B]
    threshold = 0.05 + threshold_norm * 0.45
    condition = bollinger < threshold
    reset = np.abs(bollinger - 0.5) < 0.10
    return condition, reset.astype(float)


def _build_volume_spike(indicator, threshold_norm):
    volume_ratio = indicator[:, :, IDX_VOL_RATIO]
    threshold = 1.2 + threshold_norm * 2.8
    condition = volume_ratio > threshold
    reset = volume_ratio < 1.0
    return condition, reset.astype(float)


def _build_deviation_absolute(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    threshold = -0.05 - threshold_norm * 0.45
    condition = dev < threshold
    reset = np.abs(dev) < 0.01
    return condition, reset.astype(float)


def _build_trend_follow(indicator, threshold_norm):
    adx = indicator[:, :, IDX_ADX]
    macd_hist = indicator[:, :, IDX_MACD_HIST]
    adx_threshold = 20.0 + threshold_norm * 40.0
    condition = (adx > adx_threshold) & (macd_hist > 0)
    reset = adx < 15.0
    return condition, reset.astype(float)


def _build_absolute_discount(indicator, threshold_norm):
    close = indicator[:, :, IDX_CLOSE]
    # Historical running high only. Reversing the series leaks future peaks.
    all_time_high = np.fmax.accumulate(close, axis=0)
    discount = (close - all_time_high) / np.maximum(all_time_high, 1e-6)
    threshold = -0.20 - threshold_norm * 0.30
    condition = discount < threshold
    reset = discount > -0.03
    return condition, reset.astype(float)


def _build_deep_value(indicator, threshold_norm):
    close = indicator[:, :, IDX_CLOSE]
    ma60 = indicator[:, :, IDX_MA60]
    ma200 = np.zeros_like(close)
    time_rows = close.shape[0]
    for index in range(time_rows):
        lookback_start = max(0, index - 199)
        ma200[index] = (
            np.mean(close[lookback_start : index + 1], axis=0)
            if index >= 199
            else ma60[index]
        )
    previous_ma200 = np.roll(ma200, 20, axis=0)
    slope = (ma200 - previous_ma200) / np.maximum(previous_ma200, 1e-6)
    slope[:20] = 0
    condition = (close < ma200 * 0.8) & (slope > -0.05)
    reset = close > ma200 * 0.95
    return condition, reset.astype(float)


def _build_sell_deviation_cross(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    dev_prev = np.roll(dev, 1, axis=0)
    dev_prev[0] = dev[0]
    threshold = 0.005 + threshold_norm * 0.295
    condition = (dev_prev < threshold) & (dev >= threshold)
    condition[0] = False
    reset = np.abs(dev) < 0.01
    return condition, reset.astype(float)


def _build_sell_rsi_signal(indicator, threshold_norm):
    rsi = indicator[:, :, IDX_RSI]
    threshold = 20.0 + threshold_norm * 50.0
    condition = rsi > threshold
    reset = np.abs(rsi - (100.0 - threshold)) < 5.0
    return condition, reset.astype(float)


def _build_sell_bollinger_signal(indicator, threshold_norm):
    bollinger = indicator[:, :, IDX_BOLL_PCT_B]
    threshold = 0.95 - threshold_norm * 0.45
    condition = bollinger > threshold
    reset = np.abs(bollinger - 0.5) < 0.10
    return condition, reset.astype(float)


def _build_sell_absolute(indicator, threshold_norm):
    dev = indicator[:, :, IDX_DEVIATION]
    threshold = 0.05 + threshold_norm * 0.45
    condition = dev > threshold
    reset = np.abs(dev) < 0.01
    return condition, reset.astype(float)


def _build_sell_trend_reverse(indicator, threshold_norm):
    adx = indicator[:, :, IDX_ADX]
    macd_hist = indicator[:, :, IDX_MACD_HIST]
    adx_threshold = 20.0 + threshold_norm * 40.0
    condition = (adx > adx_threshold) & (macd_hist < 0)
    reset = adx < 15.0
    return condition, reset.astype(float)


def _build_sell_profit_taking(indicator, threshold_norm):
    close = indicator[:, :, IDX_CLOSE]
    ma60 = indicator[:, :, IDX_MA60]
    deviation = (close - ma60) / np.maximum(ma60, 1e-6)
    threshold = 0.10 + threshold_norm * 0.40
    condition = deviation > threshold
    reset = deviation < 0.02
    return condition, reset.astype(float)


CONDITION_BUILDERS_FAST: dict[
    str, Callable[[np.ndarray, float], tuple[np.ndarray, np.ndarray]]
] = {
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
