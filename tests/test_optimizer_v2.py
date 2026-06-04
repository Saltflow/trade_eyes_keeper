"""
V2 优化器测试套件

测试:
  1. 约束加载器 — 默认值 + 自定义 YAML
  2. WalkForwardManager — 窗口生成 + 矩阵构建
  3. FastEvaluator — 向量化信号生成 + 组合模拟
  4. 遗传搜索器 — 编码/解码 + 交叉/变异
  5. 端到端 — 小数据集全流程
"""

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── Fixtures ──


@pytest.fixture
def synthetic_stocks_data():
    """生成 3 年 / 3 只股票的合成数据"""
    np.random.seed(42)
    dates = pd.date_range("2023-01-01", periods=750, freq="B").strftime("%Y-%m-%d").tolist()
    stocks = {}
    for code in ["600001", "600002", "600003"]:
        # 模拟有趋势的价格
        trend = np.linspace(0, 0.3, 750) + np.random.randn(750).cumsum() * 0.01
        close = 10 * np.exp(trend)
        df = pd.DataFrame({
            "date": dates,
            "open": close * (1 + np.random.randn(750) * 0.005),
            "high": close * (1 + np.abs(np.random.randn(750) * 0.008)),
            "low": close * (1 - np.abs(np.random.randn(750) * 0.008)),
            "close": close,
            "volume": np.abs(np.random.randn(750)) * 1e6 + 5e5,
        })
        stocks[code] = df
    return stocks


@pytest.fixture
def constraints_config():
    """最小约束配置（便于测试通过）"""
    from src.analysis.optimizer_constraints import StrategyConstraints

    raw = {
        "hard_constraints": {
            "min_avg_position_pct": 0.0,     # 放宽：允许空仓
            "max_drawdown_pct": -99.0,       # 放宽：几乎不限制
            "max_return_std_pct": 100.0,     # 放宽
            "min_trades_per_month": 0,       # 放宽：允许无交易
            "max_trades_per_month": 999,     # 放宽
        },
        "walk_forward": {
            "train_months": 6,               # 缩短训练期
            "test_months": 3,                # 缩短测试期
            "step_months": 1,
            "num_windows": 3,                # 减少窗口
        },
        "genetic_search": {
            "phase1_random_samples": 50,
            "phase1_top_keep": 20,
            "num_generations": 1,
            "population_size": 20,
            "offspring_size": 30,
        },
        "discrete_search": {
            "num_buy_rules": 3,              # 减少规则数（加速）
        },
    }
    return StrategyConstraints(raw)


# ════════════════════════════════════════════════════════
#  1. 约束加载器
# ════════════════════════════════════════════════════════


class TestConstraintLoader:
    """测试约束加载"""

    def test_default_values(self):
        from src.analysis.optimizer_constraints import StrategyConstraints
        c = StrategyConstraints({})
        assert c.min_avg_position_pct == 20.0
        assert c.max_drawdown_pct == -25.0
        assert c.min_trades_per_month == 1
        assert c.walk_forward.train_months == 12

    def test_load_from_yaml(self):
        from src.analysis.optimizer_constraints import load_constraints

        yaml_content = """
hard_constraints:
  min_avg_position_pct: 15.0
  max_drawdown_pct: -30.0
  max_return_std_pct: 10.0
  min_trades_per_month: 2
  max_trades_per_month: 8
walk_forward:
  train_months: 10
  test_months: 8
  step_months: 2
  num_windows: 5
"""
        import yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = f.name

        try:
            c = load_constraints(path)
            assert c.min_avg_position_pct == 15.0
            assert c.walk_forward.train_months == 10
        finally:
            Path(path).unlink(missing_ok=True)

    def test_hard_constraint_filtering(self):
        """测试硬性约束过滤"""
        from src.analysis.optimizer_constraints import StrategyConstraints, WindowStats

        c = StrategyConstraints({
            "hard_constraints": {
                "min_avg_position_pct": 10.0,
                "max_drawdown_pct": -20.0,
                "max_return_std_pct": 5.0,
                "min_trades_per_month": 1,
                "max_trades_per_month": 5,
            },
        })

        # 好的策略（全部通过）
        good_stats = [
            WindowStats(
                test_excess_return=5.0, max_drawdown_pct=-5.0,
                avg_position_pct=50.0, total_trades=9, test_months=3,
            ),
        ]
        passes, violations = c.check_hard_constraints(good_stats, 5.0)
        assert passes
        assert len(violations) == 0

        # 坏策略（仓位太低）
        bad_stats = [
            WindowStats(
                test_excess_return=0.0, max_drawdown_pct=-5.0,
                avg_position_pct=5.0, total_trades=3, test_months=3,
            ),
        ]
        passes, violations = c.check_hard_constraints(bad_stats, 0.0)
        assert not passes
        assert any("仓位" in v for v in violations)

    def test_soft_penalty(self):
        from src.analysis.optimizer_constraints import StrategyConstraints
        c = StrategyConstraints({
            "soft_constraints": {
                "min_sharpe": 0.5,
                "sharpe_penalty_weight": 0.3,
            },
        })
        # 夏普低于阈值
        penalty = c.compute_soft_penalty(0.2)
        assert penalty > 0
        # 夏普高于阈值
        penalty = c.compute_soft_penalty(1.0)
        assert penalty == 0.0


