"""TDD: 简化搜参策略测试 — holding_days + buy_limits/sell_limits。

测试驱动实现顺序:
  1. FastEvaluator 接受 buy_limits/sell_limits 参数
  2. _simulate_portfolio_numba 追踪 holding_days 阻止未满期卖出
  3. 买入金额 = limit(元)，非 cash×frac
  4. StrategyEncoding + 遗传操作支持 simplified 模式
"""

import numpy as np
import pytest

from src.analysis.fast_evaluator import FastEvaluator
from src.analysis.optimizer_constraints import (
    DiscreteSearchConfig,
    StrategyConstraints,
    WindowStats,
    load_constraints,
)
from src.analysis.genetic_searcher import (
    GeneticSearcher,
    StrategyEncoding,
)

# ── 辅助函数：构造最小测试数据 ──


def _make_indicator(T: int, prices: list, deviations: list) -> np.ndarray:
    """构造 (T, 1, 8) 指标矩阵，price 和 deviation 为唯二有意义的列。"""
    ind = np.zeros((T, 1, 8), dtype=np.float32)
    for t in range(T):
        ind[t, 0, 0] = prices[t]  # close
        ind[t, 0, 1] = prices[t] / (1 + deviations[t]) if deviations[t] != -1 else 1.0
        ind[t, 0, 2] = deviations[t]  # deviation
    return ind


def _make_price(T: int, prices: list) -> np.ndarray:
    return np.array(prices, dtype=np.float32).reshape(T, 1)


# ════════════════════════════════════════════════════════════
# 测试组 1: 持股天数阻止卖出
# ════════════════════════════════════════════════════════════


class TestHoldingDays:
    """验证 min_holding_days 参数阻止过早卖出。"""

    T = 60  # 60 天足够
    PRICE = 10.0

    @pytest.fixture
    def base_data(self):
        """构造基础数据：Day5 买入，Day15/40 各有卖出信号位置。"""
        prices = [self.PRICE] * self.T
        deviations = [0.0] * self.T
        # Day4-6 满足买入条件（连续3天偏离 < -0.05 → 触发买入确认）
        for t in [4, 5, 6]:
            deviations[t] = -0.06
        # sell deviation 在 indicator 中后设（需 >=0.15 才触发 sell_deviation_absolute）

        ind = _make_indicator(self.T, prices, deviations)
        p_close = _make_price(self.T, prices)
        cash_baseline = np.full(self.T, 100000.0, dtype=np.float64)
        return ind, p_close, cash_baseline

    def test_early_sell_blocked_by_holding_days(self, base_data):
        """Day5 买入，Day15 卖出信号 → 持股不足 30 天，不卖。"""
        ind, p_close, cash_baseline = base_data
        # 对卖出的 deviation 增幅（sell_deviation_absolute: t=0.3*0.50=0.15, 需 dev>=0.15）
        # 仅在 Day14-16 设卖出信号（早期，应被阻止）
        for t in [14, 15, 16]:
            ind[t, 0, 2] = 0.16

        ev = FastEvaluator(
            initial_cash=100000,
            min_holding_days=30,
            buy_confirmation_days=3,
            sell_confirmation_days=3,
        )
        stats = ev.evaluate(
            ind,
            p_close,
            cash_baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_limits=[20000.0],
            sell_builders=["sell_deviation_absolute"],
            sell_thresholds=[0.3],
            sell_limits=[20000.0],
        )

        # 应有买入（Day5-7），但不应卖出（持股 ~10 天 < 30 天）
        assert stats.total_trades == 1, (
            f"应仅有买入, 实际 trades={stats.total_trades}"
        )
        assert stats.final_position_pct > 0, "持股不足30天不应卖出"

    def test_late_sell_allowed_by_holding_days(self, base_data):
        """Day5 买入，Day40 卖出信号 → 持股 >= 30 天，可卖。"""
        ind, p_close, cash_baseline = base_data
        # 对卖出的 deviation 增幅（需要 dev >= 0.15）
        for t in [14, 15, 16]:
            ind[t, 0, 2] = 0.16
        for t in [39, 40, 41]:
            ind[t, 0, 2] = 0.16

        ev = FastEvaluator(
            initial_cash=100000,
            min_holding_days=30,
            buy_confirmation_days=3,
            sell_confirmation_days=3,
        )
        stats = ev.evaluate(
            ind,
            p_close,
            cash_baseline,
            buy_builders=["deviation_absolute"],
            buy_thresholds=[0.125],
            buy_limits=[20000.0],
            sell_builders=["sell_deviation_absolute"],
            sell_thresholds=[0.3],
            sell_limits=[20000.0],
        )

        assert stats.total_trades >= 1
        # 持股 >=30 天后触发卖出
        assert stats.total_trades >= 2, (
            f"应有买入和卖出, 实际 trades={stats.total_trades}"
        )


# ════════════════════════════════════════════════════════════
# 测试组 2: buy_limits 决定买入金额（非 cash×frac）
# ════════════════════════════════════════════════════════════


