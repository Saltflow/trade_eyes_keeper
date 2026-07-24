"""分位评分策略的今日告警扫描。"""

from __future__ import annotations

import numpy as np

from ...search_interface import Params
from .engine import (
    PERCENTILE_LABELS, PERCENTILE_HUMAN, PCT_WINDOW,
    _decode_tau, _decode_w,
)

# 分位信号 → 原始指标源列名（计算滚动分位用）
PERCENTILE_SOURCES = {
    "adx_pct": "adx", "rsi_pct": "rsi",
    "deviation_pct": "deviation", "vol_ratio_pct": "vol_ratio",
    "ma200_dev_pct": "ma200_dev",
}


def _rolling_percentile(series, window: int = PCT_WINDOW) -> float | None:
    """最新值在过去 window 天内的分位排名 (0-1)。"""
    vals = np.asarray(series, dtype=float)
    vals = vals[~np.isnan(vals)]
    if len(vals) < 20:
        return None
    win = vals[-window:]
    cur = win[-1]
    return float((win <= cur).sum()) / max(len(win), 1)


def scan_percentile_today(params: Params, today: dict, history=None) -> list[dict]:
    """用分位评分逻辑判断今日是否触发买/卖信号。"""
    vals = params.values
    alerts = []
    for lbl in PERCENTILE_LABELS:
        tau = _decode_tau(vals.get(f"{lbl}_tau", 5))
        src = PERCENTILE_SOURCES.get(lbl, lbl)

        # 优先用 history 算滚动分位
        pct_val = None
        if history is not None and src in history.columns:
            pct_val = _rolling_percentile(history[src].values)
        elif src in today:
            pct_val = today[src]
            # 如果 today 给的是原始值（非分位），降级
            if isinstance(pct_val, (int, float)) and pct_val > 1.5:
                pct_val = 0.5

        if pct_val is None or not isinstance(pct_val, (int, float)):
            continue
        label = PERCENTILE_HUMAN.get(lbl, lbl)
        if pct_val > tau:
            alerts.append({
                "side": "buy", "label": label,
                "detail": f"{label}: 分位 {pct_val:.2f} > τ {tau:.2f}",
            })
        elif pct_val < tau:
            alerts.append({
                "side": "sell", "label": label,
                "detail": f"{label}: 分位 {pct_val:.2f} < τ {tau:.2f}",
            })
    return alerts
