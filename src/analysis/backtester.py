"""统一回测引擎。

输入天级数据 → 调用策略生成信号 → 模拟交易 → 输出 WindowStats / PortfolioTrace。
所有搜参模式（percentile / builder / simplified）共用此引擎。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import WindowStats, get_constraints
from .search_interface import PortfolioTrace, Params, EvaluationReport, TradePlan
from .helpers import _detect_fine_group, RISK_FREE_A, RISK_FREE_NON_A

logger = logging.getLogger(__name__)

try:
    from numba import jit
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False
    def jit(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


# ═══════════════════════════════════════════════════════════════
# 指标列索引常量（与 walk_forward 对齐）
# ═══════════════════════════════════════════════════════════════

IDX_CLOSE = 0
IDX_MA60 = 1
IDX_DEVIATION = 2
IDX_RSI = 3
IDX_MACD = 4
IDX_MACD_SIGNAL = 5
IDX_MACD_HIST = 6
IDX_VOL_RATIO = 7
IDX_BOLL_PCT_B = 8
IDX_ADX = 9
IDX_ATR = 10
IDX_ADX_PCT = 11
IDX_RSI_PCT = 12
IDX_DEVIATION_PCT = 13
IDX_VOL_RATIO_PCT = 14
IDX_MA200_DEV_PCT = 15

INDICATOR_NAMES = [
    "close", "ma60", "deviation", "rsi", "macd", "macd_signal",
    "macd_hist", "vol_ratio", "boll_pct_b", "adx", "atr",
    "adx_pct", "rsi_pct", "deviation_pct", "vol_ratio_pct",
    "ma200_dev_pct",
]


# ═══════════════════════════════════════════════════════════════
# 公共分位评分（PercentileSignalFn 和 evaluate_percentile 共享）
# ═══════════════════════════════════════════════════════════════

def compute_percentile_scores(
    indicator_matrix: np.ndarray,
    pct_columns: list[int],
    pct_thresholds: list[float],
    weights: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    """分位评分计算。返回 (buy_scores, sell_scores) 各 (T, N) float32。"""
    T, N = indicator_matrix.shape[:2]
    buy_scores = np.zeros((T, N), dtype=np.float32)
    sell_scores = np.zeros((T, N), dtype=np.float32)
    total_w = 0.0
    for col, tau, w in zip(pct_columns, pct_thresholds, weights):
        if w <= 0:
            continue
        col_data = indicator_matrix[:, :, col]
        valid = ~np.isnan(col_data)
        buy_scores += w * (valid & (col_data > tau)).astype(np.float32)
        sell_scores += w * (valid & (col_data < tau)).astype(np.float32)
        total_w += w
    if total_w > 0:
        buy_scores /= total_w
        sell_scores /= total_w
    return buy_scores, sell_scores


# ═══════════════════════════════════════════════════════════════
# Numba JIT 内核
# ═══════════════════════════════════════════════════════════════

if HAS_NUMBA:

    @jit(nopython=True, parallel=False, cache=True)
    def _apply_lock_reset_numba(
        rule_conditions,  # (R, T, N) bool
        rule_resets,      # (R, T, N) float
    ):
        """锁/重置状态机。"""
        R, T, N = rule_conditions.shape
        triggered = np.zeros((T, N), dtype=np.bool_)
        locked = np.zeros((R, N), dtype=np.bool_)
        for t in range(T):
            for n in range(N):
                for r in range(R):
                    if rule_resets[r, t, n] > 0.5:
                        locked[r, n] = False
                    if rule_conditions[r, t, n] and not locked[r, n]:
                        triggered[t, n] = True
                        locked[r, n] = True
        return triggered, np.int32(0)

else:
    def _apply_lock_reset_numba(*args, **kwargs):
        raise NotImplementedError("numba required")


def _apply_lock_reset(rule_conditions, rule_resets):
    """Python 版本锁/重置状态机。"""
    R, T, N = rule_conditions.shape
    triggered = np.zeros((T, N), dtype=bool)
    locked = np.zeros((R, N), dtype=bool)
    trade_count = 0
    for t in range(T):
        for n in range(N):
            for r in range(R):
                if rule_resets[r, t, n] > 0.5:
                    locked[r, n] = False
                if rule_conditions[r, t, n] and not locked[r, n]:
                    triggered[t, n] = True
                    locked[r, n] = True
                    trade_count += 1
    return triggered, trade_count


def _apply_confirmation(conditions, confirmation_days):
    """连续确认过滤器。"""
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
                    streak[n] = 0
            else:
                streak[n] = 0
    return result


# ═══════════════════════════════════════════════════════════════
# 统一模拟引擎
# ═══════════════════════════════════════════════════════════════

if HAS_NUMBA:

    @jit(nopython=True, parallel=False, cache=True)
    def _simulate_portfolio_numba(
        buy_signals,
        sell_signals,
        prices,
        buy_fracs,
        sell_fracs,
        initial_cash,
        monthly_limit,
        lot_size,
        commission_rate,
        buy_limits=None,
        sell_limits=None,
        min_holding_days=30,
        buy_price_mode=1,
        lot_mode=1,
    ):
        """numba 加速版组合模拟 + FIFO 批次追踪 + 季度快照。

        buy_price_mode: 1=close, 2=3day_max
        lot_mode: 1=fifo, 2=simple（无持仓日限制）
        """
        T, N = buy_signals.shape
        MAX_LOTS = 5

        shares = np.zeros(N, dtype=np.float64)
        cost_basis = np.zeros(N, dtype=np.float64)
        cash = float(initial_cash)
        daily_values = np.zeros(T, dtype=np.float64)
        monthly_spent = 0.0
        current_month = -1
        total_trades = 0
        position_fraction_sum = 0.0
        position_fraction_days = 0

        buy_day = np.full((N, MAX_LOTS), -1, dtype=np.int32)
        lot_shares = np.zeros((N, MAX_LOTS), dtype=np.float64)
        lot_count = np.zeros(N, dtype=np.int32)

        N_QUARTERS = 4
        q_interval = max(1, T // N_QUARTERS)
        q_shares = np.zeros((N_QUARTERS, N), dtype=np.float64)
        q_cash = np.zeros(N_QUARTERS, dtype=np.float64)
        q_nav = np.zeros(N_QUARTERS, dtype=np.float64)
        q_prices = np.zeros((N_QUARTERS, N), dtype=np.float64)
        q_idx = 0

        use_limits = buy_limits is not None

        for t in range(T):
            month = t // 21
            if month != current_month:
                monthly_spent = 0.0
                current_month = month

            # ── 卖出 ──
            if sell_signals[t].any():
                for n in range(N):
                    if not sell_signals[t, n] or shares[n] <= 0:
                        continue
                    price = float(prices[t, n])
                    if price <= 0.0 or np.isnan(price):
                        continue

                    # 计算平均卖出比例
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

                    if lot_mode == 1:
                        # FIFO 找最早到期批次
                        k_sell = -1
                        for k in range(lot_count[n]):
                            if buy_day[n, k] < 0:
                                continue
                            if t - buy_day[n, k] >= min_holding_days:
                                k_sell = k
                                break
                        if k_sell < 0:
                            continue
                        if use_limits and sell_limits is not None:
                            max_amt = 0.0
                            for r in range(len(sell_limits)):
                                if sell_limits[r] > max_amt:
                                    max_amt = sell_limits[r]
                            sell_value = min(
                                max(max_amt, 5000.0),
                                lot_shares[n, k_sell] * price,
                            )
                        else:
                            sell_value = (
                                lot_shares[n, k_sell] * avg_frac * price
                            )
                        if sell_value <= 0:
                            continue
                        sell_qty = sell_value / price
                        sell_qty = min(sell_qty, lot_shares[n, k_sell])
                        fee = sell_value * commission_rate
                        cash += sell_value - fee
                        shares[n] -= sell_qty
                        lot_shares[n, k_sell] -= sell_qty
                        total_trades += 1
                        if lot_shares[n, k_sell] < 1e-10:
                            for j in range(k_sell, MAX_LOTS - 1):
                                buy_day[n, j] = buy_day[n, j + 1]
                                lot_shares[n, j] = lot_shares[n, j + 1]
                            buy_day[n, MAX_LOTS - 1] = -1
                            lot_shares[n, MAX_LOTS - 1] = 0.0
                            lot_count[n] -= 1
                    else:
                        # 简化模式：直接按比例卖
                        sell_qty = (
                            int(shares[n] * avg_frac / lot_size) * lot_size
                        )
                        if sell_qty > int(shares[n]):
                            sell_qty = int(shares[n])
                        if sell_qty <= 0:
                            continue
                        sell_value = sell_qty * price
                        fee = sell_value * commission_rate
                        cash += sell_value - fee
                        shares[n] -= sell_qty
                        total_trades += 1

            # ── 买入 ──
            if buy_signals[t].any() and cash > 0:
                for n in range(N):
                    if not buy_signals[t, n]:
                        continue
                    if lot_mode == 1 and lot_count[n] >= MAX_LOTS:
                        continue
                    if buy_price_mode == 2:
                        lo = max(0, t - 2)
                        pmax = 0.0
                        for tt in range(lo, t + 1):
                            pv = float(prices[tt, n])
                            if pv > 0.0 and not np.isnan(pv) and pv > pmax:
                                pmax = pv
                        price = pmax if pmax > 0.0 else float(prices[t, n])
                    else:
                        price = float(prices[t, n])
                    if price <= 0.0 or np.isnan(price):
                        continue

                    if use_limits and buy_limits is not None:
                        max_amt = 0.0
                        for r in range(len(buy_limits)):
                            if buy_limits[r] > max_amt:
                                max_amt = buy_limits[r]
                        buy_amount = min(max(max_amt, 5000.0), cash)
                    else:
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
                        buy_amount = min(
                            buy_amount, monthly_limit - monthly_spent
                        )
                    if buy_amount <= 0:
                        continue

                    cost = buy_amount * (1.0 - commission_rate)
                    qty = cost / price
                    if lot_mode == 2:
                        qty = int(qty / lot_size) * lot_size
                        if qty <= 0:
                            continue
                    cost_real = qty * price
                    fee = cost_real * commission_rate
                    total_cost = cost_real + fee

                    if total_cost <= cash:
                        old_sh = shares[n]
                        old_cb = cost_basis[n]
                        shares[n] += qty
                        if shares[n] > 0:
                            cost_basis[n] = (
                                old_sh * old_cb + qty * price
                            ) / shares[n]
                        if lot_mode == 1:
                            slot = lot_count[n]
                            buy_day[n, slot] = t
                            lot_shares[n, slot] = qty
                            lot_count[n] += 1
                        cash -= total_cost
                        monthly_spent += total_cost
                        total_trades += 1

            # 当日总资产
            pos_value = 0.0
            for n2 in range(N):
                p = float(prices[t, n2])
                if not np.isnan(p) and p > 0:
                    pos_value += shares[n2] * p
            daily_values[t] = cash + pos_value
            if daily_values[t] > 0.0:
                position_fraction_sum += pos_value / daily_values[t]
                position_fraction_days += 1

            # 季度快照
            if q_idx < N_QUARTERS and (t + 1) % q_interval == 0:
                for nq in range(N):
                    q_shares[q_idx, nq] = shares[nq]
                    p2 = float(prices[t, nq])
                    q_prices[q_idx, nq] = (
                        p2 if (not np.isnan(p2) and p2 > 0) else 0.0
                    )
                q_cash[q_idx] = cash
                q_nav[q_idx] = daily_values[t]
                q_idx += 1

        avg_pos_pct = 0.0
        if position_fraction_days > 0:
            avg_pos_pct = (
                position_fraction_sum / position_fraction_days * 100.0
            )

        final_pos_pct = 0.0
        if T > 0 and daily_values[T - 1] > 0:
            fpv = 0.0
            for n4 in range(N):
                px = float(prices[T - 1, n4])
                if not np.isnan(px) and px > 0:
                    fpv += shares[n4] * px
            final_pos_pct = fpv / daily_values[T - 1] * 100.0

        return (
            daily_values, total_trades, avg_pos_pct, final_pos_pct,
            shares.copy(), cash, cost_basis.copy(),
            q_shares, q_cash, q_nav, q_prices,
        )

else:
    def _simulate_portfolio_numba(*args, **kwargs):
        raise NotImplementedError("numba required")


# ``_simulate_portfolio_numba`` above is retained only for compatibility with
# historical imports.  All active paths use this cash-cap simulator.  It has
# no percentage-position, monthly-spend, or fixed-lot-batch branch.
if HAS_NUMBA:

    @jit(nopython=True, parallel=False, cache=True)
    def _simulate_cash_plan_numba(
        buy_signals,
        sell_signals,
        buy_priority,
        sell_priority,
        prices,
        tradable,
        initial_cash,
        buy_cash_limit,
        sell_cash_limit,
        lot_size,
        commission_rate,
        min_holding_days,
    ):
        T, N = buy_signals.shape
        shares = np.zeros(N, dtype=np.float64)
        cost_basis = np.zeros(N, dtype=np.float64)
        last_buy_day = np.full(N, -1000000, dtype=np.int32)
        cash = float(initial_cash)
        daily_values = np.zeros(T, dtype=np.float64)
        total_trades = 0
        position_fraction_sum = 0.0
        position_fraction_days = 0

        n_quarters = 4
        q_interval = max(1, T // n_quarters)
        q_shares = np.zeros((n_quarters, N), dtype=np.float64)
        q_cash = np.zeros(n_quarters, dtype=np.float64)
        q_nav = np.zeros(n_quarters, dtype=np.float64)
        q_prices = np.zeros((n_quarters, N), dtype=np.float64)
        q_idx = 0
        order = np.empty(N, dtype=np.int64)

        for t in range(T):
            # Sell first.  Sort candidates by strength descending; the input
            # columns are lexically sorted codes, giving a stable tie-breaker.
            count = 0
            for n in range(N):
                if sell_signals[t, n]:
                    order[count] = n
                    count += 1
            for i in range(1, count):
                candidate = order[i]
                candidate_score = sell_priority[t, candidate]
                j = i - 1
                while j >= 0 and sell_priority[t, order[j]] < candidate_score:
                    order[j + 1] = order[j]
                    j -= 1
                order[j + 1] = candidate
            for oi in range(count):
                n = order[oi]
                if shares[n] <= 0.0 or not tradable[t, n]:
                    continue
                if t - last_buy_day[n] < min_holding_days:
                    continue
                price = prices[t, n]
                if price <= 0.0 or np.isnan(price):
                    continue
                desired_value = min(sell_cash_limit, shares[n] * price)
                qty = int(desired_value / price / lot_size) * lot_size
                if qty <= 0 and shares[n] < lot_size:
                    qty = int(shares[n])
                if qty <= 0:
                    continue
                if qty > shares[n]:
                    qty = int(shares[n] / lot_size) * lot_size
                if qty <= 0:
                    continue
                value = qty * price
                fee = value * commission_rate
                cash += value - fee
                shares[n] -= qty
                total_trades += 1

            # Buy after sells with the same deterministic strength ordering.
            count = 0
            for n in range(N):
                if buy_signals[t, n]:
                    order[count] = n
                    count += 1
            for i in range(1, count):
                candidate = order[i]
                candidate_score = buy_priority[t, candidate]
                j = i - 1
                while j >= 0 and buy_priority[t, order[j]] < candidate_score:
                    order[j + 1] = order[j]
                    j -= 1
                order[j + 1] = candidate
            for oi in range(count):
                n = order[oi]
                if cash <= 0.0 or not tradable[t, n]:
                    continue
                price = prices[t, n]
                if price <= 0.0 or np.isnan(price):
                    continue
                gross_budget = min(buy_cash_limit, cash)
                qty = int(gross_budget / (price * (1.0 + commission_rate)) / lot_size)
                qty *= lot_size
                if qty <= 0:
                    continue
                value = qty * price
                fee = value * commission_rate
                total_cost = value + fee
                if total_cost > cash:
                    continue
                old_shares = shares[n]
                shares[n] += qty
                cost_basis[n] = (
                    (old_shares * cost_basis[n] + qty * price) / shares[n]
                )
                cash -= total_cost
                last_buy_day[n] = t
                total_trades += 1

            pos_value = 0.0
            for n in range(N):
                price = prices[t, n]
                if price > 0.0 and not np.isnan(price):
                    pos_value += shares[n] * price
            daily_values[t] = cash + pos_value
            if daily_values[t] > 0.0:
                position_fraction_sum += pos_value / daily_values[t]
                position_fraction_days += 1

            if q_idx < n_quarters and (t + 1) % q_interval == 0:
                for n in range(N):
                    q_shares[q_idx, n] = shares[n]
                    q_prices[q_idx, n] = prices[t, n]
                q_cash[q_idx] = cash
                q_nav[q_idx] = daily_values[t]
                q_idx += 1

        avg_pos_pct = 0.0
        if position_fraction_days > 0:
            avg_pos_pct = position_fraction_sum / position_fraction_days * 100.0
        final_pos_pct = 0.0
        if T > 0 and daily_values[T - 1] > 0.0:
            final_value = 0.0
            for n in range(N):
                final_value += shares[n] * prices[T - 1, n]
            final_pos_pct = final_value / daily_values[T - 1] * 100.0
        return (
            daily_values, total_trades, avg_pos_pct, final_pos_pct,
            shares.copy(), cash, cost_basis.copy(),
            q_shares, q_cash, q_nav, q_prices,
        )

else:
    def _simulate_cash_plan_numba(*args, **kwargs):
        raise NotImplementedError("numba required")


# ═══════════════════════════════════════════════════════════════
# FastEvaluator — 统一评估入口
# ═══════════════════════════════════════════════════════════════

class FastEvaluator:
    """向量化快速评估器 — 所有搜参模式共用。"""

    def __init__(
        self,
        exec_cfg,
        group: str = "a_share",
    ):
        self.initial_cash = exec_cfg.initial_capital
        self.lot_size = exec_cfg.lot_sizes.get(group, 100)
        self.commission_rate = exec_cfg.commission_rate
        self.min_holding_days = exec_cfg.min_holding_days
        self.buy_confirmation_days = 3
        self.sell_confirmation_days = 1

    def evaluate(
        self,
        indicator_matrix: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        buy_builders: list[str] | None = None,
        buy_thresholds: list[float] | None = None,
        buy_fracs: list[float] | None = None,
        sell_builders: list[str] | None = None,
        sell_thresholds: list[float] | None = None,
        sell_fracs: list[float] | None = None,
        buy_limits: list[float] | None = None,
        sell_limits: list[float] | None = None,
        buy_score_signals: np.ndarray | None = None,
        sell_score_signals: np.ndarray | None = None,
        price_open_matrix: np.ndarray | None = None,
        benchmark_series: dict[str, np.ndarray] | None = None,
        execution: dict[str, float | int] | None = None,
        trade_plan: TradePlan | None = None,
        buy_priority: np.ndarray | None = None,
        sell_priority: np.ndarray | None = None,
        tradable: np.ndarray | None = None,
    ) -> WindowStats:
        """统一评估入口（支持 builder + score 两种信号来源）。"""
        T, N = indicator_matrix.shape[:2]
        if N == 0 or T == 0:
            return WindowStats()

        use_score_signals = (
            buy_score_signals is not None or sell_score_signals is not None
        )
        if trade_plan is not None:
            buy_signals = np.asarray(trade_plan.buy_signals, dtype=bool)
            sell_signals = np.asarray(trade_plan.sell_signals, dtype=bool)
            buy_priority = np.asarray(trade_plan.buy_priority, dtype=np.float32)
            sell_priority = np.asarray(trade_plan.sell_priority, dtype=np.float32)
            execution = {
                "model": "cash_cap_v2",
                "buy_cash_limit": trade_plan.buy_cash_limit,
                "sell_cash_limit": trade_plan.sell_cash_limit,
            }
        elif use_score_signals:
            buy_signals = (
                buy_score_signals
                if buy_score_signals is not None
                else np.zeros((T, N), dtype=bool)
            )
            sell_signals = (
                sell_score_signals
                if sell_score_signals is not None
                else np.zeros((T, N), dtype=bool)
            )
        else:
            if not buy_builders:
                buy_signals = np.zeros((T, N), dtype=bool)
                sell_signals = np.zeros((T, N), dtype=bool)
            else:
                # 条件构建器 → boolean + lock/reset + confirmation
                from .strategies.builder.engine import CONDITION_BUILDERS_FAST
                R = len(buy_builders)
                buy_conds = np.zeros((R, T, N), dtype=bool)
                buy_resets = np.zeros((R, T, N), dtype=float)
                for r in range(R):
                    fn = CONDITION_BUILDERS_FAST.get(buy_builders[r])
                    if fn is None:
                        continue
                    th = (
                        buy_thresholds[r] if buy_thresholds and r < len(buy_thresholds)
                        else 0.5
                    )
                    c, rs = fn(indicator_matrix, th)
                    buy_conds[r] = c
                    buy_resets[r] = rs

                if HAS_NUMBA:
                    buy_raw, _ = _apply_lock_reset_numba(buy_conds, buy_resets)
                else:
                    buy_raw, _ = _apply_lock_reset(buy_conds, buy_resets)
                buy_signals = _apply_confirmation(
                    buy_conds.any(axis=0), self.buy_confirmation_days
                )

                S = len(sell_builders) if sell_builders else 0
                sell_conds = np.zeros((S, T, N), dtype=bool)
                sell_resets_arr = np.zeros((S, T, N), dtype=float)
                for r in range(S):
                    fn = CONDITION_BUILDERS_FAST.get(sell_builders[r])
                    if fn is None:
                        continue
                    th = (
                        sell_thresholds[r]
                        if sell_thresholds and r < len(sell_thresholds)
                        else 0.5
                    )
                    c, rs = fn(indicator_matrix, th)
                    sell_conds[r] = c
                    sell_resets_arr[r] = rs

                if HAS_NUMBA:
                    sell_raw, _ = _apply_lock_reset_numba(
                        sell_conds, sell_resets_arr
                    )
                else:
                    sell_raw, _ = _apply_lock_reset(
                        sell_conds, sell_resets_arr
                    )
                sell_signals = _apply_confirmation(
                    sell_conds.any(axis=0), self.sell_confirmation_days
                )

        # The per-trade cash cap is the only money gate.  Historical caller
        # arguments are translated to one cap for compatibility, never to a
        # percentage-position or monthly-spend rule.
        execution = execution or {}
        default_cap = min(float(self.initial_cash), 10000.0)
        legacy_buy_cap = max(buy_limits) if buy_limits else default_cap
        legacy_sell_cap = max(sell_limits) if sell_limits else default_cap
        if "position_frac" in execution and "buy_cash_limit" not in execution:
            # Compatibility for direct external callers only.  Artifacts and
            # registered strategies never emit this field after schema v2.
            legacy_buy_cap = float(self.initial_cash) * float(execution["position_frac"])
            legacy_sell_cap = legacy_buy_cap
        buy_cash_limit = float(execution.get("buy_cash_limit", legacy_buy_cap))
        sell_cash_limit = float(execution.get("sell_cash_limit", legacy_sell_cap))
        if buy_cash_limit <= 0.0 or sell_cash_limit <= 0.0:
            return WindowStats()
        if buy_priority is None:
            buy_priority = np.where(buy_signals, 1.0, -np.inf).astype(np.float32)
        if sell_priority is None:
            sell_priority = np.where(sell_signals, 1.0, -np.inf).astype(np.float32)
        if tradable is None:
            tradable = np.isfinite(price_matrix) & (price_matrix > 0)

        valuation_prices = np.asarray(price_matrix, dtype=np.float32).copy()
        for n in range(N):
            last = 0.0
            for t in range(T):
                if np.isfinite(valuation_prices[t, n]) and valuation_prices[t, n] > 0:
                    last = valuation_prices[t, n]
                elif last > 0.0:
                    valuation_prices[t, n] = last
                else:
                    valuation_prices[t, n] = 0.0

        if HAS_NUMBA:
            (
                daily_values, trade_count, avg_pos_pct, final_pos_pct,
                final_shares, final_cash, cost_basis,
                quarter_shares, quarter_cash, quarter_nav, quarter_prices,
            ) = _simulate_cash_plan_numba(
                np.ascontiguousarray(buy_signals, dtype=np.bool_),
                np.ascontiguousarray(sell_signals, dtype=np.bool_),
                np.ascontiguousarray(buy_priority, dtype=np.float32),
                np.ascontiguousarray(sell_priority, dtype=np.float32),
                np.ascontiguousarray(valuation_prices, dtype=np.float32),
                np.ascontiguousarray(tradable, dtype=np.bool_),
                float(self.initial_cash), buy_cash_limit, sell_cash_limit,
                int(self.lot_size), float(self.commission_rate),
                int(self.min_holding_days),
            )
        else:
            return WindowStats()

        return _compute_stats(
            daily_values, valuation_prices, cash_baseline,
            trade_count, 0, avg_pos_pct=avg_pos_pct,
            benchmark_series=benchmark_series,
            final_pos_pct=final_pos_pct, final_shares=final_shares,
            final_cash=final_cash, cost_basis=cost_basis,
            quarter_shares=quarter_shares, quarter_cash=quarter_cash,
            quarter_nav=quarter_nav, quarter_prices=quarter_prices,
        )


def _compute_stats(
    daily_values,
    price_matrix,
    cash_baseline,
    trade_count,
    signal_count,
    avg_pos_pct=None,
    benchmark_series=None,
    final_pos_pct=None,
    final_shares=None,
    final_cash=None,
    cost_basis=None,
    quarter_shares=None,
    quarter_cash=None,
    quarter_nav=None,
    quarter_prices=None,
) -> WindowStats:
    """daily_values → WindowStats。"""
    T = len(daily_values)

    nav = daily_values
    total_return = float((nav[-1] - nav[0]) / nav[0] * 100) if nav[0] > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_series = np.where(peak > 0, (nav - peak) / peak * 100.0, 0.0)
    dd_series = dd_series[np.isfinite(dd_series)]
    max_dd = float(np.min(dd_series)) if len(dd_series) > 0 else 0.0

    sharpe = 0.0
    if T > 5:
        rets = np.diff(nav) / nav[:-1]
        rets = rets[~np.isnan(rets) & ~np.isinf(rets)]
        if len(rets) > 5 and np.std(rets, ddof=1) > 1e-10:
            sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(252))

    benchmark_returns = {}
    excess_return = total_return
    if benchmark_series:
        for lbl, bs in benchmark_series.items():
            if bs is not None and len(bs) > 1 and bs[0] > 0:
                br = (bs[-1] - bs[0]) / bs[0] * 100
                benchmark_returns[lbl] = round(br, 2)
        if benchmark_returns:
            primary = next(iter(benchmark_returns))
            excess_return = total_return - benchmark_returns[primary]
    elif (
        cash_baseline is not None
        and len(cash_baseline) > 1
        and cash_baseline[0] > 0
    ):
        cash_return = (cash_baseline[-1] - cash_baseline[0]) / cash_baseline[0] * 100
        benchmark_returns["risk_free"] = round(float(cash_return), 2)
        excess_return = total_return - cash_return

    if avg_pos_pct is None:
        avg_pos_pct = 0.0

    return WindowStats(
        test_excess_return=round(excess_return, 2),
        max_drawdown_pct=round(max_dd, 2),
        avg_position_pct=round(avg_pos_pct, 2),
        sharpe_ratio=round(sharpe, 4),
        total_trades=trade_count,
        benchmark_returns=benchmark_returns,
        strategy_return=round(total_return, 2),
        final_position_pct=round(final_pos_pct or 0.0, 2),
        final_shares=final_shares,
        final_cash=final_cash or 0.0,
        cost_basis=cost_basis,
        quarter_shares=quarter_shares,
        quarter_cash=quarter_cash,
        quarter_nav=quarter_nav,
        quarter_prices=quarter_prices,
    )


# ═══════════════════════════════════════════════════════════════
# simulate_portfolio — 分数 → 统一引擎 → PortfolioTrace
# ═══════════════════════════════════════════════════════════════

def simulate_portfolio(
    buy_scores: np.ndarray,
    sell_scores: np.ndarray,
    price: np.ndarray,
    initial_cash: float,
    buy_threshold: float,
    sell_threshold: float,
    position_frac: float,
    lot_size: int,
    monthly_limit: float,
    commission_rate: float,
    dates: list[str],
    stock_codes: list[str],
    quarterly_interval: int = 63,
    buy_cash_limit: float | None = None,
    sell_cash_limit: float | None = None,
    buy_priority: np.ndarray | None = None,
    sell_priority: np.ndarray | None = None,
    tradable: np.ndarray | None = None,
    min_holding_days: int = 0,
) -> PortfolioTrace:
    """评分矩阵 → 仿真轨迹（统一引擎）。"""
    T, N = buy_scores.shape

    # 阈值 → boolean
    buy_signals = buy_scores > buy_threshold
    sell_signals = sell_scores > sell_threshold
    conflict = buy_signals & sell_signals
    buy_signals[conflict] = False
    sell_signals[conflict] = False

    # ``position_frac`` and ``monthly_limit`` are retained in the public
    # signature only so third-party historical callers do not crash.  New
    # paths must pass cash caps; legacy calls are translated once at the edge.
    if buy_cash_limit is None:
        buy_cash_limit = float(initial_cash) * float(position_frac or 0.10)
    if sell_cash_limit is None:
        sell_cash_limit = float(initial_cash) * float(position_frac or 0.10)
    if buy_priority is None:
        buy_priority = np.where(buy_signals, buy_scores, -np.inf)
    if sell_priority is None:
        sell_priority = np.where(sell_signals, sell_scores, -np.inf)
    if tradable is None:
        tradable = np.isfinite(price) & (price > 0)
    valuation_price = np.asarray(price, dtype=np.float32).copy()
    for n in range(N):
        last = 0.0
        for t in range(T):
            if np.isfinite(valuation_price[t, n]) and valuation_price[t, n] > 0:
                last = valuation_price[t, n]
            elif last > 0:
                valuation_price[t, n] = last
            else:
                valuation_price[t, n] = 0.0

    if HAS_NUMBA:
        (
            daily_values, trade_count, avg_pos_pct, final_pos_pct,
            final_shares, final_cash, cost_basis,
            q_shares, q_cash, q_nav, q_prices,
        ) = _simulate_cash_plan_numba(
            np.ascontiguousarray(buy_signals, dtype=np.bool_),
            np.ascontiguousarray(sell_signals, dtype=np.bool_),
            np.ascontiguousarray(buy_priority, dtype=np.float32),
            np.ascontiguousarray(sell_priority, dtype=np.float32),
            np.ascontiguousarray(valuation_price, dtype=np.float32),
            np.ascontiguousarray(tradable, dtype=np.bool_),
            float(initial_cash), float(buy_cash_limit), float(sell_cash_limit),
            int(lot_size), float(commission_rate), int(min_holding_days),
        )
    else:
        return PortfolioTrace(
            daily_values=np.zeros(T),
            daily_dates=dates, total_trades=0,
            avg_position_pct=0.0, max_drawdown_pct=0.0,
            sharpe_ratio=0.0, total_return_pct=0.0,
            final_position_pct=0.0, quarterly_holdings=[],
            composition=stock_codes,
        )

    # 季度持仓
    n_q = q_shares.shape[0]
    quarterly_holdings = []
    for qi in range(n_q):
        qpos = []
        for i, code in enumerate(stock_codes):
            sh = q_shares[qi, i]
            if sh > 0.5:
                px = float(q_prices[qi, i])
                cb_i = float(cost_basis[i])
                qpos.append({
                    "code": code, "shares": round(float(sh), 1),
                    "cost": round(cb_i, 2), "price": round(px, 2),
                    "value": round(sh * px, 2),
                    "pnl": round(sh * px - cb_i * sh, 2),
                    "pnl_pct": round((px / max(cb_i, 0.0001) - 1) * 100, 1),
                })
        qp = (
            round(float(q_shares[qi].dot(q_prices[qi])) / max(q_nav[qi], 1.0) * 100, 1)
            if q_nav[qi] > 0 else 0.0
        )
        quarterly_holdings.append({
            "quarter": qi + 1, "day": qi * quarterly_interval,
            "cash": round(float(q_cash[qi]), 2),
            "nav": round(float(q_nav[qi]), 2),
            "pos_pct": qp, "positions": qpos,
        })

    nav = daily_values
    total_return = float((nav[-1] - nav[0]) / nav[0] * 100) if nav[0] > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_series = np.where(peak > 0, (nav - peak) / peak * 100.0, 0.0)
    dd = float(np.min(dd_series[np.isfinite(dd_series)]))
    sharpe = 0.0
    if len(nav) > 5:
        rets = np.diff(nav) / nav[:-1]
        rets = rets[~np.isnan(rets) & ~np.isinf(rets)]
        if len(rets) > 5 and np.std(rets, ddof=1) > 1e-10:
            sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(252))
    final_pos_pct_calc = (
        float(np.dot(final_shares, valuation_price[-1]) / max(nav[-1], 1.0) * 100)
        if nav[-1] > 0 else 0.0
    )

    return PortfolioTrace(
        daily_values=nav, daily_dates=dates,
        total_trades=trade_count,
        avg_position_pct=round(avg_pos_pct, 2),
        max_drawdown_pct=round(dd, 2),
        sharpe_ratio=round(sharpe, 4),
        total_return_pct=round(total_return, 2),
        final_position_pct=round(final_pos_pct_calc, 2),
        quarterly_holdings=quarterly_holdings,
        composition=stock_codes,
        nav_series=[round(float(v), 2) for v in nav],
        nav_dates=dates, cost_basis=cost_basis,
        final_shares=final_shares, final_cash=float(final_cash),
    )


# ═══════════════════════════════════════════════════════════════
# Walk-Forward 窗口管理
# ═══════════════════════════════════════════════════════════════

@dataclass
class WindowSlice:
    """单个 WF 窗口索引。"""
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    window_index: int


class WalkForwardManager:
    """Walk-Forward 窗口管理器 + 指标矩阵构建。"""

    def __init__(
        self,
        stocks_data: dict[str, pd.DataFrame],
        config,
        stock_codes: list[str],
        benchmark_data: dict[str, pd.DataFrame] | None = None,
    ):
        from ..data.technical_indicators import compute_all
        from .config import WalkForwardConfig

        self.stock_codes = sorted(dict.fromkeys(stock_codes))
        self.wf_config: WalkForwardConfig = config.walk_forward
        self.benchmark_data = benchmark_data or {}

        computed = compute_all(stocks_data)
        self._build_unified_data(computed, self.stock_codes)

    def _build_unified_data(
        self, computed: dict[str, pd.DataFrame], stock_codes: list[str]
    ):
        dates_sets = []
        for code in stock_codes:
            df = computed.get(code)
            if df is not None and not df.empty:
                if 'date' in df.columns:
                    dates_sets.append(set(pd.to_datetime(df['date'])))
                else:
                    dates_sets.append(set(df.index))
        if not dates_sets:
            self.T = 0
            return
        common = sorted(dates_sets[0].intersection(*dates_sets[1:]))
        if not common:
            self.T = 0
            return

        self.dates = pd.DatetimeIndex(common)
        self.T = len(self.dates)
        N = len(stock_codes)

        self.indicator_matrix = np.full(
            (self.T, N, len(INDICATOR_NAMES)), np.nan, dtype=np.float32
        )
        self.price_matrix = np.full((self.T, N), np.nan, dtype=np.float32)
        self.price_open_matrix = np.full((self.T, N), np.nan, dtype=np.float32)

        for i, code in enumerate(stock_codes):
            df = computed.get(code)
            if df is None or df.empty:
                continue
            if 'date' in df.columns:
                df = df.copy()
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            aligned = df.reindex(self.dates)
            for k, name in enumerate(INDICATOR_NAMES):
                if name in aligned.columns:
                    self.indicator_matrix[:, i, k] = aligned[name].values
            if "close" in aligned.columns:
                self.price_matrix[:, i] = aligned["close"].values
            if "open" in aligned.columns:
                self.price_open_matrix[:, i] = aligned["open"].values

        self.benchmark_series: dict[str, np.ndarray] = {}
        for code, raw_df in self.benchmark_data.items():
            if raw_df is None or raw_df.empty or "close" not in raw_df.columns:
                continue
            benchmark = raw_df.copy()
            if "date" in benchmark.columns:
                benchmark["date"] = pd.to_datetime(benchmark["date"])
                benchmark = benchmark.set_index("date")
            else:
                benchmark.index = pd.to_datetime(benchmark.index)
            series = benchmark["close"].reindex(self.dates).ffill()
            values = series.to_numpy(dtype=np.float64)
            if np.isfinite(values).any():
                self.benchmark_series[str(code)] = values

    def build_matrices(self):
        return self.indicator_matrix, self.price_matrix, self.price_open_matrix

    def iter_windows(self) -> list[WindowSlice]:
        """Build complete windows backwards from the newest available data.

        The newest window is the daily-report holdout, so it must end at the
        newest market date.  Indicator lookback buffers may make ``self.T``
        longer than the configured Walk-Forward horizon; anchoring at index
        zero would otherwise leave the holdout months behind the current
        daily-report validation period.
        """
        train_days = self.wf_config.train_months * 21
        test_days = self.wf_config.test_months * 21
        step_days = self.wf_config.step_months * 21
        latest_test_start = self.T - test_days
        first_test_start = latest_test_start - (
            self.wf_config.num_windows - 1
        ) * step_days
        if first_test_start < train_days:
            return []

        windows = []
        for wi in range(self.wf_config.num_windows):
            test_start = first_test_start + wi * step_days
            test_end = test_start + test_days
            windows.append(WindowSlice(
                train_start=test_start - train_days,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
                window_index=wi,
            ))
        return windows


# ═══════════════════════════════════════════════════════════════
# 日报评估引擎：make_signals → simulate_portfolio → EvaluationReport
# ═══════════════════════════════════════════════════════════════

def _build_indicator_matrix(
    computed: dict[str, pd.DataFrame],
    stock_codes: list[str],
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """从 compute_all() 结果构建 (T,N,K) 指标矩阵 + 价格矩阵 + 公共日期。

    兼容 date 作为列名或 index 两种格式。

    Returns:
        (indicator_matrix, price_matrix, date_strings)
    """
    dates_sets = []
    for code in stock_codes:
        df = computed.get(code)
        if df is None or df.empty:
            continue
        # 优先用 date 列，回退到 index
        if "date" in df.columns:
            date_vals = pd.to_datetime(df["date"]).tolist()
        else:
            date_vals = df.index.tolist()
        dates_sets.append(set(date_vals))

    if not dates_sets:
        return (
            np.zeros((0, 0, len(INDICATOR_NAMES)), dtype=np.float32),
            np.zeros((0, 0), dtype=np.float32),
            [],
            np.zeros((0, 0), dtype=bool),
        )
    # Daily reports use the current interactive universe.  A short-history
    # addition must not erase years of data for every existing symbol.
    all_dates = sorted(set().union(*dates_sets))
    if not all_dates:
        return (
            np.zeros((0, 0, len(INDICATOR_NAMES)), dtype=np.float32),
            np.zeros((0, 0), dtype=np.float32),
            [],
            np.zeros((0, 0), dtype=bool),
        )

    T = len(all_dates)
    N = len(stock_codes)
    dates = pd.DatetimeIndex(all_dates)
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]

    ind_mat = np.full((T, N, len(INDICATOR_NAMES)), np.nan, dtype=np.float32)
    price_mat = np.full((T, N), np.nan, dtype=np.float32)
    tradable = np.zeros((T, N), dtype=bool)

    for i, code in enumerate(stock_codes):
        df = computed.get(code)
        if df is None or df.empty:
            continue
        # 统一用 date 列做 index 再 reindex 到公共日期
        if "date" in df.columns:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        aligned = df.reindex(dates)
        for k, name in enumerate(INDICATOR_NAMES):
            if name in aligned.columns:
                ind_mat[:, i, k] = aligned[name].values.astype(np.float32)
        if "close" in aligned.columns:
            price_mat[:, i] = aligned["close"].values.astype(np.float32)
            tradable[:, i] = np.isfinite(price_mat[:, i]) & (price_mat[:, i] > 0)

    return ind_mat, price_mat, date_strs, tradable


def _evaluate_signal_fn(
    stocks_data: dict[str, pd.DataFrame],
    active_codes: list[str],
    strategy,  # SearchStrategy
    params: Params,
    initial_capital: float,
    monthly_limit: float,
    commission_rate: float,
    lot_size: int,
    fx_rate: float = 1.0,
    start_date: str | None = None,
    end_date: str | None = None,
    min_holding_days: int = 30,
) -> tuple:
    """Use the strategy's canonical ``TradePlan`` for one market basket.

    Returns:
        (trace: PortfolioTrace, dates: list[str], codes: list[str])
    """
    from src.data.technical_indicators import compute_all

    # Lexical order is the stable final tie-break after strategy priority.
    active_codes = sorted(dict.fromkeys(active_codes))

    # 1. 补齐技术指标
    try:
        computed = compute_all({c: stocks_data[c] for c in active_codes})
    except Exception as e:
        logger.warning(f"指标计算失败，仅用兜底列: {e}")
        computed = {}

    # 2. 构建统一指标矩阵
    ind_mat, price, dates, tradable = _build_indicator_matrix(computed, active_codes)
    T, N = ind_mat.shape[:2]
    if T == 0 or N == 0:
        return (
            PortfolioTrace(
                daily_values=np.zeros(1),
                daily_dates=dates,
                total_trades=0,
                avg_position_pct=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                total_return_pct=0.0,
                final_position_pct=0.0,
                quarterly_holdings=[],
                composition=list(active_codes),
            ),
            dates,
            list(active_codes),
        )

    # 3. The same plan is used in the optimizer and live scan.
    trade_plan = strategy.make_trade_plan(params, ind_mat)
    price = price * fx_rate

    # 5. 仿真 (threshold=0.5 对 bool→float 天然等价)
    if start_date is not None or end_date is not None:
        date_index = pd.DatetimeIndex(pd.to_datetime(dates))
        date_mask = np.ones(len(date_index), dtype=bool)
        if start_date is not None:
            date_mask &= date_index >= pd.Timestamp(start_date)
        if end_date is not None:
            date_mask &= date_index <= pd.Timestamp(end_date)
        trade_plan = TradePlan(
            buy_signals=trade_plan.buy_signals[date_mask],
            sell_signals=trade_plan.sell_signals[date_mask],
            buy_priority=trade_plan.buy_priority[date_mask],
            sell_priority=trade_plan.sell_priority[date_mask],
            buy_cash_limit=trade_plan.buy_cash_limit,
            sell_cash_limit=trade_plan.sell_cash_limit,
            warmup_rows=trade_plan.warmup_rows,
        )
        price = price[date_mask]
        tradable = tradable[date_mask]
        dates = [date for date, keep in zip(dates, date_mask) if keep]

    if not dates:
        return (
            PortfolioTrace(
                daily_values=np.zeros(1),
                daily_dates=[],
                total_trades=0,
                avg_position_pct=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                total_return_pct=0.0,
                final_position_pct=0.0,
                quarterly_holdings=[],
                composition=list(active_codes),
            ),
            [],
            list(active_codes),
        )

    trace = simulate_portfolio(
        trade_plan.buy_signals.astype(float),
        trade_plan.sell_signals.astype(float),
        price,
        float(initial_capital),
        buy_threshold=0.5,
        sell_threshold=0.5,
        position_frac=None,
        lot_size=lot_size,
        monthly_limit=float(monthly_limit),  # compatibility; intentionally ignored
        commission_rate=float(commission_rate),
        dates=dates,
        stock_codes=list(active_codes),
        buy_cash_limit=trade_plan.buy_cash_limit,
        sell_cash_limit=trade_plan.sell_cash_limit,
        buy_priority=trade_plan.buy_priority,
        sell_priority=trade_plan.sell_priority,
        tradable=tradable,
        min_holding_days=int(min_holding_days),
    )
    return trace, dates, list(active_codes)


def _aligned_benchmark_values(
    nav_dates: list[str],
    nav_series: list[float],
    benchmark_df: pd.DataFrame | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Align strategy NAV and a benchmark without looking ahead.

    Markets do not share every holiday.  A backward as-of match uses the last
    close that would have been known on each strategy trading date; it never
    borrows a future benchmark price.  Returning paired arrays also makes the
    return and win-rate calculations use exactly the same observations.
    """
    if (
        benchmark_df is None
        or benchmark_df.empty
        or len(nav_dates) != len(nav_series)
        or "close" not in benchmark_df.columns
    ):
        return np.array([], dtype=float), np.array([], dtype=float)

    try:
        strategy = pd.DataFrame(
            {
                "date": pd.to_datetime(nav_dates, errors="coerce"),
                "nav": pd.to_numeric(nav_series, errors="coerce"),
            }
        ).dropna()
        if strategy.empty:
            return np.array([], dtype=float), np.array([], dtype=float)

        benchmark = benchmark_df.copy()
        if "date" in benchmark.columns:
            benchmark_dates = pd.to_datetime(benchmark["date"], errors="coerce")
        else:
            benchmark_dates = pd.to_datetime(benchmark.index, errors="coerce")
        benchmark = pd.DataFrame(
            {
                "date": benchmark_dates,
                "benchmark": pd.to_numeric(benchmark["close"], errors="coerce"),
            }
        ).dropna()
        benchmark = benchmark[benchmark["benchmark"] > 0]
        benchmark = benchmark.drop_duplicates("date", keep="last").sort_values("date")
        if benchmark.empty:
            return np.array([], dtype=float), np.array([], dtype=float)

        aligned = pd.merge_asof(
            strategy.sort_values("date"), benchmark, on="date", direction="backward"
        ).dropna()
        aligned = aligned[(aligned["nav"] > 0) & (aligned["benchmark"] > 0)]
        return (
            aligned["nav"].to_numpy(dtype=float),
            aligned["benchmark"].to_numpy(dtype=float),
        )
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning("Unable to align benchmark data: %s", exc)
        return np.array([], dtype=float), np.array([], dtype=float)


