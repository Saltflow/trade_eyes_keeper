"""统一回测引擎。

输入天级数据 → 调用策略生成信号 → 模拟交易 → 输出 WindowStats / PortfolioTrace。
所有注册策略共用此引擎。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..search.config import WindowStats, get_constraints
from ..strategy import (
    EvaluationReport,
    Params,
    PortfolioTrace,
    StrategyMarketData,
    TradePlan,
)
from ..markets import _detect_fine_group, RISK_FREE_A, RISK_FREE_NON_A
from .execution import DEFAULT_FILL_PRICE_POLICY, ExecutionPriceSlice

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
IDX_HIGH = 16
IDX_LOW = 17
IDX_MA200 = 18
IDX_MA200_SLOPE = 19
IDX_PLUS_DI = 20
IDX_MINUS_DI = 21

INDICATOR_NAMES = [
    "close", "ma60", "deviation", "rsi", "macd", "macd_signal",
    "macd_hist", "vol_ratio", "boll_pct_b", "adx", "atr",
    "adx_pct", "rsi_pct", "deviation_pct", "vol_ratio_pct",
    "ma200_dev_pct", "high", "low", "ma200", "ma200_slope",
    "plus_di", "minus_di",
]


def pessimistic_buy_prices(high_prices: np.ndarray) -> np.ndarray:
    """Compatibility wrapper around the one shared fill-price policy."""
    return DEFAULT_FILL_PRICE_POLICY.buy_prices(high_prices)


def buy_and_hold_nav(
    close_prices: np.ndarray,
    buy_prices: np.ndarray,
    initial_cash: float,
    lot_size: int,
    commission_rate: float,
    weights: np.ndarray | None = None,
) -> np.ndarray:
    """Static buy-and-hold NAV using the same fee, lots and stress price."""
    closes = np.asarray(close_prices, dtype=np.float64)
    buys = np.asarray(buy_prices, dtype=np.float64)
    if closes.ndim == 1:
        closes = closes.reshape(-1, 1)
    if buys.ndim == 1:
        buys = buys.reshape(-1, 1)
    rows, columns = closes.shape
    if rows == 0 or columns == 0:
        return np.zeros(rows, dtype=np.float64)
    valuation = closes.copy()
    for column in range(columns):
        last = 0.0
        for row in range(rows):
            if np.isfinite(valuation[row, column]) and valuation[row, column] > 0:
                last = valuation[row, column]
            elif last > 0:
                valuation[row, column] = last
            else:
                valuation[row, column] = 0.0
    requested = (
        np.asarray(weights, dtype=np.float64)
        if weights is not None
        else np.full(columns, 1.0 / columns, dtype=np.float64)
    )
    requested = np.where(np.isfinite(requested) & (requested > 0), requested, 0.0)
    if requested.sum() > 0:
        requested = requested / requested.sum()
    shares = np.zeros(columns, dtype=np.float64)
    cash = float(initial_cash)
    for column in range(columns):
        execution_price = buys[0, column]
        if execution_price <= 0 or not np.isfinite(execution_price):
            continue
        budget = initial_cash * requested[column]
        quantity = int(
            budget / (execution_price * (1.0 + commission_rate)) / lot_size
        ) * lot_size
        if quantity <= 0:
            continue
        cost = quantity * execution_price * (1.0 + commission_rate)
        if cost > cash:
            continue
        shares[column] = quantity
        cash -= cost
    return cash + valuation.dot(shares)


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

# Canonical cash-cap simulator used by optimizer and daily evaluation.
if HAS_NUMBA:

    @jit(nopython=True, parallel=False, cache=True, nogil=True)
    def _simulate_cash_plan_numba(
        buy_signals,
        sell_signals,
        buy_priority,
        sell_priority,
        valuation_prices,
        buy_prices,
        sell_prices,
        tradable,
        initial_cash,
        buy_cash_limit,
        sell_cash_limit,
        lot_size,
        commission_rate,
        min_holding_days,
        snapshot_mask,
    ):
        T, N = buy_signals.shape
        shares = np.zeros(N, dtype=np.float64)
        cost_basis = np.zeros(N, dtype=np.float64)
        last_buy_day = np.full(N, -1000000, dtype=np.int32)
        cash = float(initial_cash)
        daily_values = np.zeros(T, dtype=np.float64)
        total_trades = 0
        pending_order_count = 0
        position_fraction_sum = 0.0
        position_fraction_days = 0

        n_snapshots = 0
        for t in range(T):
            if snapshot_mask[t]:
                n_snapshots += 1
        q_shares = np.zeros((n_snapshots, N), dtype=np.float64)
        q_cost_basis = np.zeros((n_snapshots, N), dtype=np.float64)
        q_cash = np.zeros(n_snapshots, dtype=np.float64)
        q_nav = np.zeros(n_snapshots, dtype=np.float64)
        q_prices = np.zeros((n_snapshots, N), dtype=np.float64)
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
                price = sell_prices[t, n]
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
                price = buy_prices[t, n]
                if price <= 0.0 or np.isnan(price):
                    if np.isnan(price):
                        pending_order_count += 1
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
                price = valuation_prices[t, n]
                if price > 0.0 and not np.isnan(price):
                    pos_value += shares[n] * price
            daily_values[t] = cash + pos_value
            if daily_values[t] > 0.0:
                position_fraction_sum += pos_value / daily_values[t]
                position_fraction_days += 1

            if snapshot_mask[t]:
                for n in range(N):
                    q_shares[q_idx, n] = shares[n]
                    q_cost_basis[q_idx, n] = cost_basis[n]
                    q_prices[q_idx, n] = valuation_prices[t, n]
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
                final_value += shares[n] * valuation_prices[T - 1, n]
            final_pos_pct = final_value / daily_values[T - 1] * 100.0
        return (
            daily_values, total_trades, avg_pos_pct, final_pos_pct,
            shares.copy(), cash, cost_basis.copy(),
            q_shares, q_cost_basis, q_cash, q_nav, q_prices,
            pending_order_count,
        )

else:
    def _simulate_cash_plan_numba(*args, **kwargs):
        raise NotImplementedError("numba required")


if HAS_NUMBA:

    @jit(nopython=True, parallel=False, cache=True, nogil=True)
    def _simulate_target_plan_numba(
        entry_events,
        exit_events,
        force_exit_signals,
        conviction,
        declared_target_weights,
        use_declared_target_weights,
        valuation_prices,
        buy_prices,
        sell_prices,
        tradable,
        date_ordinals,
        initial_cash,
        per_symbol_cap,
        total_exposure_cap,
        lot_size,
        commission_rate,
        min_holding_calendar_days,
        snapshot_mask,
    ):
        """Shared-cap target-weight execution without symbol-order allocation."""
        rows, columns = entry_events.shape
        shares = np.zeros(columns, dtype=np.float64)
        cost_basis = np.zeros(columns, dtype=np.float64)
        active = np.zeros(columns, dtype=np.bool_)
        active_score = np.zeros(columns, dtype=np.float64)
        entry_date = np.full(columns, -1000000000, dtype=np.int64)
        cash = float(initial_cash)
        daily_values = np.zeros(rows, dtype=np.float64)
        total_trades = 0
        pending_orders = 0
        position_fraction_sum = 0.0
        position_fraction_days = 0

        n_snapshots = 0
        for row in range(rows):
            if snapshot_mask[row]:
                n_snapshots += 1
        q_shares = np.zeros((n_snapshots, columns), dtype=np.float64)
        q_cost_basis = np.zeros((n_snapshots, columns), dtype=np.float64)
        q_cash = np.zeros(n_snapshots, dtype=np.float64)
        q_nav = np.zeros(n_snapshots, dtype=np.float64)
        q_prices = np.zeros((n_snapshots, columns), dtype=np.float64)
        q_index = 0
        targets = np.zeros(columns, dtype=np.float64)
        desired_buys = np.zeros(columns, dtype=np.float64)

        for row in range(rows):
            state_changed = False
            # Explicit exits always happen before allocation.  The strategy
            # marks catastrophe exits separately so they bypass the 30-day
            # ordinary holding lock.
            for column in range(columns):
                force = force_exit_signals[row, column]
                ordinary = exit_events[row, column]
                held_days = date_ordinals[row] - entry_date[column]
                if not force and not (
                    ordinary and held_days >= min_holding_calendar_days
                ):
                    continue
                if active[column]:
                    active[column] = False
                    active_score[column] = 0.0
                    state_changed = True
                if shares[column] <= 0.0 or not tradable[row, column]:
                    continue
                execution_price = sell_prices[row, column]
                if (
                    execution_price <= 0.0
                    or np.isnan(execution_price)
                ):
                    continue
                quantity = int(shares[column] / lot_size) * lot_size
                if quantity <= 0 and shares[column] > 0:
                    quantity = int(shares[column])
                if quantity <= 0:
                    continue
                value = quantity * execution_price
                cash += value * (1.0 - commission_rate)
                shares[column] -= quantity
                if shares[column] <= 0.0:
                    shares[column] = 0.0
                    cost_basis[column] = 0.0
                total_trades += 1

            # A missing t+1 stress price means the one-shot event is pending,
            # not a fill.  It never enters target allocation for this window.
            for column in range(columns):
                if not entry_events[row, column] or active[column]:
                    continue
                execution_price = buy_prices[row, column]
                if (
                    execution_price <= 0.0
                    or np.isnan(execution_price)
                    or not tradable[row, column]
                ):
                    pending_orders += 1
                    continue
                active[column] = True
                active_score[column] = max(conviction[row, column], 0.000001)
                entry_date[column] = date_ordinals[row]
                state_changed = True

            if state_changed:
                for column in range(columns):
                    targets[column] = 0.0
                exposure_cap = min(max(total_exposure_cap, 0.0), 1.0)
                if use_declared_target_weights:
                    declared_total = 0.0
                    for column in range(columns):
                        if not active[column]:
                            continue
                        requested = declared_target_weights[row, column]
                        if np.isnan(requested) or requested <= 0.0:
                            continue
                        targets[column] = min(requested, per_symbol_cap)
                        declared_total += targets[column]
                    if declared_total > exposure_cap and declared_total > 0.0:
                        scale = exposure_cap / declared_total
                        for column in range(columns):
                            targets[column] *= scale
                else:
                    # Compatibility path for generic target-weight plans that
                    # predate the explicit target_weights matrix.
                    remaining = exposure_cap
                    while remaining > 0.000000000001:
                        score_sum = 0.0
                        open_count = 0
                        for column in range(columns):
                            if (
                                active[column]
                                and active_score[column] > 0.0
                                and targets[column]
                                < per_symbol_cap - 0.000000000001
                            ):
                                score_sum += active_score[column]
                                open_count += 1
                        if open_count == 0 or score_sum <= 0.0:
                            break
                        used = 0.0
                        for column in range(columns):
                            if (
                                not active[column]
                                or active_score[column] <= 0.0
                                or targets[column]
                                >= per_symbol_cap - 0.000000000001
                            ):
                                continue
                            proposal = (
                                remaining * active_score[column] / score_sum
                            )
                            room = per_symbol_cap - targets[column]
                            addition = min(proposal, room)
                            targets[column] += addition
                            used += addition
                        if used <= 0.000000000001:
                            break
                        remaining -= used

                nav_before = cash
                for column in range(columns):
                    nav_before += shares[column] * valuation_prices[row, column]

                # Reductions use the trigger-day low.  Young ordinary
                # holdings are never reduced merely to fund another signal.
                for column in range(columns):
                    current_value = shares[column] * valuation_prices[row, column]
                    target_value = nav_before * targets[column]
                    held_days = date_ordinals[row] - entry_date[column]
                    if (
                        current_value <= target_value
                        or shares[column] <= 0.0
                        or held_days < min_holding_calendar_days
                        or not tradable[row, column]
                    ):
                        continue
                    execution_price = sell_prices[row, column]
                    if execution_price <= 0.0 or np.isnan(execution_price):
                        continue
                    excess_value = current_value - target_value
                    quantity = int(excess_value / execution_price / lot_size)
                    quantity *= lot_size
                    max_quantity = int(shares[column] / lot_size) * lot_size
                    quantity = min(quantity, max_quantity)
                    if quantity <= 0:
                        continue
                    value = quantity * execution_price
                    cash += value * (1.0 - commission_rate)
                    shares[column] -= quantity
                    total_trades += 1

                nav_after_sells = cash
                for column in range(columns):
                    nav_after_sells += (
                        shares[column] * valuation_prices[row, column]
                    )
                    desired_buys[column] = 0.0

                required_cash = 0.0
                for column in range(columns):
                    if not active[column] or not tradable[row, column]:
                        continue
                    execution_price = buy_prices[row, column]
                    close_price = valuation_prices[row, column]
                    if (
                        execution_price <= 0.0
                        or close_price <= 0.0
                        or np.isnan(execution_price)
                    ):
                        continue
                    target_shares = nav_after_sells * targets[column] / close_price
                    raw_quantity = max(target_shares - shares[column], 0.0)
                    desired_buys[column] = raw_quantity
                    required_cash += raw_quantity * execution_price * (
                        1.0 + commission_rate
                    )
                scale = 1.0
                if required_cash > cash and required_cash > 0.0:
                    scale = cash / required_cash
                for column in range(columns):
                    execution_price = buy_prices[row, column]
                    quantity = int(
                        desired_buys[column] * scale / lot_size
                    ) * lot_size
                    if quantity <= 0 or execution_price <= 0.0:
                        continue
                    total_cost = quantity * execution_price * (
                        1.0 + commission_rate
                    )
                    if total_cost > cash + 0.000001:
                        continue
                    old_shares = shares[column]
                    shares[column] += quantity
                    cost_basis[column] = (
                        old_shares * cost_basis[column]
                        + quantity * execution_price
                    ) / shares[column]
                    cash -= total_cost
                    total_trades += 1

            position_value = 0.0
            for column in range(columns):
                position_value += shares[column] * valuation_prices[row, column]
            daily_values[row] = cash + position_value
            if daily_values[row] > 0.0:
                position_fraction_sum += position_value / daily_values[row]
                position_fraction_days += 1

            if snapshot_mask[row]:
                for column in range(columns):
                    q_shares[q_index, column] = shares[column]
                    q_cost_basis[q_index, column] = cost_basis[column]
                    q_prices[q_index, column] = valuation_prices[row, column]
                q_cash[q_index] = cash
                q_nav[q_index] = daily_values[row]
                q_index += 1

        average_position = 0.0
        if position_fraction_days > 0:
            average_position = (
                position_fraction_sum / position_fraction_days * 100.0
            )
        final_position = 0.0
        if rows > 0 and daily_values[rows - 1] > 0.0:
            value = 0.0
            for column in range(columns):
                value += shares[column] * valuation_prices[rows - 1, column]
            final_position = value / daily_values[rows - 1] * 100.0
        return (
            daily_values,
            total_trades,
            average_position,
            final_position,
            shares.copy(),
            cash,
            cost_basis.copy(),
            q_shares,
            q_cost_basis,
            q_cash,
            q_nav,
            q_prices,
            pending_orders,
        )

else:

    def _simulate_target_plan_numba(*args, **kwargs):
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
        self.fx_rate = float(exec_cfg.fx_rates.get(group, 1.0))
        self.buy_confirmation_days = 3
        self.sell_confirmation_days = 1

    def evaluate(
        self,
        indicator_matrix: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        benchmark_series: dict[str, np.ndarray] | None = None,
        benchmark_initial_values: dict[str, float] | None = None,
        benchmark_raw_returns: dict[str, float] | None = None,
        trade_plan: TradePlan | None = None,
        execution_prices: ExecutionPriceSlice | None = None,
        tradable: np.ndarray | None = None,
    ) -> WindowStats:
        """Evaluate one TradePlan through its declared execution model."""
        T, N = indicator_matrix.shape[:2]
        if N == 0 or T == 0:
            return WindowStats()

        if trade_plan is None:
            raise ValueError("FastEvaluator requires a canonical TradePlan")
        expected_shape = (T, N)
        if trade_plan.buy_signals.shape != expected_shape:
            raise ValueError("TradePlan and evaluator matrix shapes do not match")
        buy_signals = np.asarray(trade_plan.buy_signals, dtype=bool)
        sell_signals = np.asarray(trade_plan.sell_signals, dtype=bool)
        buy_priority = np.asarray(trade_plan.buy_priority, dtype=np.float32)
        sell_priority = np.asarray(trade_plan.sell_priority, dtype=np.float32)
        buy_cash_limit = float(trade_plan.buy_cash_limit)
        sell_cash_limit = float(trade_plan.sell_cash_limit)
        execution_model = str(trade_plan.execution.get("model", "cash_cap"))
        if (
            execution_model == "cash_cap"
            and (buy_cash_limit <= 0.0 or sell_cash_limit <= 0.0)
        ):
            return WindowStats()
        if execution_prices is None:
            execution_prices = DEFAULT_FILL_PRICE_POLICY.build(price_matrix)
        resolved_prices = execution_prices.scaled(self.fx_rate)
        if resolved_prices.valuation_prices.shape != expected_shape:
            raise ValueError(
                "ExecutionPriceSlice and evaluator matrix shapes do not match"
            )
        if tradable is None:
            tradable = resolved_prices.tradable
        valuation_prices = np.asarray(
            resolved_prices.valuation_prices, dtype=np.float32
        ).copy()
        buy_prices = np.asarray(
            resolved_prices.buy_prices, dtype=np.float32
        )
        sell_prices = np.asarray(
            resolved_prices.sell_prices, dtype=np.float32
        )
        for n in range(N):
            last = 0.0
            for t in range(T):
                if np.isfinite(valuation_prices[t, n]) and valuation_prices[t, n] > 0:
                    last = valuation_prices[t, n]
                elif last > 0.0:
                    valuation_prices[t, n] = last
                else:
                    valuation_prices[t, n] = 0.0

        pending_order_count = 0
        if HAS_NUMBA and execution_model == "target_weight":
            entry_events = (
                trade_plan.entry_events
                if trade_plan.entry_events is not None
                else buy_signals
            )
            exit_events = (
                trade_plan.exit_events
                if trade_plan.exit_events is not None
                else sell_signals
            )
            force_exits = (
                trade_plan.force_exit_signals
                if trade_plan.force_exit_signals is not None
                else np.zeros(expected_shape, dtype=bool)
            )
            conviction = (
                trade_plan.conviction
                if trade_plan.conviction is not None
                else np.where(entry_events, buy_priority, 0.0)
            )
            declared_target_weights = (
                np.asarray(trade_plan.target_weights, dtype=np.float32)
                if trade_plan.target_weights is not None
                else np.zeros(expected_shape, dtype=np.float32)
            )
            if (
                trade_plan.date_ordinals is not None
                and len(trade_plan.date_ordinals) == T
            ):
                date_ordinals = np.asarray(
                    trade_plan.date_ordinals, dtype=np.int64
                )
            elif len(trade_plan.dates) == T:
                parsed = pd.to_datetime(trade_plan.dates, errors="coerce")
                if not pd.isna(parsed).any():
                    date_ordinals = (
                        parsed.values.astype("datetime64[D]").astype(np.int64)
                    )
                else:
                    date_ordinals = np.arange(T, dtype=np.int64)
            else:
                date_ordinals = np.arange(T, dtype=np.int64)
            (
                daily_values, trade_count, avg_pos_pct, final_pos_pct,
                final_shares, final_cash, cost_basis,
                quarter_shares, quarter_cost_basis, quarter_cash,
                quarter_nav, quarter_prices, pending_order_count,
            ) = _simulate_target_plan_numba(
                np.ascontiguousarray(entry_events, dtype=np.bool_),
                np.ascontiguousarray(exit_events, dtype=np.bool_),
                np.ascontiguousarray(force_exits, dtype=np.bool_),
                np.ascontiguousarray(conviction, dtype=np.float32),
                np.ascontiguousarray(declared_target_weights, dtype=np.float32),
                trade_plan.target_weights is not None,
                np.ascontiguousarray(valuation_prices, dtype=np.float32),
                np.ascontiguousarray(buy_prices, dtype=np.float32),
                np.ascontiguousarray(sell_prices, dtype=np.float32),
                np.ascontiguousarray(tradable, dtype=np.bool_),
                np.ascontiguousarray(date_ordinals, dtype=np.int64),
                float(self.initial_cash),
                float(trade_plan.execution.get("per_symbol_cap", 0.20)),
                float(trade_plan.execution.get("total_exposure_cap", 0.80)),
                int(self.lot_size),
                float(self.commission_rate),
                int(
                    trade_plan.execution.get(
                        "min_holding_calendar_days", self.min_holding_days
                    )
                ),
                np.zeros(T, dtype=np.bool_),
            )
        elif HAS_NUMBA:
            (
                daily_values, trade_count, avg_pos_pct, final_pos_pct,
                final_shares, final_cash, cost_basis,
                quarter_shares, quarter_cost_basis, quarter_cash,
                quarter_nav, quarter_prices, pending_order_count,
            ) = _simulate_cash_plan_numba(
                np.ascontiguousarray(buy_signals, dtype=np.bool_),
                np.ascontiguousarray(sell_signals, dtype=np.bool_),
                np.ascontiguousarray(buy_priority, dtype=np.float32),
                np.ascontiguousarray(sell_priority, dtype=np.float32),
                np.ascontiguousarray(valuation_prices, dtype=np.float32),
                np.ascontiguousarray(buy_prices, dtype=np.float32),
                np.ascontiguousarray(sell_prices, dtype=np.float32),
                np.ascontiguousarray(tradable, dtype=np.bool_),
                float(self.initial_cash), buy_cash_limit, sell_cash_limit,
                int(self.lot_size), float(self.commission_rate),
                int(self.min_holding_days),
                np.zeros(T, dtype=np.bool_),
            )
        else:
            return WindowStats()

        return _compute_stats(
            daily_values, valuation_prices, cash_baseline,
            trade_count, 0, avg_pos_pct=avg_pos_pct,
            benchmark_series=benchmark_series,
            benchmark_initial_values=benchmark_initial_values,
            benchmark_raw_returns=benchmark_raw_returns,
            initial_asset=self.initial_cash,
            final_pos_pct=final_pos_pct, final_shares=final_shares,
            final_cash=final_cash, cost_basis=cost_basis,
            quarter_shares=quarter_shares, quarter_cash=quarter_cash,
            quarter_nav=quarter_nav, quarter_prices=quarter_prices,
            quarter_cost_basis=quarter_cost_basis,
            pending_order_count=pending_order_count,
        )

    def evaluate_batch(
        self,
        trade_plans: list[TradePlan],
        *,
        workers: int = 1,
        **window_inputs,
    ) -> list[WindowStats]:
        """Evaluate one columnar candidate batch on a shared window.

        Each Numba simulator remains strictly serial along dates.  Candidate
        evaluations share the immutable price/benchmark arrays and occupy the
        selected outer CPU axis, avoiding process copies and nested pools.
        """
        if not trade_plans:
            return []
        from .batch import evaluate_cash_batch

        cash_batch = evaluate_cash_batch(self, trade_plans, window_inputs)
        if cash_batch is not None:
            return cash_batch
        worker_count = min(max(1, int(workers)), len(trade_plans))
        if worker_count == 1:
            return [
                self.evaluate(trade_plan=plan, **window_inputs)
                for plan in trade_plans
            ]
        from concurrent.futures import ThreadPoolExecutor

        def evaluate_plan(plan):
            return self.evaluate(trade_plan=plan, **window_inputs)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            return list(pool.map(evaluate_plan, trade_plans))


def _compute_stats(
    daily_values,
    price_matrix,
    cash_baseline,
    trade_count,
    signal_count,
    avg_pos_pct=None,
    benchmark_series=None,
    benchmark_initial_values=None,
    benchmark_raw_returns=None,
    final_pos_pct=None,
    final_shares=None,
    final_cash=None,
    cost_basis=None,
    quarter_shares=None,
    quarter_cash=None,
    quarter_nav=None,
    quarter_prices=None,
    quarter_cost_basis=None,
    pending_order_count=0,
    initial_asset=None,
) -> WindowStats:
    """daily_values → WindowStats。"""
    T = len(daily_values)

    nav = daily_values
    base_asset = float(initial_asset) if initial_asset is not None else float(nav[0])
    total_return = (
        float((nav[-1] - base_asset) / base_asset * 100)
        if len(nav) and base_asset > 0
        else 0.0
    )
    drawdown_nav = np.concatenate(([base_asset], np.asarray(nav, dtype=float)))
    peak = np.maximum.accumulate(drawdown_nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_series = np.where(
            peak > 0, (drawdown_nav - peak) / peak * 100.0, 0.0
        )
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
            initial = float(
                (benchmark_initial_values or {}).get(
                    lbl, bs[0] if bs is not None and len(bs) else 0.0
                )
            )
            if bs is not None and len(bs) > 1 and initial > 0:
                br = (bs[-1] - initial) / initial * 100
                benchmark_returns[lbl] = round(br, 2)
        if benchmark_returns:
            strongest = max(
                benchmark_returns, key=lambda label: benchmark_returns[label]
            )
            excess_return = total_return - benchmark_returns[strongest]
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

    strongest_benchmark = (
        max(benchmark_returns, key=lambda label: benchmark_returns[label])
        if benchmark_returns
        else ""
    )
    return WindowStats(
        test_excess_return=round(excess_return, 2),
        max_drawdown_pct=round(max_dd, 2),
        avg_position_pct=round(avg_pos_pct, 2),
        sharpe_ratio=round(sharpe, 4),
        total_trades=trade_count,
        benchmark_returns=benchmark_returns,
        strategy_return=round(total_return, 2),
        initial_asset=round(base_asset, 2) if len(nav) else 0.0,
        final_asset=round(float(nav[-1]), 2) if len(nav) else 0.0,
        final_position_pct=round(final_pos_pct or 0.0, 2),
        final_shares=final_shares,
        final_prices=(price_matrix[-1].copy() if len(price_matrix) else None),
        final_cash=final_cash or 0.0,
        cost_basis=cost_basis,
        quarter_shares=quarter_shares,
        quarter_cash=quarter_cash,
        quarter_nav=quarter_nav,
        quarter_prices=quarter_prices,
        quarter_cost_basis=quarter_cost_basis,
        pending_order_count=int(pending_order_count),
        strongest_benchmark=strongest_benchmark,
        benchmark_raw_returns=dict(benchmark_raw_returns or {}),
    )


# ═══════════════════════════════════════════════════════════════
# simulate_portfolio — 分数 → 统一引擎 → PortfolioTrace
# ═══════════════════════════════════════════════════════════════

def simulate_portfolio(
    trade_plan: TradePlan,
    market_data: StrategyMarketData,
    initial_cash: float,
    lot_size: int,
    commission_rate: float,
    min_holding_days: int = 0,
    execution_price_scale: float = 1.0,
    execution_prices: ExecutionPriceSlice | None = None,
) -> PortfolioTrace:
    """Execute the canonical strategy decision plan."""
    if market_data.prices is None:
        raise ValueError("StrategyMarketData.prices is required for execution")
    price = np.asarray(market_data.prices, dtype=np.float32)
    buy_signals = np.asarray(trade_plan.buy_signals, dtype=bool)
    sell_signals = np.asarray(trade_plan.sell_signals, dtype=bool)
    if buy_signals.shape != price.shape or sell_signals.shape != price.shape:
        raise ValueError("TradePlan and market price shapes do not match")
    T, N = buy_signals.shape
    buy_priority = np.asarray(trade_plan.buy_priority, dtype=np.float32)
    sell_priority = np.asarray(trade_plan.sell_priority, dtype=np.float32)
    buy_cash_limit = float(trade_plan.buy_cash_limit)
    sell_cash_limit = float(trade_plan.sell_cash_limit)
    execution_model = str(trade_plan.execution.get("model", "cash_cap"))
    if execution_prices is None:
        execution_prices = DEFAULT_FILL_PRICE_POLICY.build(
            price,
            market_data.highs,
            market_data.lows,
        )
    resolved_prices = execution_prices.scaled(execution_price_scale)
    if resolved_prices.valuation_prices.shape != buy_signals.shape:
        raise ValueError(
            "ExecutionPriceSlice and TradePlan shapes do not match"
        )
    tradable = (
        np.asarray(market_data.tradable, dtype=bool)
        if market_data.tradable is not None
        else resolved_prices.tradable
    )
    dates = list(market_data.dates)
    stock_codes = list(market_data.symbols)
    valuation_price = np.asarray(
        resolved_prices.valuation_prices, dtype=np.float32
    ).copy()
    buy_prices = np.asarray(resolved_prices.buy_prices, dtype=np.float32)
    sell_prices = np.asarray(resolved_prices.sell_prices, dtype=np.float32)
    for n in range(N):
        last = 0.0
        for t in range(T):
            if np.isfinite(valuation_price[t, n]) and valuation_price[t, n] > 0:
                last = valuation_price[t, n]
            elif last > 0:
                valuation_price[t, n] = last
            else:
                valuation_price[t, n] = 0.0

    snapshot_mask = np.zeros(T, dtype=np.bool_)
    snapshot_indices: list[int] = []
    if len(dates) == T and T:
        parsed_dates = pd.DatetimeIndex(pd.to_datetime(dates, errors="coerce"))
        if not parsed_dates.hasnans:
            periods = parsed_dates.to_period("Q")
            for index in range(T - 1):
                if periods[index] != periods[index + 1]:
                    snapshot_mask[index] = True
                    snapshot_indices.append(index)
            quarter_end = periods[-1].end_time.normalize()
            days_to_end = int((quarter_end - parsed_dates[-1].normalize()).days)
            if 0 <= days_to_end <= 7:
                snapshot_mask[-1] = True
                snapshot_indices.append(T - 1)

    pending_order_count = 0
    if HAS_NUMBA and execution_model == "target_weight":
        entry_events = (
            trade_plan.entry_events
            if trade_plan.entry_events is not None
            else buy_signals
        )
        exit_events = (
            trade_plan.exit_events
            if trade_plan.exit_events is not None
            else sell_signals
        )
        force_exits = (
            trade_plan.force_exit_signals
            if trade_plan.force_exit_signals is not None
            else np.zeros_like(buy_signals, dtype=bool)
        )
        conviction = (
            trade_plan.conviction
            if trade_plan.conviction is not None
            else np.where(entry_events, buy_priority, 0.0)
        )
        declared_target_weights = (
            np.asarray(trade_plan.target_weights, dtype=np.float32)
            if trade_plan.target_weights is not None
            else np.zeros_like(buy_signals, dtype=np.float32)
        )
        if (
            trade_plan.date_ordinals is not None
            and len(trade_plan.date_ordinals) == T
        ):
            date_ordinals = np.asarray(
                trade_plan.date_ordinals, dtype=np.int64
            )
        elif len(dates) == T:
            parsed = pd.to_datetime(dates, errors="coerce")
            if not pd.isna(parsed).any():
                date_ordinals = (
                    parsed.values.astype("datetime64[D]").astype(np.int64)
                )
            else:
                date_ordinals = np.arange(T, dtype=np.int64)
        else:
            date_ordinals = np.arange(T, dtype=np.int64)
        (
            daily_values, trade_count, avg_pos_pct, final_pos_pct,
            final_shares, final_cash, cost_basis,
            q_shares, q_cost_basis, q_cash, q_nav, q_prices,
            pending_order_count,
        ) = _simulate_target_plan_numba(
            np.ascontiguousarray(entry_events, dtype=np.bool_),
            np.ascontiguousarray(exit_events, dtype=np.bool_),
            np.ascontiguousarray(force_exits, dtype=np.bool_),
            np.ascontiguousarray(conviction, dtype=np.float32),
            np.ascontiguousarray(declared_target_weights, dtype=np.float32),
            trade_plan.target_weights is not None,
            np.ascontiguousarray(valuation_price, dtype=np.float32),
            np.ascontiguousarray(buy_prices, dtype=np.float32),
            np.ascontiguousarray(sell_prices, dtype=np.float32),
            np.ascontiguousarray(tradable, dtype=np.bool_),
            np.ascontiguousarray(date_ordinals, dtype=np.int64),
            float(initial_cash),
            float(trade_plan.execution.get("per_symbol_cap", 0.20)),
            float(trade_plan.execution.get("total_exposure_cap", 0.80)),
            int(lot_size),
            float(commission_rate),
            int(
                trade_plan.execution.get(
                    "min_holding_calendar_days", min_holding_days
                )
            ),
            np.ascontiguousarray(snapshot_mask),
        )
    elif HAS_NUMBA:
        (
            daily_values, trade_count, avg_pos_pct, final_pos_pct,
            final_shares, final_cash, cost_basis,
            q_shares, q_cost_basis, q_cash, q_nav, q_prices,
            pending_order_count,
        ) = _simulate_cash_plan_numba(
            np.ascontiguousarray(buy_signals, dtype=np.bool_),
            np.ascontiguousarray(sell_signals, dtype=np.bool_),
            np.ascontiguousarray(buy_priority, dtype=np.float32),
            np.ascontiguousarray(sell_priority, dtype=np.float32),
            np.ascontiguousarray(valuation_price, dtype=np.float32),
            np.ascontiguousarray(buy_prices, dtype=np.float32),
            np.ascontiguousarray(sell_prices, dtype=np.float32),
            np.ascontiguousarray(tradable, dtype=np.bool_),
            float(initial_cash), float(buy_cash_limit), float(sell_cash_limit),
            int(lot_size), float(commission_rate), int(min_holding_days),
            np.ascontiguousarray(snapshot_mask),
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
    quarterly_holdings = []
    for qi, snapshot_index in enumerate(snapshot_indices):
        qpos = []
        for i, code in enumerate(stock_codes):
            sh = q_shares[qi, i]
            if sh > 0.5:
                px = float(q_prices[qi, i])
                cb_i = float(q_cost_basis[qi, i])
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
            "quarter": str(pd.Timestamp(dates[snapshot_index]).to_period("Q")),
            "date": dates[snapshot_index],
            "day": snapshot_index,
            "cash": round(float(q_cash[qi]), 2),
            "nav": round(float(q_nav[qi]), 2),
            "pos_pct": qp, "positions": qpos,
        })

    nav = daily_values
    total_return = (
        float((nav[-1] - initial_cash) / initial_cash * 100)
        if len(nav) and initial_cash > 0
        else 0.0
    )
    drawdown_nav = np.concatenate(([float(initial_cash)], nav))
    peak = np.maximum.accumulate(drawdown_nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_series = np.where(
            peak > 0, (drawdown_nav - peak) / peak * 100.0, 0.0
        )
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
        final_shares=final_shares,
        final_prices=(valuation_price[-1].copy() if len(valuation_price) else None),
        final_cash=float(final_cash),
        pending_order_count=int(pending_order_count),
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
    train_start_date: str = ""
    train_end_date: str = ""
    test_start_date: str = ""
    test_end_date: str = ""


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
        from ..search.config import WalkForwardConfig

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
        self.price_high_matrix = np.full((self.T, N), np.nan, dtype=np.float32)
        self.price_low_matrix = np.full((self.T, N), np.nan, dtype=np.float32)

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
            if "high" in aligned.columns:
                self.price_high_matrix[:, i] = aligned["high"].values
            if "low" in aligned.columns:
                self.price_low_matrix[:, i] = aligned["low"].values

        self.benchmark_series: dict[str, np.ndarray] = {}
        self.benchmark_high_series: dict[str, np.ndarray] = {}
        self.benchmark_raw_returns: dict[str, np.ndarray] = {}
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
                self.benchmark_raw_returns[str(code)] = values.copy()
            if "high" in benchmark.columns:
                high_values = (
                    pd.to_numeric(benchmark["high"], errors="coerce")
                    .reindex(self.dates)
                    .ffill()
                    .to_numpy(dtype=np.float64)
                )
            else:
                high_values = values.copy()
            if np.isfinite(high_values).any():
                self.benchmark_high_series[str(code)] = high_values

    def build_matrices(self):
        return self.indicator_matrix, self.price_matrix, self.price_open_matrix

    def iter_windows(self) -> list[WindowSlice]:
        """Build calendar-month windows ending on the newest market date."""
        if self.T == 0:
            return []

        end_exclusive = self.dates[-1].normalize() + pd.Timedelta(days=1)
        horizon_start = end_exclusive - pd.DateOffset(
            months=self.wf_config.total_months_needed
        )
        if self.dates[0] > horizon_start:
            return []

        windows = []
        for wi in range(self.wf_config.num_windows):
            train_start_date = horizon_start + pd.DateOffset(
                months=wi * self.wf_config.step_months
            )
            test_start_date = train_start_date + pd.DateOffset(
                months=self.wf_config.train_months
            )
            test_end_date = test_start_date + pd.DateOffset(
                months=self.wf_config.test_months
            )
            train_start = int(self.dates.searchsorted(train_start_date, side="left"))
            test_start = int(self.dates.searchsorted(test_start_date, side="left"))
            test_end = int(self.dates.searchsorted(test_end_date, side="left"))
            if train_start >= test_start or test_start >= test_end:
                return []
            windows.append(WindowSlice(
                train_start=train_start,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
                window_index=wi,
                train_start_date=str(train_start_date.date()),
                train_end_date=str(test_start_date.date()),
                test_start_date=str(test_start_date.date()),
                test_end_date=str((test_end_date - pd.Timedelta(days=1)).date()),
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


class Backtester:
    """The single TradePlan execution and EvaluationReport engine."""

    def __init__(self, execution_config, group: str):
        self.execution = execution_config
        self.group = group

    def simulate(
        self,
        trade_plan: TradePlan,
        market_data: StrategyMarketData,
        execution_prices: ExecutionPriceSlice | None = None,
    ) -> PortfolioTrace:
        prices = np.asarray(market_data.prices, dtype=np.float32)
        expected = prices.shape
        if trade_plan.buy_signals.shape != expected:
            raise ValueError("TradePlan and market price shapes do not match")
        fx_rate = float(self.execution.fx_rates.get(self.group, 1.0))
        return simulate_portfolio(
            trade_plan,
            market_data,
            float(self.execution.initial_capital),
            lot_size=int(self.execution.lot_sizes.get(self.group, 100)),
            commission_rate=float(self.execution.commission_rate),
            min_holding_days=int(self.execution.min_holding_days),
            execution_price_scale=fx_rate,
            execution_prices=execution_prices,
        )

    def run(
        self,
        trade_plan: TradePlan,
        market_data: StrategyMarketData,
        benchmark_data: dict[str, pd.DataFrame] | None = None,
        benchmark_codes: list[str] | None = None,
        primary_benchmark: str = "risk_free",
        risk_free_rate: float = 0.02,
        strategy_id: str = "",
        strategy_label: str = "",
        execution_prices: ExecutionPriceSlice | None = None,
    ) -> EvaluationReport:
        if execution_prices is None:
            execution_prices = DEFAULT_FILL_PRICE_POLICY.build(
                market_data.prices,
                market_data.highs,
                market_data.lows,
            )
        trace = self.simulate(trade_plan, market_data, execution_prices)
        benchmark_data = benchmark_data or {}
        benchmark_returns: dict[str, float] = {}
        benchmark_win_rates: dict[str, float] = {}
        benchmark_excess_returns: dict[str, float] = {}
        benchmark_details: dict[str, dict[str, object]] = {}
        benchmark_raw_returns: dict[str, float] = {}
        for code in benchmark_codes or []:
            if code in {"risk_free", "510300", "universe_equal_weight"}:
                continue
            strategy_values, benchmark_values = _aligned_benchmark_values(
                trace.nav_dates, trace.nav_series, benchmark_data.get(code)
            )
            detail = _benchmark_detail(strategy_values, benchmark_values)
            if detail is None:
                continue
            detail["is_primary"] = code == primary_benchmark
            benchmark_details[code] = detail
            benchmark_returns[code] = float(detail["benchmark_return"])
            benchmark_excess_returns[code] = float(
                detail["strategy_excess_return"]
            )
            if detail["win_rate"] is not None:
                benchmark_win_rates[code] = float(detail["win_rate"])
            benchmark_raw_returns[code] = float(detail["benchmark_return"])

        # The tradable 510300 baseline pays the same fee and uses the same
        # pessimistic high-price entry as the strategy execution contract.
        benchmark_close, _benchmark_high = _aligned_benchmark_ohlc(
            trace.nav_dates, benchmark_data.get("510300")
        )
        if len(benchmark_close) == len(trace.nav_series) and len(benchmark_close) > 1:
            entry_prices = _benchmark_stress_prices(
                trace.nav_dates, benchmark_data.get("510300")
            )
            detail = _tradable_benchmark_detail(
                np.asarray(trace.nav_series, dtype=float),
                benchmark_close,
                entry_prices,
                float(self.execution.initial_capital),
                int(self.execution.lot_sizes.get("a_share", 100)),
                float(self.execution.commission_rate),
            )
            if detail is not None:
                raw_return = (
                    benchmark_close[-1] / benchmark_close[0] - 1.0
                ) * 100.0
                detail["raw_price_return"] = round(float(raw_return), 2)
                benchmark_details["510300"] = detail
                benchmark_returns["510300"] = float(
                    detail["benchmark_return"]
                )
                benchmark_excess_returns["510300"] = float(
                    detail["strategy_excess_return"]
                )
                benchmark_raw_returns["510300"] = round(float(raw_return), 2)
                if detail["win_rate"] is not None:
                    benchmark_win_rates["510300"] = float(detail["win_rate"])

        # Static equal-weight in the exact configured universe is the control
        # for asset selection; only timing should earn excess return.
        fx_rate = float(self.execution.fx_rates.get(self.group, 1.0))
        resolved_execution = execution_prices.scaled(fx_rate)
        universe_close = resolved_execution.valuation_prices
        if len(universe_close) > 1 and universe_close.shape[1] > 0:
            detail = _tradable_benchmark_detail(
                np.asarray(trace.nav_series, dtype=float),
                universe_close,
                resolved_execution.buy_prices,
                float(self.execution.initial_capital),
                int(self.execution.lot_sizes.get(self.group, 100)),
                float(self.execution.commission_rate),
            )
            if detail is not None:
                raw_components = []
                for column in range(universe_close.shape[1]):
                    values = universe_close[:, column]
                    valid = values[np.isfinite(values) & (values > 0)]
                    if len(valid) > 1:
                        raw_components.append(valid[-1] / valid[0] - 1.0)
                raw_return = (
                    float(np.mean(raw_components) * 100.0)
                    if raw_components
                    else 0.0
                )
                detail["raw_price_return"] = round(raw_return, 2)
                benchmark_details["universe_equal_weight"] = detail
                benchmark_returns["universe_equal_weight"] = float(
                    detail["benchmark_return"]
                )
                benchmark_excess_returns["universe_equal_weight"] = float(
                    detail["strategy_excess_return"]
                )
                benchmark_raw_returns["universe_equal_weight"] = round(
                    raw_return, 2
                )
                if detail["win_rate"] is not None:
                    benchmark_win_rates["universe_equal_weight"] = float(
                        detail["win_rate"]
                    )

        cash_detail = _risk_free_detail(trace.nav_series, risk_free_rate)
        cash_return = 0.0
        if cash_detail is not None:
            benchmark_details["risk_free"] = cash_detail
            cash_return = float(cash_detail["benchmark_return"])
            benchmark_returns["risk_free"] = cash_return
            benchmark_excess_returns["risk_free"] = float(
                cash_detail["strategy_excess_return"]
            )
            if cash_detail["win_rate"] is not None:
                benchmark_win_rates["risk_free"] = float(cash_detail["win_rate"])

        final_asset = (
            float(trace.daily_values[-1])
            if len(trace.daily_values)
            else float(self.execution.initial_capital)
        )
        holdings = _final_holdings(trace, list(market_data.symbols))
        holdings_value = sum(float(item["value"]) for item in holdings)
        unified_candidates = [
            code
            for code in ("risk_free", "510300", "universe_equal_weight")
            if code in benchmark_returns
        ]
        # Legacy reports without 510300 keep their configured display primary;
        # native unified runs always select the strongest of the three controls.
        if "510300" in benchmark_returns and unified_candidates:
            primary_benchmark = max(
                unified_candidates, key=lambda code: benchmark_returns[code]
            )
        primary_return = benchmark_returns.get(primary_benchmark, cash_return)
        for code, detail in benchmark_details.items():
            detail["is_primary"] = code == primary_benchmark
            detail["strategy_excess_return"] = round(
                float(
                    trace.total_return_pct
                    - float(detail.get("benchmark_return", 0.0))
                ),
                2,
            )
            benchmark_excess_returns[code] = float(
                detail["strategy_excess_return"]
            )
        return EvaluationReport(
            group=self.group,
            engine_name=strategy_id,
            strategy_label=strategy_label or strategy_id,
            timestamp=pd.Timestamp.now().isoformat(),
            total_return=round(trace.total_return_pct, 2),
            excess_return=round(trace.total_return_pct - primary_return, 2),
            max_drawdown=round(trace.max_drawdown_pct, 2),
            sharpe_ratio=round(trace.sharpe_ratio, 4),
            trade_count=int(trace.total_trades),
            avg_cash_pct=round(100.0 - trace.avg_position_pct, 2),
            pending_order_count=int(trace.pending_order_count),
            initial_asset=round(float(self.execution.initial_capital), 2),
            final_asset=round(final_asset, 2),
            final_cash=round(float(trace.final_cash), 2),
            final_holdings_value=round(holdings_value, 2),
            final_position_pct=round(trace.final_position_pct, 2),
            final_holdings=holdings,
            benchmark_returns=benchmark_returns,
            benchmark_win_rates=benchmark_win_rates,
            benchmark_excess_returns=benchmark_excess_returns,
            benchmark_details=benchmark_details,
            benchmark_raw_returns=benchmark_raw_returns,
            primary_benchmark=primary_benchmark,
            composition=list(market_data.symbols),
            nav_series=list(trace.nav_series),
            nav_dates=list(trace.nav_dates),
            weekly_nav_ohlc=_weekly_nav_ohlc(trace.nav_dates, trace.nav_series),
            quarterly_holdings=list(trace.quarterly_holdings),
        )


def _build_signal_plan(
    stocks_data: dict[str, pd.DataFrame],
    active_codes: list[str],
    strategy,  # TradingStrategy
    params: Params,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[
    TradePlan | None,
    StrategyMarketData | None,
    ExecutionPriceSlice | None,
    list[str],
]:
    """Build the canonical decision plan for one market basket.

    Returns:
        ``(trade_plan, market_data, execution_prices, codes)``.  Strategies
        receive only market inputs and decisions; execution prices are built
        once for the exact evaluation slice and owned by :class:`Backtester`.
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
        return None, None, None, list(active_codes)

    # 3. The same plan is used in the optimizer and live scan.
    market_data = StrategyMarketData(
        indicator_matrix=ind_mat,
        dates=list(dates),
        symbols=list(active_codes),
        prices=price,
        highs=ind_mat[:, :, IDX_HIGH],
        lows=ind_mat[:, :, IDX_LOW],
        tradable=tradable,
    )
    trade_plan = strategy.make_signals(params, market_data)

    # 5. 仿真 (threshold=0.5 对 bool→float 天然等价)
    window_start = 0
    window_end = len(dates)
    if start_date is not None or end_date is not None:
        date_index = pd.DatetimeIndex(pd.to_datetime(dates))
        date_mask = np.ones(len(date_index), dtype=bool)
        if start_date is not None:
            date_mask &= date_index >= pd.Timestamp(start_date)
        if end_date is not None:
            date_mask &= date_index <= pd.Timestamp(end_date)
        selected = np.flatnonzero(date_mask)
        if len(selected):
            window_start = int(selected[0])
            window_end = int(selected[-1]) + 1
            trade_plan = trade_plan.sliced(window_start, window_end)
        else:
            return None, None, None, list(active_codes)
        execution_prices = DEFAULT_FILL_PRICE_POLICY.build(
            price,
            market_data.highs,
            market_data.lows,
            start=window_start,
            end=window_end,
        )
        ind_mat = ind_mat[date_mask]
        price = price[date_mask]
        highs = market_data.highs[date_mask]
        lows = market_data.lows[date_mask]
        tradable = tradable[date_mask]
        dates = [date for date, keep in zip(dates, date_mask) if keep]
    else:
        execution_prices = DEFAULT_FILL_PRICE_POLICY.build(
            price,
            market_data.highs,
            market_data.lows,
        )
        highs = market_data.highs
        lows = market_data.lows

    if not dates:
        return None, None, None, list(active_codes)

    report_market_data = StrategyMarketData(
        indicator_matrix=ind_mat,
        dates=list(dates),
        symbols=list(active_codes),
        prices=price,
        highs=highs,
        lows=lows,
        tradable=tradable,
    )
    return trade_plan, report_market_data, execution_prices, list(active_codes)


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


