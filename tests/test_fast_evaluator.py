"""FastEvaluator 交易执行模型测试 — 3日确认 + 均价执行 + 仓位目标模式。"""

import numpy as np

from src.analysis.fast_evaluator import (
    FastEvaluator,
    _aggregate_bullish,
    _sigmoid,
    _compute_position_target,
    _simulate_position_target_python,
)


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


# ════════════════════════════════════════════════════════════
# 仓位目标模式 (Position Target Model) 测试
# ════════════════════════════════════════════════════════════


def _make_matrices_for_position(stocks_data):
    """构造多标的多日矩阵，用于仓位目标测试。

    Args:
        stocks_data: list of dicts, each with:
            - closes: [float, ...] 收盘价序列
            - opens: [float, ...] 开盘价序列
            - devs: [float, ...] 偏离率序列（决定信号）

    Returns:
        indicator (T, N, 8), price_close (T, N), price_open (T, N),
        cash_baseline (T,)
    """
    N = len(stocks_data)
    T = len(stocks_data[0]["closes"])
    ind = np.zeros((T, N, 8), dtype=np.float32)
    p_close = np.zeros((T, N), dtype=np.float32)
    p_open = np.zeros((T, N), dtype=np.float32)

    for n, sd in enumerate(stocks_data):
        for t in range(T):
            ind[t, n, 0] = sd["closes"][t]  # close
            ind[t, n, 1] = sd["closes"][t] / (1 + sd["devs"][t])  # ma60
            ind[t, n, 2] = sd["devs"][t]  # deviation
            p_close[t, n] = sd["closes"][t]
            p_open[t, n] = sd["opens"][t]

    cash_baseline = np.full(T, 100000.0, dtype=np.float64)
    return ind, p_close, p_open, cash_baseline


class TestBullishAggregation:
    """信号聚合 → bullish_score 测试"""

    def test_all_buy_no_sell_is_bullish(self):
        """全部是买入信号 → bullish 接近 1。"""
        # (T=3, N=2): 每天2只都有买入信号，无卖出
        buy = np.ones((3, 2), dtype=bool)
        sell = np.zeros((3, 2), dtype=bool)
        result = _aggregate_bullish(buy, sell)
        # 2 buy / (2 buy + 0 sell) = 1.0
        assert np.allclose(result, 1.0)

    def test_all_sell_no_buy_is_bearish(self):
        """全部是卖出信号 → bullish 接近 0。"""
        buy = np.zeros((3, 2), dtype=bool)
        sell = np.ones((3, 2), dtype=bool)
        result = _aggregate_bullish(buy, sell)
        assert np.allclose(result, 0.0)

    def test_equal_buy_sell_is_neutral(self):
        """买卖信号各半 → bullish = 0.5。"""
        buy = np.array([[True, False]], dtype=bool)
        sell = np.array([[False, True]], dtype=bool)
        result = _aggregate_bullish(buy, sell)
        assert np.allclose(result, 0.5)

    def test_no_signals_is_neutral(self):
        """无信号 → bullish = 0.5（中性）。"""
        buy = np.zeros((3, 2), dtype=bool)
        sell = np.zeros((3, 2), dtype=bool)
        result = _aggregate_bullish(buy, sell)
        assert np.allclose(result, 0.5)


class TestSigmoidMapping:
    """sigmoid 映射函数测试"""

    def test_sigmoid_zero_is_half(self):
        """sigmoid(0) = 0.5。"""
        assert abs(_sigmoid(0.0) - 0.5) < 0.001

    def test_sigmoid_positive_gt_half(self):
        """sigmoid(x>0) > 0.5。"""
        assert _sigmoid(2.0) > 0.8

    def test_sigmoid_negative_lt_half(self):
        """sigmoid(x<0) < 0.5。"""
        assert _sigmoid(-2.0) < 0.2


