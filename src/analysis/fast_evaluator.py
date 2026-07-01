"""
向量化快速评估器 (FastEvaluator)

使用 numpy + numba JIT 对一组策略做近似回测评估。
核心理念: 用矩阵运算替代逐日 Python 循环，提速 100x+。

三层架构:
  1. 信号生成: 用 numpy 向量化为每条规则生成 (T, N) 布尔矩阵
  2. 状态机: numba JIT 加速的锁/重置状态机 + 组合持仓模拟
  3. 指标计算: numpy 计算超额收益/回撤/夏普/仓位率

用法:
    from src.analysis.fast_evaluator import FastEvaluator
    evaluator = FastEvaluator(initial_cash=100000, monthly_limit=15000)
    stats = evaluator.evaluate(combined_signals, price_matrix, cash_baseline)

或直接从策略编码评估:
    stats = evaluator.evaluate_from_rules(
        rule_codes, rule_thresholds, rule_fracs,
        indicator_matrix, price_matrix, cash_baseline,
    )
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 指标列索引 (与 walk_forward.py 的 INDICATOR_NAMES 对齐) ──
IDX_CLOSE = 0
IDX_MA60 = 1
IDX_DEVIATION = 2
IDX_RSI = 3
IDX_MACD_HIST = 4
IDX_BOLL_PCT_B = 5
IDX_ADX = 6
IDX_VOL_RATIO = 7
IDX_PCT_FROM_ATH = 8
IDX_MA60_SLOPE = 9
IDX_MA200_DEV = 10

# ── 构建器 → 条件/重置矩阵生成函数 ──
# 每个构建器返回 (condition_matrix, reset_matrix) 各为 (T, N) bool


def _build_deviation_cross(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """MA偏离穿越: 从上方穿越阈值 (买入)"""
    dev = indicator[:, :, IDX_DEVIATION]  # (T, N)
    t = -0.005 + threshold_norm * (-0.30 + 0.005)
    if dev.shape[0] == 0:
        return np.zeros_like(dev, dtype=bool), np.ones_like(dev, dtype=bool)
    prev = np.roll(dev, 1, axis=0)
    prev[0, :] = 0.0
    cond = (dev <= t) & (prev > t)
    reset = dev > 0
    return cond, reset


def _build_rsi_signal(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """RSI 超卖"""
    rsi = indicator[:, :, IDX_RSI]
    if rsi.shape[0] == 0:
        return np.zeros_like(rsi, dtype=bool), np.ones_like(rsi, dtype=bool)
    t = 10 + (1.0 - threshold_norm) * 30
    cond = rsi < t
    reset = rsi > 50
    return cond, reset


def _build_bollinger_signal(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """布林带 %B 低位"""
    bb = indicator[:, :, IDX_BOLL_PCT_B]
    if bb.shape[0] == 0:
        return np.zeros_like(bb, dtype=bool), np.ones_like(bb, dtype=bool)
    t = 0.0 + (1.0 - threshold_norm) * 0.35
    cond = bb < t
    reset = bb > 0.5
    return cond, reset


def _build_volume_spike(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """放量异动 (仅买入)"""
    vr = indicator[:, :, IDX_VOL_RATIO]
    if vr.shape[0] == 0:
        return np.zeros_like(vr, dtype=bool), np.ones_like(vr, dtype=bool)
    t = 1.2 + threshold_norm * 2.8
    cond = vr > t
    reset = vr < 1.0
    return cond, reset


def _build_deviation_absolute(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """MA绝对偏离 (不要求穿越)"""
    dev = indicator[:, :, IDX_DEVIATION]
    if dev.shape[0] == 0:
        return np.zeros_like(dev, dtype=bool), np.ones_like(dev, dtype=bool)
    t = threshold_norm * -0.40
    cond = dev <= t
    reset = dev > 0
    return cond, reset


def _build_trend_follow(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """趋势跟踪: ADX确认 + MACD方向"""
    adx = indicator[:, :, IDX_ADX]
    macd = indicator[:, :, IDX_MACD_HIST]
    if adx.shape[0] == 0:
        return np.zeros_like(adx, dtype=bool), np.ones_like(adx, dtype=bool)
    t = 15 + threshold_norm * 25
    cond = (adx > t) & (macd > 0)
    reset = adx < 15
    return cond, reset


# ── 卖出条件构建器 ──

def _build_sell_deviation_cross(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """MA偏离穿越: 从下方穿越阈值 (卖出)"""
    dev = indicator[:, :, IDX_DEVIATION]
    if dev.shape[0] == 0:
        return np.zeros_like(dev, dtype=bool), np.ones_like(dev, dtype=bool)
    t = 0.005 + threshold_norm * 0.30  # norm 0→1 maps to 0.005→0.30
    prev = np.roll(dev, 1, axis=0)
    prev[0, :] = 0.0
    cond = (dev >= t) & (prev < t)
    reset = dev < 0
    return cond, reset


def _build_sell_rsi_signal(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """RSI 超买"""
    rsi = indicator[:, :, IDX_RSI]
    if rsi.shape[0] == 0:
        return np.zeros_like(rsi, dtype=bool), np.ones_like(rsi, dtype=bool)
    t = 60 + threshold_norm * 30  # norm 0→1 maps to 60→90
    cond = rsi > t
    reset = rsi < 50
    return cond, reset


def _build_sell_bollinger_signal(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """布林带 %B 高位"""
    bb = indicator[:, :, IDX_BOLL_PCT_B]
    if bb.shape[0] == 0:
        return np.zeros_like(bb, dtype=bool), np.ones_like(bb, dtype=bool)
    t = 0.65 + threshold_norm * 0.35  # norm 0→1 maps to 0.65→1.0
    cond = bb > t
    reset = bb < 0.5
    return cond, reset


def _build_sell_deviation_absolute(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """MA绝对偏离 (高位)"""
    dev = indicator[:, :, IDX_DEVIATION]
    if dev.shape[0] == 0:
        return np.zeros_like(dev, dtype=bool), np.ones_like(dev, dtype=bool)
    t = threshold_norm * 0.50  # norm 0→1 maps to 0→0.50
    cond = dev >= t
    reset = dev < 0
    return cond, reset


def _build_sell_trend_follow(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """趋势反转: ADX确认 + MACD<0"""
    adx = indicator[:, :, IDX_ADX]
    macd = indicator[:, :, IDX_MACD_HIST]
    if adx.shape[0] == 0:
        return np.zeros_like(adx, dtype=bool), np.ones_like(adx, dtype=bool)
    t = 15 + threshold_norm * 25
    cond = (adx > t) & (macd < 0)
    reset = adx < 15
    return cond, reset


def _build_none(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """禁用 (永假)"""
    T, N = indicator.shape[:2]
    cond = np.zeros((T, N), dtype=bool)
    reset = np.ones((T, N), dtype=bool)
    return cond, reset


# ── 新增构建器 (v1.18) ──

def _build_absolute_discount(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """距2年高点跌幅超过阈值 (绝对便宜)"""
    pct = indicator[:, :, IDX_PCT_FROM_ATH]
    if pct.shape[0] == 0:
        return np.zeros_like(pct, dtype=bool), np.ones_like(pct, dtype=bool)
    t = -0.10 + threshold_norm * (-0.60)  # norm 0→1 maps to -0.10→-0.70
    cond = pct <= t
    reset = pct > -0.05  # 回到接近高点解锁
    return cond, reset


def _build_deep_value(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """长周期低估 + 趋势不再下滑"""
    dev200 = indicator[:, :, IDX_MA200_DEV]
    slope60 = indicator[:, :, IDX_MA60_SLOPE]
    if dev200.shape[0] == 0:
        return np.zeros_like(dev200, dtype=bool), np.ones_like(dev200, dtype=bool)
    t = -0.05 + threshold_norm * (-0.35)  # norm 0→1 maps to -0.05→-0.40
    cond = (dev200 <= t) & (slope60 > -0.005)  # ma200偏离大 + MA60不再加速下跌
    reset = dev200 > 0
    return cond, reset


def _build_sell_overextended(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """接近2年高点 → 卖"""
    pct = indicator[:, :, IDX_PCT_FROM_ATH]
    if pct.shape[0] == 0:
        return np.zeros_like(pct, dtype=bool), np.ones_like(pct, dtype=bool)
    t = -0.05 + threshold_norm * 0.05  # norm 0→1 maps to -0.05→0.0 (越靠近0越卖)
    cond = pct >= t
    reset = pct < -0.10  # 回撤10%以上解锁
    return cond, reset


# 构建器注册表
CONDITION_BUILDERS_FAST: dict[str, callable] = {
    "deviation_cross": _build_deviation_cross,
    "rsi_signal": _build_rsi_signal,
    "bollinger_signal": _build_bollinger_signal,
    "volume_spike": _build_volume_spike,
    "deviation_absolute": _build_deviation_absolute,
    "trend_follow": _build_trend_follow,
    "none": _build_none,
    # v1.18 新增构建器
    "absolute_discount": _build_absolute_discount,
    "deep_value": _build_deep_value,
    # 卖出构建器（前缀 sell_）
    "sell_deviation_cross": _build_sell_deviation_cross,
    "sell_rsi_signal": _build_sell_rsi_signal,
    "sell_bollinger_signal": _build_sell_bollinger_signal,
    "sell_deviation_absolute": _build_sell_deviation_absolute,
    "sell_trend_follow": _build_sell_trend_follow,
    "sell_overextended": _build_sell_overextended,
}


# ════════════════════════════════════════════════════════════
# numba JIT 加速内核
# ════════════════════════════════════════════════════════════

def _apply_lock_reset(
    rule_conditions: np.ndarray,
    rule_resets: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    对每条规则应用锁/重置状态机。

    Args:
        rule_conditions: (R, T, N) bool — 各规则的原始条件
        rule_resets:      (R, T, N) bool — 什么时候重置锁

    Returns:
        triggered: (T, N) bool — 实际买入信号（任一规则触发）
        trade_count: int — 总交易次数
    """
    R, T, N = rule_conditions.shape
    triggered_combined = np.zeros((T, N), dtype=bool)
    locked = np.zeros((R, N), dtype=bool)
    trade_count = 0

    for t in range(T):
        for n in range(N):
            for r in range(R):
                if rule_resets[r, t, n]:
                    locked[r, n] = False
                if rule_conditions[r, t, n] and not locked[r, n]:
                    triggered_combined[t, n] = True
                    locked[r, n] = True
                    trade_count += 1

    return triggered_combined, trade_count