def _aligned_benchmark_ohlc(
    nav_dates: list[str],
    benchmark_df: pd.DataFrame | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Backward-align benchmark close/high without borrowing future quotes."""
    if (
        benchmark_df is None
        or benchmark_df.empty
        or "close" not in benchmark_df.columns
        or not nav_dates
    ):
        return np.array([], dtype=float), np.array([], dtype=float)
    try:
        target = pd.DataFrame(
            {"date": pd.to_datetime(nav_dates, errors="coerce")}
        ).dropna()
        benchmark = benchmark_df.copy()
        dates = (
            pd.to_datetime(benchmark["date"], errors="coerce")
            if "date" in benchmark.columns
            else pd.to_datetime(benchmark.index, errors="coerce")
        )
        close = pd.to_numeric(benchmark["close"], errors="coerce")
        high = (
            pd.to_numeric(benchmark["high"], errors="coerce")
            if "high" in benchmark.columns
            else close
        )
        source = pd.DataFrame(
            {"date": dates, "close": close, "high": high}
        ).dropna()
        source = source[
            (source["close"] > 0) & (source["high"] > 0)
        ].drop_duplicates("date", keep="last").sort_values("date")
        if source.empty or len(target) != len(nav_dates):
            return np.array([], dtype=float), np.array([], dtype=float)
        aligned = pd.merge_asof(
            target.sort_values("date"),
            source,
            on="date",
            direction="backward",
        )
        if aligned[["close", "high"]].isna().any().any():
            return np.array([], dtype=float), np.array([], dtype=float)
        return (
            aligned["close"].to_numpy(dtype=float),
            aligned["high"].to_numpy(dtype=float),
        )
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning("Unable to align benchmark OHLC data: %s", exc)
        return np.array([], dtype=float), np.array([], dtype=float)


def _benchmark_entry_stress_price(
    trigger_date: str,
    benchmark_df: pd.DataFrame | None,
) -> float:
    """Resolve prior/current/next benchmark highs around the entry date."""
    if benchmark_df is None or benchmark_df.empty:
        return float("nan")
    try:
        frame = benchmark_df.copy()
        dates = (
            pd.to_datetime(frame["date"], errors="coerce")
            if "date" in frame.columns
            else pd.to_datetime(frame.index, errors="coerce")
        )
        high = pd.to_numeric(
            frame["high"] if "high" in frame.columns else frame["close"],
            errors="coerce",
        )
        source = pd.DataFrame({"date": dates, "high": high}).dropna()
        source = source[source["high"] > 0].sort_values("date").reset_index(
            drop=True
        )
        trigger = pd.Timestamp(trigger_date)
        current_indexes = np.flatnonzero(
            source["date"].to_numpy(dtype="datetime64[ns]")
            <= trigger.to_datetime64()
        )
        next_indexes = np.flatnonzero(
            source["date"].to_numpy(dtype="datetime64[ns]")
            > trigger.to_datetime64()
        )
        if len(current_indexes) == 0 or len(next_indexes) == 0:
            return float("nan")
        current_index = int(current_indexes[-1])
        if current_index == 0:
            return float("nan")
        next_index = int(next_indexes[0])
        return float(
            source.loc[
                [current_index - 1, current_index, next_index], "high"
            ].max()
        )
    except (TypeError, ValueError, KeyError):
        return float("nan")


def _benchmark_stress_prices(
    trigger_dates: list[str],
    benchmark_df: pd.DataFrame | None,
) -> np.ndarray:
    """Build one pessimistic executable entry price for every start date."""
    result = np.full(len(trigger_dates), np.nan, dtype=float)
    if benchmark_df is None or benchmark_df.empty or not trigger_dates:
        return result
    try:
        frame = benchmark_df.copy()
        dates = (
            pd.to_datetime(frame["date"], errors="coerce")
            if "date" in frame.columns
            else pd.to_datetime(frame.index, errors="coerce")
        )
        high = pd.to_numeric(
            frame["high"] if "high" in frame.columns else frame["close"],
            errors="coerce",
        )
        source = pd.DataFrame({"date": dates, "high": high}).dropna()
        source = source[source["high"] > 0].sort_values("date").drop_duplicates(
            "date", keep="last"
        )
        source_dates = source["date"].to_numpy(dtype="datetime64[D]").astype(
            np.int64
        )
        source_high = source["high"].to_numpy(dtype=float)
        triggers = pd.to_datetime(
            trigger_dates, errors="coerce"
        ).values.astype("datetime64[D]").astype(np.int64)
        current = np.searchsorted(source_dates, triggers, side="right") - 1
        following = np.searchsorted(source_dates, triggers, side="right")
        valid = (
            (current > 0)
            & (following < len(source_dates))
        )
        rows = np.flatnonzero(valid)
        result[rows] = np.maximum(
            np.maximum(
                source_high[current[rows] - 1],
                source_high[current[rows]],
            ),
            source_high[following[rows]],
        )
    except (TypeError, ValueError, KeyError):
        return result
    return result


def _tradable_benchmark_detail(
    strategy_values: np.ndarray,
    close_prices: np.ndarray,
    buy_prices: np.ndarray,
    initial_cash: float,
    lot_size: int,
    commission_rate: float,
    validation_days: int = 9 * 21,
) -> dict[str, object] | None:
    """Evaluate a tradable baseline, re-entering with costs at every start."""
    strategy = np.asarray(strategy_values, dtype=float)
    closes = np.asarray(close_prices, dtype=float)
    buys = np.asarray(buy_prices, dtype=float)
    if closes.ndim == 1:
        closes = closes.reshape(-1, 1)
    if buys.ndim == 1:
        buys = buys.reshape(-1, 1)
    if (
        len(strategy) < 2
        or len(strategy) != len(closes)
        or buys.shape != closes.shape
        or not np.isfinite(buys[0]).any()
    ):
        return None
    full_nav = buy_and_hold_nav(
        closes,
        buys,
        initial_cash,
        lot_size,
        commission_rate,
    )
    benchmark_return = (full_nav[-1] / initial_cash - 1.0) * 100.0
    cut = max(0, len(strategy) - validation_days)
    wins = 0
    comparisons = 0
    final_prices = closes[-1]
    weights = np.full(closes.shape[1], 1.0 / closes.shape[1])
    for start in range(cut, len(strategy) - 1):
        if strategy[start] <= 0 or not np.isfinite(buys[start]).any():
            continue
        cash = float(initial_cash)
        final_value = 0.0
        for column in range(closes.shape[1]):
            execution_price = buys[start, column]
            final_price = final_prices[column]
            if (
                execution_price <= 0
                or final_price <= 0
                or not np.isfinite(execution_price)
                or not np.isfinite(final_price)
            ):
                continue
            budget = initial_cash * weights[column]
            quantity = int(
                budget
                / (execution_price * (1.0 + commission_rate))
                / lot_size
            ) * lot_size
            if quantity <= 0:
                continue
            cost = quantity * execution_price * (1.0 + commission_rate)
            if cost > cash:
                continue
            cash -= cost
            final_value += quantity * final_price
        strategy_forward = strategy[-1] / strategy[start] - 1.0
        benchmark_forward = (cash + final_value) / initial_cash - 1.0
        if not (
            np.isfinite(strategy_forward) and np.isfinite(benchmark_forward)
        ):
            continue
        wins += int(strategy_forward > benchmark_forward)
        comparisons += 1
    return {
        "benchmark_return": round(float(benchmark_return), 2),
        "strategy_excess_return": round(
            float((strategy[-1] / strategy[0] - 1.0) * 100.0)
            - float(benchmark_return),
            2,
        ),
        "win_rate": (
            round(wins / comparisons * 100.0, 2) if comparisons else None
        ),
        "win_days": wins,
        "comparison_days": comparisons,
        "entry_cost_model": "fee_and_pessimistic_high_per_start",
    }


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


def _benchmark_detail(
    strategy_values: np.ndarray,
    benchmark_values: np.ndarray,
    validation_days: int = 9 * 21,
) -> dict[str, object] | None:
    if len(strategy_values) < 2 or len(strategy_values) != len(benchmark_values):
        return None
    strategy_return = (strategy_values[-1] / strategy_values[0] - 1) * 100
    benchmark_return = (benchmark_values[-1] / benchmark_values[0] - 1) * 100
    cut = max(0, len(strategy_values) - validation_days)
    strategy_recent = strategy_values[cut:]
    benchmark_recent = benchmark_values[cut:]
    if len(strategy_recent) < 2:
        wins = total = 0
    else:
        strategy_forward = strategy_recent[-1] / strategy_recent[:-1] - 1
        benchmark_forward = benchmark_recent[-1] / benchmark_recent[:-1] - 1
        valid = np.isfinite(strategy_forward) & np.isfinite(benchmark_forward)
        total = int(valid.sum())
        wins = int((strategy_forward[valid] > benchmark_forward[valid]).sum())
    return {
        "benchmark_return": round(float(benchmark_return), 2),
        "strategy_excess_return": round(
            float(strategy_return - benchmark_return), 2
        ),
        "win_rate": round(wins / total * 100, 2) if total else None,
        "win_days": wins,
        "comparison_days": total,
    }


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
    recent = values[max(0, len(values) - validation_days):]
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


def _risk_free_detail(
    nav_series: list[float],
    annual_rate: float,
    validation_days: int = 9 * 21,
) -> dict[str, object] | None:
    values = np.asarray(nav_series, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if len(values) < 2:
        return None
    daily_rate = (1 + annual_rate) ** (1 / 252) - 1
    benchmark_return = ((1 + daily_rate) ** (len(values) - 1) - 1) * 100
    strategy_return = (values[-1] / values[0] - 1) * 100
    recent = values[max(0, len(values) - validation_days):]
    offsets = np.arange(len(recent) - 1, 0, -1)
    strategy_forward = recent[-1] / recent[:-1] - 1
    benchmark_forward = (1 + daily_rate) ** offsets - 1
    valid = np.isfinite(strategy_forward)
    total = int(valid.sum())
    wins = int((strategy_forward[valid] > benchmark_forward[valid]).sum())
    return {
        "benchmark_return": round(float(benchmark_return), 2),
        "strategy_excess_return": round(
            float(strategy_return - benchmark_return), 2
        ),
        "win_rate": round(wins / total * 100, 2) if total else None,
        "win_days": wins,
        "comparison_days": total,
    }


def _weekly_nav_ohlc(nav_dates: list[str], nav_series: list[float]) -> dict[str, list]:
    """Aggregate daily NAV into natural Monday-Sunday OHLC bars."""
    if not nav_dates or len(nav_dates) != len(nav_series):
        return {}
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(nav_dates, errors="coerce"),
            "nav": pd.to_numeric(nav_series, errors="coerce"),
        }
    ).dropna()
    if frame.empty:
        return {}
    frame = frame.sort_values("date")
    frame["week"] = frame["date"].dt.to_period("W-SUN")
    grouped = frame.groupby("week", sort=True)["nav"]
    bars = grouped.agg(["first", "max", "min", "last"])
    return {
        "labels": [str(period) for period in bars.index],
        "open": [round(float(value), 2) for value in bars["first"]],
        "high": [round(float(value), 2) for value in bars["max"]],
        "low": [round(float(value), 2) for value in bars["min"]],
        "close": [round(float(value), 2) for value in bars["last"]],
    }


def _final_holdings(trace: PortfolioTrace, codes: list[str]) -> list[dict]:
    shares = np.asarray(trace.final_shares, dtype=float)
    costs = np.asarray(trace.cost_basis, dtype=float)
    prices = np.asarray(trace.final_prices, dtype=float)
    if not (len(shares) == len(costs) == len(prices) == len(codes)):
        return []
    final_asset = float(trace.daily_values[-1]) if len(trace.daily_values) else 0.0
    holdings = []
    for code, quantity, cost, price in zip(codes, shares, costs, prices):
        if quantity <= 0 or not np.isfinite(price) or price <= 0:
            continue
        value = float(quantity * price)
        pnl = float((price - cost) * quantity)
        holdings.append(
            {
                "code": code,
                "shares": round(float(quantity), 4),
                "cost": round(float(cost), 4),
                "price": round(float(price), 4),
                "value": round(value, 2),
                "weight": round(value / final_asset * 100, 2) if final_asset else 0.0,
                "pnl": round(pnl, 2),
                "pnl_pct": round((price / cost - 1) * 100, 2) if cost > 0 else 0.0,
            }
        )
    return holdings


def evaluate_all_groups(
    stocks_data: dict[str, pd.DataFrame],
    stock_codes: list[str],
    strategy,  # TradingStrategy
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
        strategy: registered TradingStrategy instance
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

    engine_name = getattr(params, "_engine", "") or getattr(
        strategy, "name", "percentile")
    timestamp = datetime.now().isoformat()

    results: dict[str, EvaluationReport] = {}
    for group_name in target_groups:
        group_data = group_data_map.get(group_name, {})
        active_codes = list(group_data.keys())
        if not active_codes:
            continue

        fx = fx_map.get(group_name, 1.0)
        lot = lot_map.get(group_name, 100)
        trade_plan, market_data, execution_prices, codes = _build_signal_plan(
            group_data,
            active_codes,
            strategy,
            params,
            start_date,
            end_date,
        )
        if trade_plan is None or market_data is None:
            continue

        constraints = get_constraints()
        benchmark_codes = constraints.benchmark_codes_for(group_name)
        primary_benchmark = constraints.primary_benchmark_for(group_name)
        risk_free = RISK_FREE_A if group_name == "a_share" else RISK_FREE_NON_A
        execution = SimpleNamespace(
            initial_capital=float(exec_cfg.initial_capital),
            commission_rate=float(exec_cfg.commission_rate),
            min_holding_days=int(exec_cfg.min_holding_days),
            lot_sizes={group_name: int(lot)},
            fx_rates={group_name: float(fx)},
        )
        report = Backtester(execution, group_name).run(
            trade_plan,
            market_data,
            benchmark_data=benchmark_data or {},
            benchmark_codes=benchmark_codes,
            primary_benchmark=primary_benchmark,
            risk_free_rate=risk_free,
            strategy_id=engine_name,
            strategy_label=getattr(strategy, "label", engine_name),
            execution_prices=execution_prices,
        )
        report.timestamp = timestamp
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

        report.eligible_codes = eligible_codes
        report.warming_codes = warming_codes
        report.eligible_from = eligible_from
        results[group_name] = report
        logger.info(
            "%s evaluation complete: return=%.1f%% excess=%.1f%% "
            "drawdown=%.1f%% sharpe=%.2f trades=%d benchmarks=%s win_rates=%s",
            group_name,
            report.total_return,
            report.excess_return,
            report.max_drawdown,
            report.sharpe_ratio,
            report.trade_count,
            report.benchmark_returns,
            report.benchmark_win_rates,
        )

    return results