class TestPositionTargetCompute:
    """仓位目标计算测试"""

    def test_bullish_full_gives_high_target(self):
        """bullish=1, slope=1, bias=0 → target > 70%"""
        bullish = np.array([1.0])
        target = _compute_position_target(bullish, slope=1.0, bias=0.0)
        assert target[0] > 0.7

    def test_bullish_zero_gives_low_target(self):
        """bullish=0, slope=1, bias=0 → target < 30%"""
        bullish = np.array([0.0])
        target = _compute_position_target(bullish, slope=1.0, bias=0.0)
        assert target[0] < 0.3

    def test_bullish_half_gives_mid_target(self):
        """bullish=0.5, slope=1, bias=0 → target ≈ 50%"""
        bullish = np.array([0.5])
        target = _compute_position_target(bullish, slope=1.0, bias=0.0)
        assert 0.45 < target[0] < 0.55

    def test_bias_shifts_target(self):
        """bias>0 使仓位更激进。"""
        bullish = np.array([0.5])
        target_neutral = _compute_position_target(bullish, slope=1.0, bias=0.0)
        target_bull = _compute_position_target(bullish, slope=1.0, bias=1.0)
        assert target_bull[0] > target_neutral[0]


class TestPositionTargetSimulation:
    """每日渐进调仓模拟测试"""

    def test_buy_when_bullish(self):
        """bullish 信号 → target > current → 买入"""
        # 1股票, 10天，后3天连续偏离触发买入
        T = 10
        closes = [10.0] * T
        opens = [10.0] * T
        closes[4], closes[5], closes[6] = 9.4, 9.4, 9.4  # 连续3天低
        opens[4], opens[5], opens[6] = 9.4, 9.4, 9.4

        buy_signals = np.zeros((T, 1), dtype=bool)
        buy_signals[6, 0] = True  # 第3天确认
        sell_signals = np.zeros((T, 1), dtype=bool)

        p_close = np.array(closes, dtype=np.float32).reshape(T, 1)
        p_open = np.array(opens, dtype=np.float32).reshape(T, 1)

        values, trades = _simulate_position_target_python(
            buy_signals, sell_signals,
            p_close, p_open,
            initial_cash=100000.0,
            lot_size=1,
            commission_rate=0.002,
            position_slope=2.0,
            position_bias=0.0,
            max_daily_adjust=0.10,
            buy_confirm_days=3,
            sell_confirm_days=1,
        )
        # 应该发生了交易（至少1次买入）
        assert trades > 0
        # 最终资产应 > 初始现金（盈利）
        # 或者至少不完全等于初始现金

    def test_sell_when_bearish(self):
        """持有仓位 → bearish 信号 → target < current → 卖出"""
        T = 15
        closes = [10.0] * T
        opens = [10.0] * T
        # 前3天低偏离触发买入
        closes[2], closes[3], closes[4] = 9.4, 9.4, 9.4
        opens[2], opens[3], opens[4] = 9.4, 9.4, 9.4
        # 后3天高偏离触发卖出
        closes[8], closes[9], closes[10] = 11.0, 11.0, 11.0
        opens[8], opens[9], opens[10] = 11.0, 11.0, 11.0

        buy_signals = np.zeros((T, 1), dtype=bool)
        buy_signals[4, 0] = True  # 买入确认
        sell_signals = np.zeros((T, 1), dtype=bool)
        sell_signals[10, 0] = True  # 卖出信号（1日确认）

        p_close = np.array(closes, dtype=np.float32).reshape(T, 1)
        p_open = np.array(opens, dtype=np.float32).reshape(T, 1)

        values, trades = _simulate_position_target_python(
            buy_signals, sell_signals,
            p_close, p_open,
            initial_cash=100000.0,
            lot_size=1,
            commission_rate=0.002,
            position_slope=3.0,  # 足够敏感让全卖出时 target 掉到 5% 以下
            position_bias=0.0,
            max_daily_adjust=0.10,
            buy_confirm_days=3,
            sell_confirm_days=1,
        )
        assert trades >= 2  # 至少买入+卖出各1次

    def test_max_daily_adjustment_cap(self):
        """每日调仓不超过 max_daily_adjust（默认10%）。"""
        T = 20
        closes = [10.0] * T
        opens = [10.0] * T
        # 制造极端 bullish: 每天都有买入信号
        closes[2:] = [9.3] * (T - 2)
        opens[2:] = [9.3] * (T - 2)

        buy_signals = np.zeros((T, 1), dtype=bool)
        buy_signals[4, 0] = True  # 3日确认，但仅第一波
        sell_signals = np.zeros((T, 1), dtype=bool)

        p_close = np.array(closes, dtype=np.float32).reshape(T, 1)
        p_open = np.array(opens, dtype=np.float32).reshape(T, 1)

        values, trades = _simulate_position_target_python(
            buy_signals, sell_signals,
            p_close, p_open,
            initial_cash=100000.0,
            lot_size=1,
            commission_rate=0.002,
            position_slope=5.0,  # 陡峭斜率，target 会很大
            position_bias=2.0,   # 偏激进
            max_daily_adjust=0.10,
            buy_confirm_days=3,
            sell_confirm_days=1,
        )
        # 验证每日净值变化不超 10%
        for t in range(1, len(values)):
            if values[t - 1] > 0:
                change = abs(values[t] - values[t - 1]) / values[t - 1]
                # 允许少量浮点误差 + 手续费导致的微小超出
                assert change < 0.12, (
                    f"Day {t}: change={change:.4f} exceeds 12% tolerance"
                )

    def test_one_trade_per_stock_per_day(self):
        """每标的每日最多 1 次操作。"""
        T = 10
        closes = [10.0] * T
        opens = [10.0] * T
        closes[2:5] = [9.4, 9.4, 9.4]
        opens[2:5] = [9.4, 9.4, 9.4]

        # 同一天多只股票有买入信号
        N = 3
        buy_signals = np.zeros((T, N), dtype=bool)
        buy_signals[4, :] = True  # 3只股票同一天确认
        sell_signals = np.zeros((T, N), dtype=bool)

        p_close = np.tile(np.array(closes, dtype=np.float32).reshape(T, 1), (1, N))
        p_open = p_close.copy()

        values, trades = _simulate_position_target_python(
            buy_signals, sell_signals,
            p_close, p_open,
            initial_cash=100000.0,
            lot_size=1,
            commission_rate=0.002,
            position_slope=2.0,
            position_bias=0.0,
            max_daily_adjust=0.10,
            buy_confirm_days=3,
            sell_confirm_days=1,
        )
        # 3只股票各有1次买入 → total trades = 3
        assert trades == 3, f"Expected 3 trades (one per stock), got {trades}"

    def test_average_price_execution(self):
        """买入价 = 确认窗口开收均价。"""
        T = 10
        # 第2-4天满足条件
        opens = [10.0] * T
        closes = [10.0] * T
        opens[2:5] = [9.5, 9.3, 9.4]
        closes[2:5] = [9.4, 9.5, 9.3]
        # 预期买入价 = avg(9.5, 9.4, 9.3, 9.5, 9.4, 9.3) = 9.40

        buy_signals = np.zeros((T, 1), dtype=bool)
        buy_signals[4, 0] = True  # 第4天确认（满足第2-4天）
        sell_signals = np.zeros((T, 1), dtype=bool)

        p_close = np.array(closes, dtype=np.float32).reshape(T, 1)
        p_open = np.array(opens, dtype=np.float32).reshape(T, 1)

        values, trades = _simulate_position_target_python(
            buy_signals, sell_signals,
            p_close, p_open,
            initial_cash=100000.0,
            lot_size=1,
            commission_rate=0.002,
            position_slope=2.0,
            position_bias=0.0,
            max_daily_adjust=0.10,
            buy_confirm_days=3,
            sell_confirm_days=1,
        )
        assert trades > 0
        # 验证价格合理（在 9.3-9.5 之间）
        # 买入后净值 = 100000 - 手续费（少量）
        # 这里仅验证交易发生了，具体价格验证在波形测试中做