def _apply_confirmation(
    conditions: np.ndarray,
    confirmation_days: int,
) -> np.ndarray:
    """连续确认过滤。

    只有连续 confirmation_days 天条件都满足时，才在第 confirmation_days 天
    输出 True 信号。中断后计数归零。

    Args:
        conditions: (T, N) bool — 每日条件是否满足
        confirmation_days: 需要连续满足的天数

    Returns:
        (T, N) bool — 确认后的信号
    """
    if confirmation_days <= 1:
        return conditions.copy()

    T, N = conditions.shape
    result = np.zeros((T, N), dtype=bool)
    streak = np.zeros(N, dtype=np.int32)

    for t in range(T):
        for n in range(N):
            if conditions[t, n]:
                streak[n] += 1
                if streak[n] == confirmation_days:
                    result[t, n] = True
                    streak[n] = 0  # 重置，等下一轮连续
            else:
                streak[n] = 0

    return result


try:
    from numba import jit, prange

    @jit(nopython=True, parallel=False, cache=True)
    def _apply_lock_reset_numba(
        rule_conditions,  # (R, T, N) bool
        rule_resets,      # (R, T, N) bool
    ):
        """numba 加速版锁/重置状态机"""
        R, T, N = rule_conditions.shape
        triggered = np.zeros((T, N), dtype=np.bool_)
        locked = np.zeros((R, N), dtype=np.bool_)
        trade_count = 0

        for t in range(T):
            for n in range(N):
                for r in range(R):
                    if rule_resets[r, t, n]:
                        locked[r, n] = False
                    if rule_conditions[r, t, n] and not locked[r, n]:
                        triggered[t, n] = True
                        locked[r, n] = True
                        trade_count += 1

        return triggered, trade_count

    @jit(nopython=True, parallel=False, cache=True)
    def _simulate_portfolio_numba(
        buy_signals,      # (T, N) bool — 买入信号
        sell_signals,     # (T, N) bool — 卖出信号
        prices,           # (T, N) float32
        buy_fracs,        # (R,) float32 — 各规则买入比例
        sell_fracs,       # (S,) float32 — 各规则卖出比例
        initial_cash,     # float
        monthly_limit,    # float
        lot_size,         # int — A股100, 非A股1
        commission_rate,  # float
    ):
        """numba 加速版组合模拟（支持买入+卖出）

        Returns:
            daily_values: (T,) float64 — 每日总资产
            total_trades: int
        """
        T, N = buy_signals.shape
        shares = np.zeros(N, dtype=np.float64)
        cost_basis = np.zeros(N, dtype=np.float64)
        cash = float(initial_cash)
        daily_values = np.zeros(T, dtype=np.float64)
        monthly_spent = 0.0
        current_month = -1
        total_trades = 0

        for t in range(T):
            month = t // 21  # 每月约21个交易日
            if month != current_month:
                monthly_spent = 0.0
                current_month = month

            # ── 先卖后买 ──
            if sell_signals[t].any():
                for n in range(N):
                    if not sell_signals[t, n] or shares[n] <= 0:
                        continue
                    price = float(prices[t, n])
                    if price <= 0.0 or np.isnan(price):
                        continue

                    # 平均卖出比例
                    avg_frac = 0.0
                    count = 0
                    for r in range(len(sell_fracs)):
                        if sell_fracs[r] > 0:
                            avg_frac += sell_fracs[r]
                            count += 1
                    if count > 0:
                        avg_frac /= count
                    else:
                        avg_frac = 0.25

                    sell_qty = shares[n] * avg_frac
                    sell_value = sell_qty * price
                    fee = sell_value * commission_rate
                    cash += sell_value - fee
                    shares[n] -= sell_qty
                    total_trades += 1

            if buy_signals[t].any() and cash > 0:
                for n in range(N):
                    if not buy_signals[t, n]:
                        continue
                    price = float(prices[t, n])
                    if price <= 0.0 or np.isnan(price):
                        continue

                    # 平均买入比例
                    avg_frac = 0.0
                    count = 0
                    for r in range(len(buy_fracs)):
                        if buy_fracs[r] > 0:
                            avg_frac += buy_fracs[r]
                            count += 1
                    if count > 0:
                        avg_frac /= count
                    else:
                        avg_frac = 0.15

                    buy_amount = cash * avg_frac
                    remaining = monthly_limit - monthly_spent
                    buy_amount = min(buy_amount, remaining)

                    if buy_amount <= 0:
                        continue

                    cost = buy_amount * (1.0 - commission_rate)
                    qty = cost / price
                    cost_real = qty * price
                    fee = cost_real * commission_rate
                    total_cost = cost_real + fee

                    if total_cost <= cash:
                        old_sh = shares[n]
                        old_cb = cost_basis[n]
                        shares[n] += qty
                        if shares[n] > 0:
                            cost_basis[n] = (old_sh * old_cb + qty * price) / shares[n]
                        cash -= total_cost
                        monthly_spent += total_cost
                        total_trades += 1

            # 当日总资产
            pos_value = 0.0
            for n in range(N):
                p = float(prices[t, n])
                if not np.isnan(p) and p > 0:
                    pos_value += shares[n] * p
            daily_values[t] = cash + pos_value

        # 计算平均仓位率
        avg_pos_pct = 0.0
        valid_days = 0
        for t in range(T):
            if daily_values[t] > 0:
                pos_value_t = daily_values[t] - cash  # 估算；下面更精确
                # 更精确：遍历持仓
                pv = 0.0
                for n in range(N):
                    px = float(prices[t, n])
                    if not np.isnan(px) and px > 0:
                        pv += shares[n] * px
                if daily_values[t] > 0:
                    avg_pos_pct += pv / daily_values[t]
                    valid_days += 1
        if valid_days > 0:
            avg_pos_pct = avg_pos_pct / valid_days * 100.0

        # 期末仓位率
        final_pos_pct = 0.0
        if T > 0 and daily_values[T - 1] > 0:
            fpv = 0.0
            for n2 in range(N):
                px = float(prices[T - 1, n2])
                if not np.isnan(px) and px > 0:
                    fpv += shares[n2] * px
            final_pos_pct = fpv / daily_values[T - 1] * 100.0

        return daily_values, total_trades, avg_pos_pct, final_pos_pct, shares.copy(), cash, cost_basis.copy()

    @jit(nopython=True, parallel=False, cache=True)
    def _simulate_position_target_numba(
        buy_signals,      # (T, N) bool
        sell_signals,     # (T, N) bool
        prices_close,     # (T, N) float32
        prices_open,      # (T, N) float32
        initial_cash,     # float
        lot_size,         # int
        commission_rate,  # float
        position_slope,   # float
        position_bias,    # float
        max_daily_adjust, # float
        buy_confirm_days, # int
        sell_confirm_days,# int
    ):
        """numba 加速版 Position-Target 组合模拟。"""
        T, N = buy_signals.shape
        shares = np.zeros(N, dtype=np.float64)
        cost_basis = np.zeros(N, dtype=np.float64)
        cash = float(initial_cash)
        daily_values = np.zeros(T, dtype=np.float64)

        # buy_window 用 int 数组替代 Python list of tuples
        buy_win_start = np.full(N, -1, dtype=np.int32)
        buy_win_end = np.full(N, -1, dtype=np.int32)
        total_trades = 0

        for t in range(T):
            # 更新买入窗口
            for n in range(N):
                if buy_signals[t, n] and buy_win_start[n] < 0:
                    start = t - buy_confirm_days + 1
                    if start < 0:
                        start = 0
                    buy_win_start[n] = start
                    buy_win_end[n] = t

            # 计算 NAV
            position_value = 0.0
            for n in range(N):
                p = float(prices_close[t, n])
                if not np.isnan(p) and p > 0:
                    position_value += shares[n] * p
            nav = cash + position_value
            current_pct = position_value / nav if nav > 0 else 0.0

            # 聚合信号 → bullish_score
            n_buy = 0
            n_sell = 0
            for n in range(N):
                if buy_signals[t, n]:
                    n_buy += 1
                if sell_signals[t, n]:
                    n_sell += 1
            total_sig = n_buy + n_sell
            bullish = 0.5 if total_sig == 0 else (float(n_buy) / float(total_sig))

            # sigmoid → target
            centered = (bullish - 0.5) * 2.0
            x = position_slope * centered + position_bias
            if x >= 0:
                target = 1.0 / (1.0 + np.exp(-x))
            else:
                ex = np.exp(x)
                target = ex / (1.0 + ex)

            # delta
            delta = target - current_pct
            if delta > max_daily_adjust:
                delta = max_daily_adjust
            elif delta < -max_daily_adjust:
                delta = -max_daily_adjust

            # ── 执行调仓 ──
            if delta > 1e-6 and cash > 0:
                buy_cash = delta * nav
                if buy_cash > cash:
                    buy_cash = cash

                # 候选：有买入窗口的股票
                n_active = 0
                for n in range(N):
                    if buy_win_start[n] >= 0:
                        n_active += 1

                if n_active > 0:
                    per_stock = buy_cash / float(n_active)
                    for n in range(N):
                        if buy_win_start[n] < 0 or per_stock <= 0:
                            continue
                        sd = buy_win_start[n]
                        ed = buy_win_end[n]
                        buy_win_start[n] = -1

                        # 均价
                        price_sum = 0.0
                        price_count = 0
                        for d in range(sd, ed + 1):
                            po = float(prices_open[d, n])
                            pc = float(prices_close[d, n])
                            if po > 0 and not np.isnan(po):
                                price_sum += po
                                price_count += 1
                            if pc > 0 and not np.isnan(pc):
                                price_sum += pc
                                price_count += 1
                        if price_count == 0:
                            continue
                        exec_price = price_sum / float(price_count)

                        cost = per_stock * (1.0 - commission_rate)
                        qty_raw = cost / exec_price
                        if lot_size > 1:
                            qty = float(np.int64(qty_raw / float(lot_size)) * lot_size)
                        else:
                            qty = qty_raw
                        if qty <= 0:
                            continue
                        cost_real = qty * exec_price
                        fee = cost_real * commission_rate
                        total_cost = cost_real + fee

                        if total_cost <= cash:
                            old_sh = shares[n]
                            old_cb = cost_basis[n]
                            shares[n] += qty
                            if shares[n] > 0:
                                cost_basis[n] = (old_sh * old_cb + qty * exec_price) / shares[n]
                            cash -= total_cost
                            total_trades += 1

            elif delta < -1e-6:
                sell_needed = -delta * nav

                # 持仓总市值
                total_pos = 0.0
                for n in range(N):
                    if shares[n] > 0:
                        p = float(prices_close[t, n])
                        if not np.isnan(p) and p > 0:
                            total_pos += shares[n] * p

                if total_pos > 0:
                    for n in range(N):
                        if sell_needed <= 0 or shares[n] <= 0:
                            continue
                        p_o = float(prices_open[t, n])
                        p_c = float(prices_close[t, n])
                        if p_o <= 0 or p_c <= 0 or np.isnan(p_o) or np.isnan(p_c):
                            continue
                        exec_price = (p_o + p_c) / 2.0

                        pos_val_n = shares[n] * p_c
                        weight = pos_val_n / total_pos if total_pos > 0 else 0
                        sell_amount = sell_needed * weight
                        if sell_amount > pos_val_n:
                            sell_amount = pos_val_n

                        sell_qty_raw = sell_amount / exec_price
                        if lot_size > 1:
                            sell_qty = float(np.int64(sell_qty_raw / float(lot_size)) * lot_size)
                        else:
                            sell_qty = sell_qty_raw
                        if sell_qty <= 0:
                            continue

                        sell_value = sell_qty * exec_price
                        fee = sell_value * commission_rate
                        cash += sell_value - fee
                        shares[n] -= sell_qty
                        sell_needed -= sell_value
                        total_trades += 1

            # 记录 NAV
            pv = 0.0
            for n in range(N):
                p = float(prices_close[t, n])
                if not np.isnan(p) and p > 0:
                    pv += shares[n] * p
            daily_values[t] = cash + pv

        # avg_pos
        pos_sum = 0.0
        vld = 0
        for t in range(T):
            nv = daily_values[t]
            if nv <= 0:
                continue
            pvt = 0.0
            for n in range(N):
                px = float(prices_close[t, n])
                if not np.isnan(px) and px > 0:
                    pvt += shares[n] * px
            pos_sum += pvt / nv
            vld += 1
        avg_pos = (pos_sum / float(vld) * 100.0) if vld > 0 else 0.0

        # final_pos
        fin_nav = daily_values[T - 1] if T > 0 else initial_cash
        fin_pv = 0.0
        for n in range(N):
            px = float(prices_close[T - 1, n])
            if not np.isnan(px) and px > 0:
                fin_pv += shares[n] * px
        fin_pos = (fin_pv / fin_nav * 100.0) if fin_nav > 0 else 0.0

        return daily_values, total_trades, avg_pos, fin_pos, shares.copy(), float(cash), cost_basis.copy()

    HAS_NUMBA = True
    logger.info("numba JIT 已启用，FastEvaluator 将使用加速内核")