# ════════════════════════════════════════════════════════
#  2. Walk-Forward 管理器
# ════════════════════════════════════════════════════════


class TestWalkForwardManager:
    """测试 Walk-Forward 窗口管理器"""

    def test_creates_windows(self, synthetic_stocks_data):
        from src.analysis.walk_forward import WalkForwardManager
        mgr = WalkForwardManager(
            synthetic_stocks_data,
            train_months=6, test_months=3, step_months=1, num_windows=3,
        )
        windows = mgr.iter_windows()
        # 750天 ≈ 35个月，至少应生成3个窗口
        assert len(windows) >= 1
        # 每个窗口有日期
        for w in windows:
            assert w.train_days > 0
            assert w.test_days > 0

    def test_build_matrices(self, synthetic_stocks_data):
        from src.analysis.walk_forward import WalkForwardManager
        mgr = WalkForwardManager(
            synthetic_stocks_data,
            train_months=6, test_months=3, step_months=1, num_windows=3,
        )
        windows = mgr.iter_windows()
        if not windows:
            pytest.skip("No windows generated (data too short)")

        w = windows[0]
        train_mat = mgr.build_matrices(w, "train")
        test_mat = mgr.build_matrices(w, "test")

        assert train_mat.ndim == 3  # (T, N, K)
        assert test_mat.ndim == 3
        assert train_mat.shape[1] == mgr.n_stocks  # N 只股票

    def test_price_matrix(self, synthetic_stocks_data):
        from src.analysis.walk_forward import WalkForwardManager
        mgr = WalkForwardManager(
            synthetic_stocks_data,
            train_months=6, test_months=3, step_months=1, num_windows=3,
        )
        windows = mgr.iter_windows()
        if not windows:
            pytest.skip("No windows")

        price = mgr.get_price_matrix(windows[0])
        assert price.ndim == 2  # (T, N)
        assert price.shape[1] == mgr.n_stocks


# ════════════════════════════════════════════════════════
#  3. 向量化快速评估器
# ════════════════════════════════════════════════════════


class TestFastEvaluator:
    """测试向量化快速评估器"""

    def test_signal_generation(self):
        """测试基本信号生成"""
        from src.analysis.fast_evaluator import (
            FastEvaluator,
            _build_bollinger_signal,
            _build_volume_spike,
            _build_none,
        )

        T, N, K = 200, 3, 8
        ind = np.random.randn(T, N, K).astype(np.float32)
        # 设置一些明显的低位信号
        ind[:, :, 5] = 0.1  # boll_pct_b 很低
        ind[:, :, 7] = 2.0  # vol_ratio 很高
        ind[:, :, 0] = np.cumsum(np.random.randn(T, N), axis=0) + 100  # close

        cond, _ = _build_bollinger_signal(ind, 0.5)
        assert cond.shape == (T, N)
        # boll_pct_b=0.1 < 0.175 (大约), 应该生成大量信号
        assert cond.sum() > 0

    def test_evaluate_returns_stats(self):
        """测试评估器返回 WindowStats"""
        from src.analysis.fast_evaluator import FastEvaluator

        T, N, K = 200, 3, 8
        ind = np.random.randn(T, N, K).astype(np.float32)
        ind[:, :, 0] = np.cumsum(np.random.randn(T, N), axis=0) + 100  # close
        ind[:, :, 5] = 0.1  # boll_pct_b 很低 → 触发买入
        price = ind[:, :, 0].copy()
        cash_baseline = np.cumsum(np.ones(T) * 100000 * 0.02 / 252) + 100000

        ev = FastEvaluator(initial_cash=100000, monthly_buy_limit=999999)
        stats = ev.evaluate(
            ind, price, cash_baseline,
            ["bollinger_signal", "none", "none", "none", "none"],
            [0.5, 0.0, 0.0, 0.0, 0.0],
            [0.15, 0.0, 0.0, 0.0, 0.0],
        )

        assert stats is not None
        assert isinstance(stats.test_excess_return, float)

    def test_none_builder_no_signal(self):
        """测试 'none' 构建器不产生信号"""
        from src.analysis.fast_evaluator import _build_none

        T, N, K = 100, 2, 8
        ind = np.zeros((T, N, K), dtype=np.float32)
        cond, reset = _build_none(ind, 0.0)
        assert cond.sum() == 0  # 永远不触发