class TestBuyLimits:
    """验证买入金额 = buy_limits（元），非持仓比例。"""

    T = 30

    def test_buy_amount_equals_limit(self):
        """限额 5000 → 买入 ~500 股 (5000/10)，非 cash×frac 方式。"""
        prices = [10.0] * self.T
        deviations = [0.0] * self.T
        for t in [3, 4, 5]:
            deviations[t] = -0.06

        ind = _make_indicator(self.T, prices, deviations)
        p_close = _make_price(self.T, prices)
        cash_baseline = np.full(self.T, 100000.0, dtype=np.float64)

        ev_large = FastEvaluator(
            initial_cash=100000, buy_confirmation_days=3
        )
        stats_large = ev_large.evaluate(
            ind, p_close, cash_baseline,
            buy_builders=["deviation_absolute"], buy_thresholds=[0.125],
            buy_limits=[20000.0],
        )
        # 重新构造独立测试（避免 FastEvaluator 有状态）
        ev_small = FastEvaluator(
            initial_cash=100000, buy_confirmation_days=3
        )
        stats_small = ev_small.evaluate(
            ind, p_close, cash_baseline,
            buy_builders=["deviation_absolute"], buy_thresholds=[0.125],
            buy_limits=[5000.0],
        )

        # 限额大的买入金额应 > 限额小的
        # 验证：large 的最终持仓市值 > small 的
        large_pos_val = stats_large.final_position_pct / 100.0 * 100000
        small_pos_val = stats_small.final_position_pct / 100.0 * 100000
        assert (
            large_pos_val > small_pos_val
        ), f"限额20000的持仓({large_pos_val:.0f})应大于限额5000({small_pos_val:.0f})"

    def test_buy_limits_independent_of_cash(self):
        """限额固定 → 买入金额与初始现金量无关。"""
        prices = [10.0] * self.T
        deviations = [0.0] * self.T
        for t in [3, 4, 5]:
            deviations[t] = -0.06

        ind = _make_indicator(self.T, prices, deviations)
        p_close = _make_price(self.T, prices)
        cb_100k = np.full(self.T, 100000.0, dtype=np.float64)
        cb_200k = np.full(self.T, 200000.0, dtype=np.float64)

        ev_100k = FastEvaluator(initial_cash=100000, buy_confirmation_days=3)
        s1 = ev_100k.evaluate(
            ind, p_close, cb_100k,
            buy_builders=["deviation_absolute"], buy_thresholds=[0.125],
            buy_limits=[20000.0],
        )
        ev_200k = FastEvaluator(initial_cash=200000, buy_confirmation_days=3)
        s2 = ev_200k.evaluate(
            ind, p_close, cb_200k,
            buy_builders=["deviation_absolute"], buy_thresholds=[0.125],
            buy_limits=[20000.0],
        )

        # 同样限额 20000，不同现金，买入股数应相同
        pos1 = s1.final_position_pct / 100.0 * 100000
        pos2 = s2.final_position_pct / 100.0 * 200000
        # 仓位市值应都在 ~20000 左右
        assert abs(pos1 - 20000) < 5000, f"100k现金买入应约20000，实际{pos1:.0f}"
        assert abs(pos2 - 20000) < 5000, f"200k现金买入应约20000，实际{pos2:.0f}"


# ════════════════════════════════════════════════════════════
# 测试组 3: 向后兼容 — 旧参数仍可用
# ════════════════════════════════════════════════════════════


class TestBackwardCompatibility:
    """验证新参数不影响旧模式。"""

    def test_old_frac_mode_still_works(self):
        """不传 buy_limits → 回退到 buy_fracs 行为。"""
        T = 30
        prices = [10.0] * T
        deviations = [0.0] * T
        for t in [3, 4, 5]:
            deviations[t] = -0.06

        ind = _make_indicator(T, prices, deviations)
        p_close = _make_price(T, prices)
        cash_baseline = np.full(T, 100000.0, dtype=np.float64)

        ev = FastEvaluator(initial_cash=100000, buy_confirmation_days=3)
        stats = ev.evaluate(
            ind, p_close, cash_baseline,
            buy_builders=["deviation_absolute"], buy_thresholds=[0.125],
            buy_fracs=[0.5],  # 旧参数
        )
        assert stats.total_trades >= 1, "旧 frac 模式应正常工作"


# ════════════════════════════════════════════════════════════
# 测试组 4: StrategyEncoding simplified 模式
# ════════════════════════════════════════════════════════════


class TestSimplifiedEncoding:
    """StrategyEncoding 在 simplified 模式下的编解码。"""

    def test_encoding_with_limits(self):
        """含 buy_limits/sell_limits 的编码可正确 flat 编解码。"""
        enc = StrategyEncoding(
            buy_builders=[0, 1, 2, 3, 4],
            buy_thresholds=[5, 6, 7, 8, 9],
            buy_fracs=[2, 3, 1, 0, 4],
            sell_builders=[0, 1, 2],
            sell_thresholds=[3, 4, 5],
            sell_fracs=[1, 2, 0],
        )
        flat = enc.to_flat()
        restored = StrategyEncoding.from_flat(flat, n_buy=5, n_sell=3)
        assert restored.buy_limits == [2, 3, 1, 0, 4]
        assert restored.sell_limits == [1, 2, 0]
        assert restored.buy_builders == [0, 1, 2, 3, 4]

    def test_to_simplified_params(self):
        """to_simplified_params 将索引映射为实际金额。"""
        cfg = DiscreteSearchConfig({"mode": "simplified"})
        enc = StrategyEncoding(
            buy_builders=[0, 1, 2, 3, 4],
            buy_thresholds=[5, 6, 7, 8, 9],
            buy_fracs=[0, 2, 0, 3, 4],
            sell_builders=[0, 1, 2],
            sell_thresholds=[3, 4, 5],
            sell_fracs=[0, 1, 2],  # 索引0→5000, 1→10000, 2→20000
        )
        buy_names, buy_thr, buy_limits = enc.to_simplified_params(cfg)
        sell_names, sell_thr, sell_limits = enc.to_simplified_params(cfg, side="sell")

        assert len(buy_limits) == 5
        assert len(sell_limits) == 3
        # 索引 0 → 第一档金额
        assert buy_limits[0] == cfg.buy_limit_levels[0]
        assert sell_limits[0] == cfg.sell_limit_levels[0]
        assert sell_limits[1] == cfg.sell_limit_levels[1]