except ImportError:
    HAS_NUMBA = False
    logger.warning(
        "numba 未安装，FastEvaluator 将使用纯 Python 回退。"
        "建议安装: pip install numba",
    )


# ════════════════════════════════════════════════════════════
# 仓位目标模型 (Position Target Model)
# ════════════════════════════════════════════════════════════


def _aggregate_bullish(
    buy_signals: np.ndarray,
    sell_signals: np.ndarray,
) -> np.ndarray:
    """聚合买卖信号为 portfolio bullish score。

    buy_signals:  (T, N) bool — 已确认的买入信号
    sell_signals: (T, N) bool — 已确认的卖出信号

    Returns:
        bullish_score: (T,) float64 in [0, 1]
        0 = 全卖出信号，1 = 全买入信号，0.5 = 中性（无信号或均衡）
    """
    T, N = buy_signals.shape
    bullish = np.full(T, 0.5, dtype=np.float64)

    for t in range(T):
        n_buy = int(buy_signals[t].sum())
        n_sell = int(sell_signals[t].sum())
        total = n_buy + n_sell
        if total > 0:
            bullish[t] = n_buy / total

    return bullish


def _sigmoid(x: float) -> float:
    """稳定版 sigmoid，防溢出。"""
    if x >= 0:
        return 1.0 / (1.0 + np.exp(-x))
    else:
        ex = np.exp(x)
        return ex / (1.0 + ex)


