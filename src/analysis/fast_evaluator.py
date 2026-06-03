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

# ── 构建器 → 条件/重置矩阵生成函数 ──
# 每个构建器返回 (condition_matrix, reset_matrix) 各为 (T, N) bool


def _build_deviation_cross(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """MA偏离穿越: 从上方穿越阈值 (买入)"""
    dev = indicator[:, :, IDX_DEVIATION]  # (T, N)
    t = -0.005 + threshold_norm * (-0.30 + 0.005)  # norm 0→1 maps to -0.005→-0.30
    prev = np.roll(dev, 1, axis=0)
    prev[0, :] = 0.0
    cond = (dev <= t) & (prev > t)
    reset = dev > 0
    return cond, reset


def _build_rsi_signal(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """RSI 超卖"""
    rsi = indicator[:, :, IDX_RSI]
    t = 10 + (1.0 - threshold_norm) * 30  # norm 0→1 maps to 40→10
    cond = rsi < t
    reset = rsi > 50
    return cond, reset


def _build_bollinger_signal(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """布林带 %B 低位"""
    bb = indicator[:, :, IDX_BOLL_PCT_B]
    t = 0.0 + (1.0 - threshold_norm) * 0.35  # norm 0→1 maps to 0.35→0.0
    cond = bb < t
    reset = bb > 0.5
    return cond, reset


def _build_volume_spike(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """放量异动 (仅买入)"""
    vr = indicator[:, :, IDX_VOL_RATIO]
    t = 1.2 + threshold_norm * 2.8  # norm 0→1 maps to 1.2→4.0
    cond = vr > t
    reset = vr < 1.0
    return cond, reset


def _build_deviation_absolute(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """MA绝对偏离 (不要求穿越)"""
    dev = indicator[:, :, IDX_DEVIATION]
    t = threshold_norm * -0.40  # norm 0→1 maps to 0→-0.40
    cond = dev <= t
    reset = dev > 0
    return cond, reset


def _build_trend_follow(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """趋势跟踪: ADX确认 + MACD方向"""
    adx = indicator[:, :, IDX_ADX]
    macd = indicator[:, :, IDX_MACD_HIST]
    t = 15 + threshold_norm * 25  # norm 0→1 maps to 15→40
    cond = (adx > t) & (macd > 0)
    reset = adx < 15
    return cond, reset


def _build_none(indicator: np.ndarray, threshold_norm: float) -> tuple[np.ndarray, np.ndarray]:
    """禁用 (永假)"""
    T, N = indicator.shape[:2]
    cond = np.zeros((T, N), dtype=bool)
    reset = np.ones((T, N), dtype=bool)
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
        signals,          # (T, N) bool — 买入信号
        prices,           # (T, N) float32
        rule_fracs,       # (R,) float32 — 各规则买入比例
        initial_cash,     # float
        monthly_limit,    # float
        lot_size,         # int — A股100, 非A股1
        commission_rate,  # float
    ):
        """numba 加速版组合模拟

        Returns:
            daily_values: (T,) float64 — 每日总资产
            total_trades: int
        """
        T, N = signals.shape
        shares = np.zeros(N, dtype=np.float64)
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

            if signals[t].any() and cash > 0:
                for n in range(N):
                    if not signals[t, n]:
                        continue
                    price = float(prices[t, n])
                    if price <= 0.0 or np.isnan(price):
                        continue

                    # 买入金额 = 平均规则比例 × 当前现金
                    avg_frac = 0.0
                    count = 0
                    for r in range(len(rule_fracs)):
                        if rule_fracs[r] > 0:
                            avg_frac += rule_fracs[r]
                            count += 1
                    if count > 0:
                        avg_frac /= count
                    else:
                        avg_frac = 0.10  # fallback

                    buy_amount = cash * avg_frac
                    remaining = monthly_limit - monthly_spent
                    buy_amount = min(buy_amount, remaining)

                    if buy_amount <= 0:
                        continue

                    # 忽略整手数约束（向量化层近似）
                    cost = buy_amount * (1.0 - commission_rate)
                    qty = cost / price
                    cost_real = qty * price
                    fee = cost_real * commission_rate
                    total_cost = cost_real + fee

                    if total_cost <= cash:
                        shares[n] += qty
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

        return daily_values, total_trades

    HAS_NUMBA = True
    logger.info("numba JIT 已启用，FastEvaluator 将使用加速内核")

except ImportError:
    HAS_NUMBA = False
    logger.warning(
        "numba 未安装，FastEvaluator 将使用纯 Python 回退。"
        "建议安装: pip install numba",
    )


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
    ):
        self.initial_cash = initial_cash
        self.monthly_buy_limit = monthly_buy_limit
        self.lot_size = lot_size
        self.commission_rate = commission_rate

    def evaluate(
        self,
        indicator_matrix: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        rule_builders: list[str],
        rule_thresholds: list[float],
        rule_fracs: list[float],
    ) -> "WindowStats":
        """评估单窗口单策略

        Args:
            indicator_matrix: (T, N, K) float32 指标矩阵
            price_matrix: (T, N) float32 价格矩阵
            cash_baseline: (T,) float64 现金基准线(含无风险复利)
            rule_builders: 每条规则的构建器名
            rule_thresholds: 每条规则的归一化阈值
            rule_fracs: 每条规则的买入比例

        Returns:
            WindowStats 包含各项测试期统计指标
        """
        R = len(rule_builders)
        T, N = indicator_matrix.shape[:2]
        if N == 0:
            return WindowStats()

        # ── 1. 构建条件/重置矩阵 ──
        conditions = np.zeros((R, T, N), dtype=bool)
        resets = np.zeros((R, T, N), dtype=bool)

        for r in range(R):
            builder_name = rule_builders[r]
            threshold = rule_thresholds[r]

            builder_fn = CONDITION_BUILDERS_FAST.get(
                builder_name, _build_none,
            )
            cond, rst = builder_fn(indicator_matrix, threshold)
            conditions[r] = cond
            resets[r] = rst

        # ── 2. 锁/重置状态机 → 实际信号 ──
        if HAS_NUMBA:
            signals, signal_count = _apply_lock_reset_numba(conditions, resets)
        else:
            signals, signal_count = _apply_lock_reset(conditions, resets)

        # ── 3. 组合模拟 → 日资产 ──
        fracs_arr = np.array(rule_fracs, dtype=np.float32)
        if HAS_NUMBA:
            daily_values, trade_count = _simulate_portfolio_numba(
                signals, price_matrix, fracs_arr,
                float(self.initial_cash), float(self.monthly_buy_limit),
                self.lot_size, float(self.commission_rate),
            )
        else:
            daily_values, trade_count = _simulate_portfolio_python(
                signals, price_matrix, fracs_arr,
                self.initial_cash, self.monthly_buy_limit,
                self.lot_size, self.commission_rate,
            )

        # ── 4. 计算指标 ──
        return self._compute_stats(daily_values, price_matrix, cash_baseline, trade_count, signal_count)

    def _compute_stats(
        self,
        daily_values: np.ndarray,
        price_matrix: np.ndarray,
        cash_baseline: np.ndarray,
        trade_count: int,
        signal_count: int,
    ) -> "WindowStats":
        """从日资产序列计算 WindowStats"""
        from .optimizer_constraints import WindowStats

        T = len(daily_values)
        if T < 2:
            return WindowStats()

        initial_val = daily_values[0]
        final_val = daily_values[-1]
        total_return = (final_val - initial_val) / initial_val * 100.0 if initial_val > 0 else 0.0

        # 现金基准超额收益
        bench_initial = cash_baseline[0]
        bench_final = cash_baseline[-1]
        bench_return = (bench_final - bench_initial) / bench_initial * 100.0 if bench_initial > 0 else 0.0
        excess_return = total_return - bench_return

        # 最大回撤
        peak = np.maximum.accumulate(daily_values)
        drawdown = (daily_values - peak) / peak * 100.0
        max_dd = float(np.min(drawdown))

        # 平均仓位率 (估算)
        # position_value ≈ daily_values - remaining_cash
        # 简化: 用 daily_values 的波动性 vs 价格波动来估计
        # 更精确的做法: 直接用 _simulate 中的 shares 计算, 但这里用近似
        # 简化: 如果 daily_values 远大于初始现金, 说明仓位重
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
    signals,
    prices,
    rule_fracs,
    initial_cash,
    monthly_limit,
    lot_size,
    commission_rate,
):
    """纯 Python 回退, 与 numba 版本逻辑一致"""
    T, N = signals.shape
    shares = np.zeros(N, dtype=np.float64)
    cash = float(initial_cash)
    daily_values = np.zeros(T, dtype=np.float64)
    monthly_spent = 0.0
    current_month = -1
    total_trades = 0

    avg_frac = float(np.mean(rule_fracs)) if len(rule_fracs) > 0 else 0.10

    for t in range(T):
        month = t // 21
        if month != current_month:
            monthly_spent = 0.0
            current_month = month

        for n in range(N):
            if signals[t, n] and cash > 0:
                price = float(prices[t, n])
                if price <= 0 or np.isnan(price):
                    continue

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
                    shares[n] += qty
                    cash -= total_cost
                    monthly_spent += total_cost
                    total_trades += 1

        pos_value = sum(
            shares[n] * float(prices[t, n])
            for n in range(N)
            if not np.isnan(prices[t, n]) and prices[t, n] > 0
        )
        daily_values[t] = cash + pos_value

    return daily_values, total_trades