# ════════════════════════════════════════════════════════
#  4. 遗传搜索器
# ════════════════════════════════════════════════════════


class TestGeneticSearcher:
    """测试遗传搜索器"""

    def test_strategy_encoding(self):
        """策略编码 ↔ 解码"""
        from src.analysis.genetic_searcher import StrategyEncoding

        enc = StrategyEncoding(
            buy_builders=[0, 1, 2, 3, 4],
            buy_thresholds=[5, 2, 7, 0, 9],
            buy_fracs=[0, 1, 2, 3, 4],
            sell_builders=[0, 1, 1],
            sell_thresholds=[2, 3, 4],
            sell_fracs=[0, 1, 2],
        )
        flat = enc.to_flat()
        assert len(flat) == 24  # 5 buy + 3 sell = 8 rules × 3 dims

        decoded = StrategyEncoding.from_flat(flat, n_buy=5, n_sell=3)
        assert decoded.buy_builders == enc.buy_builders
        assert decoded.sell_builders == enc.sell_builders

    def test_to_rule_params(self):
        """编码 → FastEvaluator 参数"""
        from src.analysis.genetic_searcher import StrategyEncoding
        from src.analysis.optimizer_constraints import DiscreteSearchConfig

        cfg = DiscreteSearchConfig({
            "buy_builders": ["bollinger_signal", "none"],
            "threshold_levels": 10,
            "frac_levels": [0.05, 0.10],
            "num_buy_rules": 3,
            "sell_builders": ["sell_bollinger_signal", "none"],
            "sell_frac_levels": [0.20, 0.50],
            "num_sell_rules": 2,
        })

        enc = StrategyEncoding(
            buy_builders=[0, 1, 1],
            buy_thresholds=[3, 0, 9],
            buy_fracs=[0, 1, 0],
            sell_builders=[0, 1],
            sell_thresholds=[5, 2],
            sell_fracs=[0, 1],
        )
        buy_names, buy_vals, buy_fracs = enc.to_buy_params(cfg)
        assert buy_names == ["bollinger_signal", "none", "none"]
        assert len(buy_vals) == 3
        assert buy_fracs == [0.05, 0.10, 0.05]

        sell_names, sell_vals, sell_fracs = enc.to_sell_params(cfg)
        assert sell_names == ["sell_bollinger_signal", "none"]
        assert len(sell_vals) == 2
        assert sell_fracs == [0.20, 0.50]

    def test_crossover(self, synthetic_stocks_data, constraints_config):
        """交叉操作不抛异常"""
        from src.analysis.genetic_searcher import (
            GeneticSearcher, StrategyEncoding,
        )
        from src.analysis.walk_forward import WalkForwardManager
        from src.analysis.fast_evaluator import FastEvaluator

        wf_mgr = WalkForwardManager(
            synthetic_stocks_data,
            train_months=6, test_months=3, step_months=1, num_windows=3,
        )
        ev = FastEvaluator(initial_cash=100000, monthly_buy_limit=999999)

        searcher = GeneticSearcher(constraints_config, wf_mgr, ev)

        p1 = searcher._random_strategy()
        p2 = searcher._random_strategy()
        child = searcher._crossover(p1, p2)

        assert len(child.buy_builders) == constraints_config.discrete_search.num_buy_rules

    def test_mutation(self, synthetic_stocks_data, constraints_config):
        """变异操作不抛异常"""
        from src.analysis.genetic_searcher import GeneticSearcher
        from src.analysis.walk_forward import WalkForwardManager
        from src.analysis.fast_evaluator import FastEvaluator

        wf_mgr = WalkForwardManager(
            synthetic_stocks_data,
            train_months=6, test_months=3, step_months=1, num_windows=3,
        )
        ev = FastEvaluator(initial_cash=100000, monthly_buy_limit=999999)

        searcher = GeneticSearcher(constraints_config, wf_mgr, ev)

        enc = searcher._random_strategy()
        mutated = searcher._mutate(enc)

        assert len(mutated.buy_builders) == len(enc.buy_builders)


# ════════════════════════════════════════════════════════
#  5. 端到端
# ════════════════════════════════════════════════════════


class TestEndToEnd:
    """端到端测试"""

    def test_full_pipeline(self, synthetic_stocks_data, constraints_config):
        """全流程: 约束 → WF → FastEvaluator → GeneticSearcher"""
        from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2

        opt = StrategyOptimizerV2(synthetic_stocks_data, "a_share")
        opt.constraints = constraints_config
        opt.gs_cfg = constraints_config.genetic_search
        opt.wf_cfg = constraints_config.walk_forward
        opt.ds_cfg = constraints_config.discrete_search

        report = opt.run(
            stock_codes=list(synthetic_stocks_data.keys()),
        )

        assert report is not None
        assert report.group == "a_share"
        assert isinstance(report.elapsed_seconds, float)