def _compute_position_target(
    bullish_scores: np.ndarray,
    slope: float,
    bias: float,
) -> np.ndarray:
    """sigmoid 映射 bullish_score → 目标仓位比例。

    Args:
        bullish_scores: (T,) float in [0, 1]
        slope: 敏感度，越大越激进（0.5~10.0）
        bias: 基准偏移，负=偏保守，正=偏激进（-3.0~3.0）

    Returns:
        target: (T,) float in [0, 1]
    """
    # 归一化: bullish -> [-1, 1] 区间
    centered = (bullish_scores - 0.5) * 2.0
    x = slope * centered + bias
    return np.array([_sigmoid(float(v)) for v in x], dtype=np.float64)


# ════════════════════════════════════════════════════════════
# FastEvaluator 主类
# ════════════════════════════════════════════════════════════


class FastEvaluator:
    """向量化快速评估器

    对一组策略（或单个策略）快速估算 Walk-Forward 窗口统计数据。
    """

    def __init__(
        self,
        initial_cash: float = 100000.0,
        monthly_buy_limit: float = 15000.0,
        lot_size: int = 100,  # A股默认100股/手，非A股改为1
        commission_rate: float = 0.002,
        buy_confirmation_days: int = 3,
        sell_confirmation_days: int = 1,
    ):
        self.initial_cash = initial_cash
        self.monthly_buy_limit = monthly_buy_limit
        self.lot_size = lot_size
        self.commission_rate = commission_rate
        self.buy_confirmation_days = buy_confirmation_days
        self.sell_confirmation_days = sell_confirmation_days

    def evaluate(
        self,
        indicator_matrix: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        buy_builders: list[str],
        buy_thresholds: list[float],
        buy_fracs: list[float],
        sell_builders: list[str] | None = None,
        sell_thresholds: list[float] | None = None,
        sell_fracs: list[float] | None = None,
        price_open_matrix: np.ndarray | None = None,
        benchmark_series: dict[str, np.ndarray] | None = None,
    ) -> "WindowStats":
        """评估单窗口单策略（支持买入+卖出）

        Args:
            indicator_matrix: (T, N, K) float32 指标矩阵
            price_matrix: (T, N) float32 收盘价矩阵
            cash_baseline: (T,) float64 现金基准线
            buy_builders: 买入规则构建器名列表
            buy_thresholds: 买入规则阈值列表
            buy_fracs: 买入规则比例列表
            sell_builders: 卖出规则构建器名列表（可选）
            sell_thresholds: 卖出规则阈值列表（可选）
            sell_fracs: 卖出规则比例列表（可选）
            price_open_matrix: (T, N) float32 开盘价矩阵（可选，用于均价执行）

        Returns:
            WindowStats 包含各项测试期统计指标
        """
        T, N = indicator_matrix.shape[:2]
        if N == 0 or T == 0:
            return WindowStats()

        # ── 1. 构建买入条件/重置矩阵 ──
        R_buy = len(buy_builders)
        buy_conditions = np.zeros((R_buy, T, N), dtype=bool)
        buy_resets = np.zeros((R_buy, T, N), dtype=bool)

        for r in range(R_buy):
            builder_fn = CONDITION_BUILDERS_FAST.get(
                buy_builders[r], _build_none,
            )
            cond, rst = builder_fn(indicator_matrix, buy_thresholds[r])
            buy_conditions[r] = cond
            buy_resets[r] = rst

        # ── 2. 构建卖出条件/重置矩阵 ──
        if sell_builders:
            R_sell = len(sell_builders)
            sell_conditions = np.zeros((R_sell, T, N), dtype=bool)
            sell_resets = np.zeros((R_sell, T, N), dtype=bool)
            for r in range(R_sell):
                builder_fn = CONDITION_BUILDERS_FAST.get(
                    sell_builders[r], _build_none,
                )
                cond, rst = builder_fn(indicator_matrix, sell_thresholds[r])
                sell_conditions[r] = cond
                sell_resets[r] = rst
        else:
            sell_conditions = np.zeros((1, T, N), dtype=bool)
            sell_resets = np.ones((1, T, N), dtype=bool)

        # ── 3. 锁/重置状态机 → 原始信号 ──
        if HAS_NUMBA:
            buy_signals_raw, _ = _apply_lock_reset_numba(buy_conditions, buy_resets)
            sell_signals_raw, _ = _apply_lock_reset_numba(sell_conditions, sell_resets)
        else:
            buy_signals_raw, _ = _apply_lock_reset(buy_conditions, buy_resets)
            sell_signals_raw, _ = _apply_lock_reset(sell_conditions, sell_resets)

        # ── 3b. 连续确认过滤 ──
        # 买入信号需要连续 N 日满足（用原始条件，不是锁后信号）
        # 卖出信号需要连续 M 日满足
        buy_cond_any = buy_conditions.any(axis=0)  # (T, N) 任一规则条件满足
        sell_cond_any = sell_conditions.any(axis=0)

        buy_signals = _apply_confirmation(
            buy_cond_any, self.buy_confirmation_days,
        )
        sell_signals = _apply_confirmation(
            sell_cond_any, self.sell_confirmation_days,
        )

        # ── 4. 组合模拟 → 日资产 ──
        buy_fracs_arr = np.array(buy_fracs, dtype=np.float32)
        sell_fracs_arr = np.array(sell_fracs if sell_fracs else [0.0], dtype=np.float32)

        if HAS_NUMBA:
            daily_values, trade_count, avg_pos_pct, final_pos_pct, final_shares, final_cash, cost_basis = _simulate_portfolio_numba(
                buy_signals, sell_signals, price_matrix,
                buy_fracs_arr, sell_fracs_arr,
                float(self.initial_cash), float(self.monthly_buy_limit),
                self.lot_size, float(self.commission_rate),
            )
        else:
            daily_values, trade_count, avg_pos_pct, final_pos_pct, final_shares, final_cash, cost_basis = _simulate_portfolio_python(
                buy_signals, sell_signals, price_matrix,
                buy_fracs_arr, sell_fracs_arr,
                self.initial_cash, self.monthly_buy_limit,
                self.lot_size, self.commission_rate,
                self.buy_confirmation_days, self.sell_confirmation_days,
                price_open_matrix,
            )

        # ── 5. 计算指标 ──
        signal_count = int(buy_signals.sum()) + int(sell_signals.sum())
        return self._compute_stats(
            daily_values, price_matrix, cash_baseline,
            trade_count, signal_count, avg_pos_pct=avg_pos_pct,
            benchmark_series=benchmark_series,
            final_pos_pct=final_pos_pct,
            final_shares=final_shares,
            final_cash=final_cash,
            cost_basis=cost_basis,
        )

    def _compute_stats(
        self,
        daily_values: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        trade_count: int,
        signal_count: int,
        avg_pos_pct: float | None = None,
        benchmark_series: dict[str, np.ndarray] | None = None,
        final_pos_pct: float = 0.0,
        final_shares: np.ndarray | None = None,
        final_cash: float = 0.0,
        cost_basis: np.ndarray | None = None,
    ) -> "WindowStats":
        """从日资产序列计算 WindowStats

        Args:
            daily_values: (T,) 每日净值
            avg_pos_pct: 仿真循环直接计算的仓位率（None=回退到旧启发式）
            benchmark_series: {"510300": (T,) close, "risk_free": (T,) nav, ...}
        """
        from .optimizer_constraints import WindowStats

        T = len(daily_values)
        if T < 2:
            return WindowStats()

        initial_val = daily_values[0]
        final_val = daily_values[-1]
        strategy_return = (final_val - initial_val) / initial_val * 100.0 if initial_val > 0 else 0.0

        # 基准收益
        benchmark_returns: dict[str, float] = {}
        if benchmark_series:
            # 使用提供的多基准序列
            for label, b_series in benchmark_series.items():
                if b_series is not None and len(b_series) > 1 and b_series[0] > 0:
                    bench_ret = (b_series[-1] - b_series[0]) / b_series[0] * 100.0
                    benchmark_returns[label] = round(bench_ret, 2)
            # 主超额 = vs 第一个基准
            if benchmark_returns:
                primary_label = next(iter(benchmark_returns))
                excess_return = strategy_return - benchmark_returns[primary_label]
            else:
                excess_return = strategy_return
        else:
            # 回退：用 cash_baseline 作为基准（旧行为兼容）
            bench_initial = cash_baseline[0]
            bench_final = cash_baseline[-1]
            bench_return = (bench_final - bench_initial) / bench_initial * 100.0 if bench_initial > 0 else 0.0
            excess_return = strategy_return - bench_return
            benchmark_returns = {"cash_baseline": round(bench_return, 2)}

        # 最大回撤
        peak = np.maximum.accumulate(daily_values)
        drawdown = (daily_values - peak) / peak * 100.0
        max_dd = float(np.min(drawdown))

        # 平均仓位率
        if avg_pos_pct is not None:
            avg_position_pct = avg_pos_pct
        else:
            # 回退：旧启发式（仅兼容，不应再走到这里）
            avg_val = float(np.mean(daily_values))
            avg_position_pct = max(0.0, (avg_val - self.initial_cash) / avg_val * 100.0)

        # 夏普比率
        sharpe = 0.0
        if T > 5:
            daily_rets = np.diff(daily_values) / daily_values[:-1]
            daily_rets = daily_rets[~np.isnan(daily_rets) & ~np.isinf(daily_rets)]
            if len(daily_rets) > 5:
                mean_ret = np.mean(daily_rets)
                std_ret = np.std(daily_rets, ddof=1)
                if std_ret > 1e-10:
                    sharpe = float(mean_ret / std_ret * np.sqrt(252))

        return WindowStats(
            test_excess_return=round(excess_return, 2),
            max_drawdown_pct=round(max_dd, 2),
            avg_position_pct=round(avg_position_pct, 2),
            sharpe_ratio=round(sharpe, 4),
            total_trades=trade_count,
            test_months=9,  # 窗口测试期月数，调用方应覆盖
            benchmark_returns=benchmark_returns,
            strategy_return=round(strategy_return, 2),
            final_position_pct=round(final_pos_pct, 2),
            final_shares=final_shares,
            final_cash=final_cash,
            cost_basis=cost_basis,
        )

    def evaluate_position_target(
        self,
        indicator_matrix: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        buy_builders: list[str],
        buy_thresholds: list[float],
        sell_builders: list[str] | None = None,
        sell_thresholds: list[float] | None = None,
        position_slope: float = 1.0,
        position_bias: float = 0.0,
        price_open_matrix: np.ndarray | None = None,
        benchmark_series: dict[str, np.ndarray] | None = None,
    ) -> "WindowStats":
        """仓位目标模式评估单窗口单策略。

        与 evaluate() 的区别: 交易量由 bullish_score → sigmoid 仓位目标驱动，
        而非固定的 buy_frac / sell_frac。

        Args:
            indicator_matrix: (T, N, K) float32 指标矩阵
            price_matrix: (T, N) float32 收盘价矩阵
            cash_baseline: (T,) float64 现金基准线
            buy_builders: 买入规则构建器名列表
            buy_thresholds: 买入规则阈值列表
            sell_builders: 卖出规则构建器名列表（可选）
            sell_thresholds: 卖出规则阈值列表（可选）
            position_slope: sigmoid 斜率（0.5~10.0）
            position_bias: sigmoid 偏移（-3.0~3.0）
            price_open_matrix: (T, N) float32 开盘价矩阵（可选，用于均价执行）

        Returns:
            WindowStats
        """
        T, N = indicator_matrix.shape[:2]
        if N == 0 or T == 0:
            return WindowStats()

        # ── 1. 构建买入条件/重置矩阵 ──
        R_buy = len(buy_builders)
        buy_conditions = np.zeros((R_buy, T, N), dtype=bool)
        buy_resets = np.zeros((R_buy, T, N), dtype=bool)
        for r in range(R_buy):
            builder_fn = CONDITION_BUILDERS_FAST.get(
                buy_builders[r], _build_none,
            )
            cond, rst = builder_fn(indicator_matrix, buy_thresholds[r])
            buy_conditions[r] = cond
            buy_resets[r] = rst

        # ── 2. 构建卖出条件/重置矩阵 ──
        if sell_builders and sell_thresholds:
            R_sell = len(sell_builders)
            sell_conditions = np.zeros((R_sell, T, N), dtype=bool)
            sell_resets = np.zeros((R_sell, T, N), dtype=bool)
            for r in range(R_sell):
                builder_fn = CONDITION_BUILDERS_FAST.get(
                    sell_builders[r], _build_none,
                )
                cond, rst = builder_fn(indicator_matrix, sell_thresholds[r])
                sell_conditions[r] = cond
                sell_resets[r] = rst
        else:
            R_sell = 1
            sell_conditions = np.zeros((1, T, N), dtype=bool)
            sell_resets = np.ones((1, T, N), dtype=bool)

        # ── 3. 锁/重置状态机 ──
        if HAS_NUMBA:
            buy_signals_raw, _ = _apply_lock_reset_numba(buy_conditions, buy_resets)
            sell_signals_raw, _ = _apply_lock_reset_numba(sell_conditions, sell_resets)
        else:
            buy_signals_raw, _ = _apply_lock_reset(buy_conditions, buy_resets)
            sell_signals_raw, _ = _apply_lock_reset(sell_conditions, sell_resets)

        # ── 4. 连续确认过滤 ──
        buy_cond_any = buy_conditions.any(axis=0)
        sell_cond_any = sell_conditions.any(axis=0)
        buy_signals = _apply_confirmation(buy_cond_any, self.buy_confirmation_days)
        sell_signals = _apply_confirmation(sell_cond_any, self.sell_confirmation_days)

        # ── 5. 仓位目标模拟 ──
        if price_open_matrix is None:
            price_open_matrix = price_matrix.copy()

        if HAS_NUMBA:
            daily_values, trade_count, avg_pos_pct, final_pos_pct, final_shares, final_cash, cost_basis = _simulate_position_target_numba(
                buy_signals, sell_signals,
                price_matrix, price_open_matrix,
                self.initial_cash, self.lot_size, self.commission_rate,
                position_slope, position_bias,
                max_daily_adjust=0.40,
                buy_confirm_days=self.buy_confirmation_days,
                sell_confirm_days=self.sell_confirmation_days,
            )
        else:
            daily_values, trade_count, avg_pos_pct, final_pos_pct, final_shares, final_cash, cost_basis = _simulate_position_target_python(
                buy_signals, sell_signals,
                price_matrix, price_open_matrix,
                self.initial_cash, self.lot_size, self.commission_rate,
                position_slope, position_bias,
                max_daily_adjust=0.40,
                buy_confirm_days=self.buy_confirmation_days,
                sell_confirm_days=self.sell_confirmation_days,
            )

        # ── 6. 计算统计 ──
        signal_count = int(buy_signals.sum()) + int(sell_signals.sum())
        return self._compute_stats(
            daily_values, price_matrix, cash_baseline,
            trade_count, signal_count, avg_pos_pct=avg_pos_pct,
            benchmark_series=benchmark_series,
            final_pos_pct=final_pos_pct,
            final_shares=final_shares,
            final_cash=final_cash,
            cost_basis=cost_basis,
        )

    def evaluate_multiple(
        self,
        indicator_matrix: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        strategies: list[tuple[list[str], list[float], list[float]]],
    ) -> list["WindowStats"]:
        """批量评估多个策略

        Args:
            strategies: [(builders, thresholds, fracs), ...]

        Returns:
            WindowStats 列表，顺序与输入一致
        """
        results = []
        for builders, thresholds, fracs in strategies:
            stats = self.evaluate(
                indicator_matrix, price_matrix, cash_baseline,
                builders, thresholds, fracs,
            )
            results.append(stats)
        return results


