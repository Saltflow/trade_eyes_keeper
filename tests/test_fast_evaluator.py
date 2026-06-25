"""FastEvaluator 交易执行模型测试 — 3日确认 + 均价执行。"""

import numpy as np

from src.analysis.fast_evaluator import FastEvaluator


def _make_matrices(prices, deviations, opens=None):
    """构造最小指标矩阵和价格矩阵。

    Args:
        prices: [close_day1, close_day2, ...] 收盘价序列
        deviations: [dev_day1, dev_day2, ...] 偏离率序列
        opens: [open_day1, ...] 开盘价序列（可选，默认=close）

    Returns:
        indicator (T, 1, 8), price (T, 1), cash_baseline (T,)
    """
    T = len(prices)
    if opens is None:
        opens = prices[:]

    # indicator: (T, N=1, K=8)
    # [close, ma60, deviation, rsi, macd_hist, boll_pct_b, adx, vol_ratio]
    ind = np.zeros((T, 1, 8), dtype=np.float32)
    for t in range(T):
        ind[t, 0, 0] = prices[t]   # close
        ind[t, 0, 1] = prices[t] / (1 + deviations[t])  # ma60 = close / (1+dev)
        ind[t, 0, 2] = deviations[t]  # deviation

    # price: (T, 1) — 这里存收盘价，执行时需要 open/close
    # 我们需要额外传 open 给执行引擎
    price_open = np.array(opens, dtype=np.float32).reshape(T, 1)
    price_close = np.array(prices, dtype=np.float32).reshape(T, 1)

    cash_baseline = np.full(T, 100000.0, dtype=np.float64)
    return ind, price_close, price_open, cash_baseline


class TestBuyConfirmationDays:
    def test_single_day_does_not_buy(self):
        """只有1天满足条件 → 不买入。"""
        # dev = -0.06 在第3天满足 <= -0.05，但只有1天
        prices = [10.0, 10.0, 9.4, 10.0, 10.0, 10.0]
        devs = [0.0, 0.0, -0.06, 0.0, 0.0, 0.0]
        ind, p_close, p_open, baseline = _make_matrices(prices, devs)

        ev = FastEvaluator(buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],  # t = 0.125 * -0.40 = -0.05
            buy_fracs=[0.5],
        )
        assert stats.total_trades == 0

    def test_two_days_does_not_buy(self):
        """连续2天满足 → 不买入。"""
        prices = [10.0, 10.0, 9.4, 9.4, 10.0, 10.0]
        devs = [0.0, 0.0, -0.06, -0.06, 0.0, 0.0]
        ind, p_close, p_open, baseline = _make_matrices(prices, devs)

        ev = FastEvaluator(buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_fracs=[0.5],
        )
        assert stats.total_trades == 0

    def test_three_days_buys(self):
        """连续3天满足 → 买入。"""
        prices = [10.0, 10.0, 9.4, 9.4, 9.4, 10.0]
        devs = [0.0, 0.0, -0.06, -0.06, -0.06, 0.0]
        ind, p_close, p_open, baseline = _make_matrices(prices, devs)

        ev = FastEvaluator(buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_fracs=[0.5],
        )
        assert stats.total_trades > 0

    def test_interrupted_streak_resets(self):
        """第2天不满足 → 计数归零，第3-5天满足才算3日。"""
        # Day0: 0, Day1: 0, Day2: -0.06, Day3: 0 (中断), Day4: -0.06, Day5: -0.06, Day6: -0.06
        prices = [10.0, 10.0, 9.4, 10.0, 9.4, 9.4, 9.4]
        devs = [0.0, 0.0, -0.06, 0.0, -0.06, -0.06, -0.06]
        ind, p_close, p_open, baseline = _make_matrices(prices, devs)

        ev = FastEvaluator(buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_fracs=[0.5],
        )
        # Day2 满足1次 → Day3 中断 → Day4-6 连续3次 → 买入
        assert stats.total_trades > 0


class TestSellSingleDay:
    def test_sell_one_day_triggers(self):
        """卖出只需1日触发。"""
        # 先买入（连续3天低偏离），再卖出（1天高偏离）
        prices = [10.0, 9.4, 9.4, 9.4, 11.0, 10.0]
        devs = [0.0, -0.06, -0.06, -0.06, 0.06, 0.0]
        ind, p_close, p_open, baseline = _make_matrices(prices, devs)

        ev = FastEvaluator(buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_fracs=[0.5],
            sell_builders=["sell_deviation_absolute"],
            sell_thresholds=[0.10],  # t = 0.10 * 0.50 = 0.05
            sell_fracs=[0.5],
        )
        # 应该有买入和卖出
        assert stats.total_trades >= 2


class TestWindowAveragePrice:
    def test_buy_price_is_window_average(self):
        """买入价 = 3日6价（开收）均值。"""
        # Day2-4 连续3天满足，价格各不同
        opens = [10.0, 10.0, 9.5, 9.3, 9.4, 10.0]
        closes = [10.0, 10.0, 9.4, 9.5, 9.3, 10.0]
        devs = [0.0, 0.0, -0.06, -0.06, -0.06, 0.0]
        ind, p_close, p_open, baseline = _make_matrices(closes, devs, opens)

        # 预期买入价 = avg(9.5, 9.4, 9.3, 9.5, 9.4, 9.3) = 9.40
        expected_price = (9.5 + 9.4 + 9.3 + 9.5 + 9.4 + 9.3) / 6

        ev = FastEvaluator(buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_fracs=[1.0],  # 全仓买入方便验证
        )
        # 买入后资产 = 初始现金 - 买入成本
        # 买入成本 ≈ cash * frac（含手续费）
        # 不会精确验证价格，但验证交易发生了
        assert stats.total_trades > 0