def _benchmark_return_and_win_rate(
    strategy_values: np.ndarray,
    benchmark_values: np.ndarray,
    validation_days: int = 9 * 21,
) -> tuple[float | None, float | None]:
    """Return full-period benchmark return and recent validation win rate.

    The win rate means: from every eligible validation-day entry point, did
    holding the strategy until the final date beat holding the same benchmark?
    This is intentionally distinct from the benchmark's cumulative return.
    """
    if len(strategy_values) < 2 or len(strategy_values) != len(benchmark_values):
        return None, None

    benchmark_return = (benchmark_values[-1] / benchmark_values[0] - 1) * 100
    cut = max(0, len(strategy_values) - validation_days)
    strategy_validation = strategy_values[cut:]
    benchmark_validation = benchmark_values[cut:]
    if len(strategy_validation) < 2:
        return round(float(benchmark_return), 2), None

    strategy_forward = strategy_validation[-1] / strategy_validation[:-1] - 1
    benchmark_forward = benchmark_validation[-1] / benchmark_validation[:-1] - 1
    valid = np.isfinite(strategy_forward) & np.isfinite(benchmark_forward)
    if not valid.any():
        return round(float(benchmark_return), 2), None
    win_rate = float(np.mean(strategy_forward[valid] > benchmark_forward[valid]) * 100)
    return round(float(benchmark_return), 2), round(win_rate, 2)