# ════════════════════════════════════════════════════════════
# numba 回退: 纯 Python 实现
# ════════════════════════════════════════════════════════════


def _simulate_portfolio_python(
    buy_signals,
    sell_signals,
    prices,
    buy_fracs,
    sell_fracs,
    initial_cash,
    monthly_limit,
    lot_size,
    commission_rate,
    buy_confirmation_days=3,
    sell_confirmation_days=1,
    price_open=None,
):
    """纯 Python 回退, 支持均价执行。

    信号已在上游 _apply_confirmation 做了连续确认过滤。
    本函数只负责执行：
    - 买入价 = 确认窗口内每日开盘+收盘的均值
    - 卖出价 = 当日开盘+收盘均值
    """
    T, N = buy_signals.shape
    shares = np.zeros(N, dtype=np.float64)
    cost_basis = np.zeros(N, dtype=np.float64)  # 加权平均买入价
    cash = float(initial_cash)
    daily_values = np.zeros(T, dtype=np.float64)
    monthly_spent = 0.0
    current_month = -1
    total_trades = 0

    avg_buy_frac = float(np.mean(buy_fracs)) if len(buy_fracs) > 0 else 0.15
    avg_sell_frac = float(np.mean(sell_fracs)) if len(sell_fracs) > 0 else 0.25

    if price_open is None:
        price_open = prices.copy()

    # 记录买入信号触发的窗口（用于算均价）
    # buy_window[n] = (start_day, end_day) 或 None
    buy_window: list[tuple | None] = [None] * N

    for t in range(T):
        month = t // 21
        if month != current_month:
            monthly_spent = 0.0
            current_month = month

        # 计算买入窗口（从 confirmation_days 往前数）
        for n in range(N):
            if buy_signals[t, n] and buy_window[n] is None:
                start = max(0, t - buy_confirmation_days + 1)
                buy_window[n] = (start, t)

        # ── 先卖后买 ──
        # 执行卖出
        for n in range(N):
            if not sell_signals[t, n] or shares[n] <= 0:
                continue
            p_open = float(price_open[t, n])
            p_close = float(prices[t, n])
            if p_open <= 0 or p_close <= 0 or np.isnan(p_open) or np.isnan(p_close):
                continue
            exec_price = (p_open + p_close) / 2.0

            sell_qty = shares[n] * avg_sell_frac
            sell_value = sell_qty * exec_price
            fee = sell_value * commission_rate
            cash += sell_value - fee
            shares[n] -= sell_qty
            total_trades += 1

        # 执行买入
        for n in range(N):
            if buy_window[n] is None or cash <= 0:
                continue
            start_day, end_day = buy_window[n]
            buy_window[n] = None  # 消费掉

            # 买入价 = 窗口内每日开盘+收盘的均值
            window_prices = []
            for d in range(start_day, end_day + 1):
                po = float(price_open[d, n])
                pc = float(prices[d, n])
                if po > 0 and not np.isnan(po):
                    window_prices.append(po)
                if pc > 0 and not np.isnan(pc):
                    window_prices.append(pc)
            if not window_prices:
                continue
            exec_price = np.mean(window_prices)

            buy_amount = cash * avg_buy_frac
            remaining = monthly_limit - monthly_spent
            buy_amount = min(buy_amount, remaining)

            if buy_amount <= 0:
                continue

            cost = buy_amount * (1.0 - commission_rate)
            qty = cost / exec_price
            cost_real = qty * exec_price
            fee = cost_real * commission_rate
            total_cost = cost_real + fee

            if total_cost <= cash:
                old_shares = shares[n]
                old_cost = cost_basis[n]
                shares[n] += qty
                if shares[n] > 0:
                    cost_basis[n] = (old_shares * old_cost + qty * exec_price) / shares[n]
                cash -= total_cost
                monthly_spent += total_cost
                total_trades += 1

        # 当日总资产
        pos_value = sum(
            shares[n] * float(prices[t, n])
            for n in range(N)
            if not np.isnan(prices[t, n]) and prices[t, n] > 0
        )
        daily_values[t] = cash + pos_value

    # 计算平均仓位率
    pos_pct_sum = 0.0
    valid = 0
    for t in range(T):
        nav = daily_values[t]
        if nav <= 0:
            continue
        pv = sum(
            shares[n] * float(prices[t, n])
            for n in range(N)
            if not np.isnan(prices[t, n]) and prices[t, n] > 0
        )
        pos_pct_sum += pv / nav
        valid += 1
    avg_pos_pct = (pos_pct_sum / valid * 100.0) if valid > 0 else 0.0

    # 期末仓位率
    final_nav = daily_values[-1] if T > 0 else initial_cash
    final_pos_value = sum(
        shares[n] * float(prices[-1, n])
        for n in range(N)
        if not np.isnan(prices[-1, n]) and prices[-1, n] > 0
    )
    final_pos_pct = (final_pos_value / final_nav * 100.0) if final_nav > 0 else 0.0

    return daily_values, total_trades, avg_pos_pct, final_pos_pct, shares.copy(), cash, cost_basis.copy()


def _simulate_position_target_python(
    buy_signals: np.ndarray,
    sell_signals: np.ndarray,
    prices_close: np.ndarray,
    prices_open: np.ndarray,
    initial_cash: float,
    lot_size: int,
    commission_rate: float,
    position_slope: float,
    position_bias: float,
    max_daily_adjust: float = 0.40,
    buy_confirm_days: int = 3,
    sell_confirm_days: int = 1,
):
    """仓位目标模型：每日渐进调仓。

    每日流程:
      1. 算 bullish_score → target_position
      2. delta = clamp(target - current_pct, -max_daily_adjust, max_daily_adjust)
      3. delta > 0: 买入（候选=有买入信号的股票，等额分配）
      4. delta < 0: 卖出（候选=有卖出信号的股票优先，按持仓比例）

    执行价格:
      买入: buy_confirm_days 窗口内的每日 (open+close)/2 均值
      卖出: 当日 (open+close)/2

    每标的每日最多 1 次操作。

    Returns:
        daily_values: (T,) float64 — 每日总资产
        total_trades: int
    """
    T, N = buy_signals.shape

    shares = np.zeros(N, dtype=np.float64)
    cost_basis = np.zeros(N, dtype=np.float64)  # 加权平均买入价
    cash = float(initial_cash)
    daily_values = np.zeros(T, dtype=np.float64)

    # 记录各类交易的消费
    # buy_window[n] = (start_day, end_day) 或 None
    buy_window: list[tuple | None] = [None] * N
    total_trades = 0

    for t in range(T):
        # ── 更新买入窗口（连续确认日记录） ──
        for n in range(N):
            if buy_signals[t, n] and buy_window[n] is None:
                start = max(0, t - buy_confirm_days + 1)
                buy_window[n] = (start, t)

        # ── 计算当前位置和目标仓位 ──
        position_value = 0.0
        for n in range(N):
            p = float(prices_close[t, n])
            if not np.isnan(p) and p > 0:
                position_value += shares[n] * p
        nav = cash + position_value
        current_pct = position_value / nav if nav > 0 else 0.0

        # 聚合信号 → 仓位目标
        bullish = _aggregate_bullish(
            buy_signals[t:t + 1], sell_signals[t:t + 1],
        )[0]
        target_pct = _compute_position_target(
            np.array([bullish]),
            position_slope, position_bias,
        )[0]

        # 每日调仓 delta
        delta = target_pct - current_pct
        delta = float(np.clip(delta, -max_daily_adjust, max_daily_adjust))

        # ── 执行调仓 ──
        if delta > 1e-6 and cash > 0:
            # 买入方向
            buy_cash = delta * nav
            buy_cash = min(buy_cash, cash)

            # 候选标的：当日有买入窗口的股票
            active_buyers = [
                n for n in range(N)
                if buy_window[n] is not None
            ]
            if active_buyers:
                per_stock_cash = buy_cash / len(active_buyers)
                for n in active_buyers:
                    if per_stock_cash <= 0 or buy_window[n] is None:
                        continue
                    start_day, end_day = buy_window[n]
                    buy_window[n] = None  # 消费掉

                    # 买入价 = 窗口内每日(开+收)/2 的均值
                    window_prices = []
                    for d in range(start_day, end_day + 1):
                        po = float(prices_open[d, n])
                        pc = float(prices_close[d, n])
                        if po > 0 and not np.isnan(po):
                            window_prices.append(po)
                        if pc > 0 and not np.isnan(pc):
                            window_prices.append(pc)
                    if not window_prices:
                        continue
                    exec_price = float(np.mean(window_prices))

                    cost = per_stock_cash * (1.0 - commission_rate)
                    qty_raw = cost / exec_price
                    if lot_size > 1:
                        qty = float(int(qty_raw / lot_size) * lot_size)
                    else:
                        qty = qty_raw
                    if qty <= 0:
                        continue
                    cost_real = qty * exec_price
                    fee = cost_real * commission_rate
                    total_cost = cost_real + fee

                    if total_cost <= cash:
                        old_sh = shares[n]
                        old_cb = cost_basis[n]
                        shares[n] += qty
                        if shares[n] > 0:
                            cost_basis[n] = (old_sh * old_cb + qty * exec_price) / shares[n]
                        cash -= total_cost
                        total_trades += 1

        elif delta < -1e-6:
            # 卖出方向
            sell_value_needed = abs(delta) * nav

            # 候选：有卖出信号的持仓股票优先，否则所有持仓按比例
            has_sell_signal = [
                n for n in range(N)
                if sell_signals[t, n] and shares[n] > 0
            ]
            all_holders = [
                n for n in range(N) if shares[n] > 0
            ]

            # 按持仓市值比例分配卖出金额
            total_position = sum(
                shares[n] * float(prices_close[t, n])
                for n in all_holders
                if not np.isnan(float(prices_close[t, n]))
                and float(prices_close[t, n]) > 0
            )
            if total_position > 0 and all_holders:
                for n in all_holders:
                    if sell_value_needed <= 0:
                        break
                    p_o = float(prices_open[t, n])
                    p_c = float(prices_close[t, n])
                    if p_o <= 0 or p_c <= 0 or np.isnan(p_o) or np.isnan(p_c):
                        continue
                    exec_price = (p_o + p_c) / 2.0

                    # 该标的持仓占比
                    pos_val_n = shares[n] * p_c
                    weight = pos_val_n / total_position if total_position > 0 else 0
                    sell_amount = sell_value_needed * weight
                    sell_amount = min(sell_amount, pos_val_n)  # 不超过持仓市值

                    sell_qty_raw = sell_amount / exec_price
                    if lot_size > 1:
                        sell_qty = float(
                            int(sell_qty_raw / lot_size) * lot_size
                        )
                    else:
                        sell_qty = sell_qty_raw
                    if sell_qty <= 0:
                        continue

                    sell_value = sell_qty * exec_price
                    fee = sell_value * commission_rate
                    cash += sell_value - fee
                    shares[n] -= sell_qty
                    sell_value_needed -= sell_value
                    total_trades += 1

        # ── 记录当日净值 ──
        position_value = 0.0
        for n in range(N):
            p = float(prices_close[t, n])
            if not np.isnan(p) and p > 0:
                position_value += shares[n] * p
        daily_values[t] = cash + position_value

    # 计算平均仓位率
    pos_pct_sum = 0.0
    valid = 0
    for t in range(T):
        nav = daily_values[t]
        if nav <= 0:
            continue
        pv = 0.0
        for n in range(N):
            p = float(prices_close[t, n])
            if not np.isnan(p) and p > 0:
                pv += shares[n] * p
        pos_pct_sum += pv / nav
        valid += 1
    avg_pos_pct = (pos_pct_sum / valid * 100.0) if valid > 0 else 0.0

    # 期末仓位率
    final_nav = daily_values[-1] if T > 0 else initial_cash
    final_pv = 0.0
    for n in range(N):
        p = float(prices_close[-1, n])
        if not np.isnan(p) and p > 0:
            final_pv += shares[n] * p
    final_pos_pct = (final_pv / final_nav * 100.0) if final_nav > 0 else 0.0

    return daily_values, total_trades, avg_pos_pct, final_pos_pct, shares.copy(), cash, cost_basis.copy()