def _risk_free_return_and_win_rate(
    nav_series: list[float],
    annual_rate: float,
    validation_days: int = 9 * 21,
) -> tuple[float, float | None]:
    """Calculate the cash baseline return and its forward-holding win rate."""
    values = np.asarray(nav_series, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if len(values) < 2:
        return 0.0, None

    daily_rate = (1 + annual_rate) ** (1 / 252) - 1
    cash_return = ((1 + daily_rate) ** (len(values) - 1) - 1) * 100
    recent = values[max(0, len(values) - validation_days) :]
    if len(recent) < 2:
        return round(float(cash_return), 2), None

    offsets = np.arange(len(recent) - 1, 0, -1)
    strategy_forward = recent[-1] / recent[:-1] - 1
    cash_forward = (1 + daily_rate) ** offsets - 1
    valid = np.isfinite(strategy_forward)
    if not valid.any():
        return round(float(cash_return), 2), None
    win_rate = float(np.mean(strategy_forward[valid] > cash_forward[valid]) * 100)
    return round(float(cash_return), 2), round(win_rate, 2)


def evaluate_all_groups(
    stocks_data: dict[str, pd.DataFrame],
    stock_codes: list[str],
    strategy,  # SearchStrategy
    params: Params,
    exec_cfg,
    benchmark_data: dict[str, pd.DataFrame] | None = None,
    target_groups: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, EvaluationReport]:
    """日报/IM 的唯一评估入口。

    读 stock_codes → 按 fine_group 分组 → make_signals → simulate_portfolio
    → EvaluationReport。只调 ABC 方法，零策略硬编码。

    Args:
        stocks_data: {code: DataFrame} 已拉取的历史数据
        stock_codes: 全量标的代码列表
        strategy: SearchStrategy 实例（percentile/builder/simplified）
        params: 策略参数
        exec_cfg: get_execution_config() 返回值
        benchmark_data: 基准价格数据
        target_groups: 分组子集，None=全部

    Returns:
        {group_key: EvaluationReport}
    """
    from datetime import datetime

    target_groups = target_groups or ["a_share", "hk", "us"]
    fx_map = exec_cfg.fx_rates
    lot_map = exec_cfg.lot_sizes

    # 按 fine_group 分组
    group_data_map: dict[str, dict[str, pd.DataFrame]] = {g: {} for g in target_groups}
    for code in stock_codes:
        code_str = str(code)
        if code_str not in stocks_data:
            continue
        df = stocks_data[code_str]
        if df is None or df.empty or "close" not in df.columns:
            continue
        # A newly interactive-selected symbol stays visible as "warming";
        # its strategy plan prevents orders until the declared warmup is met.
        if len(df) < 1:
            continue
        group = _detect_fine_group(code_str)
        if group not in target_groups:
            continue
        group_data_map[group][code_str] = df

    engine_name = getattr(params, "_engine", "") or getattr(strategy, "name", "percentile")
    engine_names = {
        "percentile": "分位评分",
        "builder": "条件构建",
        "simplified": "固定限额",
    }
    timestamp = datetime.now().isoformat()

    results: dict[str, EvaluationReport] = {}
    for group_name in target_groups:
        group_data = group_data_map.get(group_name, {})
        active_codes = list(group_data.keys())
        if not active_codes:
            continue

        fx = fx_map.get(group_name, 1.0)
        lot = lot_map.get(group_name, 100)
        initial_capital = float(exec_cfg.initial_capital)
        monthly_limit = 0.0  # retained call shape; cash tiers are the only cap

        # 回测
        trace, dates, codes = _evaluate_signal_fn(
            group_data, active_codes, strategy, params,
            initial_capital, monthly_limit,
            float(exec_cfg.commission_rate), lot, fx, start_date, end_date,
            int(exec_cfg.min_holding_days),
        )

        # 三基线：两个市场基准 + 无风险。 这些数据既用于累计收益/超额
        # 也用于最近九个月的“任意日买入持有到期”胜率；两类指标不能混写。
        risk_free = RISK_FREE_A if group_name == "a_share" else RISK_FREE_NON_A
        benchmark_returns: dict[str, float] = {}
        benchmark_win_rates: dict[str, float] = {}
        benchmark_data = benchmark_data or {}
        benchmark_codes = get_constraints().benchmark_codes_for(group_name)
        for benchmark_code in benchmark_codes:
            if benchmark_code == "risk_free":
                continue
            strategy_values, benchmark_values = _aligned_benchmark_values(
                trace.nav_dates,
                trace.nav_series,
                benchmark_data.get(benchmark_code),
            )
            benchmark_return, win_rate = _benchmark_return_and_win_rate(
                strategy_values, benchmark_values
            )
            if benchmark_return is not None:
                benchmark_returns[benchmark_code] = benchmark_return
            if win_rate is not None:
                benchmark_win_rates[benchmark_code] = win_rate

        cash_return, cash_win_rate = _risk_free_return_and_win_rate(
            trace.nav_series, risk_free
        )
        benchmark_returns["risk_free"] = cash_return
        if cash_win_rate is not None:
            benchmark_win_rates["risk_free"] = cash_win_rate

        # 优先使用配置中的第一个可获得的市场基准；基准价格缺失时才回退现金。
        primary_return = next(iter(benchmark_returns.values()), cash_return)
        excess = trace.total_return_pct - primary_return

        # 平均现金仓位
        qh = trace.quarterly_holdings or []
        cash_pcts = [(100 - q.get("pos_pct", 0)) for q in qh if q.get("nav", 0) > 0]
        avg_cash = sum(cash_pcts) / len(cash_pcts) if cash_pcts else 0.0
        warmup_rows = max(1, int(getattr(strategy, "warmup_rows", 1)))
        eligible_codes: list[str] = []
        warming_codes: list[str] = []
        eligible_from: dict[str, str] = {}
        for code in codes:
            history = group_data.get(code)
            if history is None:
                continue
            if "date" in history.columns:
                rows = history["date"].dropna().tolist()
            else:
                rows = list(history.index)
            if len(rows) >= warmup_rows:
                eligible_codes.append(code)
                eligible_from[code] = str(pd.Timestamp(rows[warmup_rows - 1]).date())
            else:
                warming_codes.append(code)

        report = EvaluationReport(
            group=group_name,
            engine_name=engine_name,
            strategy_label=engine_names.get(engine_name, engine_name),
            timestamp=timestamp,
            total_return=round(trace.total_return_pct, 2),
            excess_return=round(excess, 2),
            max_drawdown=round(trace.max_drawdown_pct, 2),
            sharpe_ratio=round(trace.sharpe_ratio, 4),
            trade_count=trace.total_trades,
            avg_cash_pct=round(avg_cash, 0),
            benchmark_returns=benchmark_returns,
            benchmark_win_rates=benchmark_win_rates,
            composition=list(codes),
            eligible_codes=eligible_codes,
            warming_codes=warming_codes,
            eligible_from=eligible_from,
            nav_series=list(trace.nav_series),
            nav_dates=list(trace.nav_dates),
            quarterly_holdings=list(qh),
        )
        results[group_name] = report
        logger.info(
            f"{group_name} 评估完成: 收益{trace.total_return_pct:.1f}% "
            f"超额{excess:.1f}% 回撤{trace.max_drawdown_pct:.1f}% "
            f"夏普{trace.sharpe_ratio:.2f} {trace.total_trades}笔 "
            f"基线={benchmark_returns} 胜率={benchmark_win_rates}"
        )

    return results
