"""SignalFnSearchEngine 适配器测试：把 SignalFn 接入遗传搜索的桥接层。

锁定 scope A 核心：分位引擎经此适配器真正参与搜索
（evaluate() 被调用 → WindowStats），而非空壳。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")


def _mk_stocks(codes=("600001", "600002", "600003"), n=760, seed=1):
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2022-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    out = {}
    for i, c in enumerate(codes):
        t = np.linspace(0, 0.4 + 0.1 * i, n) + rng.randn(n).cumsum() * 0.012
        close = 10 * np.exp(t)
        out[c] = pd.DataFrame({
            "date": dates, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "volume": np.abs(rng.randn(n)) * 1e6 + 5e5,
        })
    return out


def _constraints():
    from analysis.optimizer_constraints import StrategyConstraints
    return StrategyConstraints({
        "hard_constraints": {
            "min_avg_position_pct": 0.0, "max_drawdown_pct": -99.0,
            "max_return_std_pct": 100.0, "min_trades_per_month": 0,
            "max_trades_per_month": 999,
        },
        "walk_forward": {"train_months": 12, "test_months": 6,
                         "step_months": 3, "num_windows": 2},
        "genetic_search": {
            "phase1_random_samples": 30, "phase1_top_keep": 10,
            "num_generations": 1, "population_size": 10, "offspring_size": 15,
        },
        "discrete_search": {"num_buy_rules": 3},
    })


class TestAdapterEncodingOps:
    def test_param_count_matches_space(self):
        from analysis.signal_fn_engine import SignalFnSearchEngine
        from analysis.percentile_engine import PercentileSignalFn
        eng = SignalFnSearchEngine(PercentileSignalFn())
        assert eng.param_count() == 13  # 5×(tau+w)+buy+sell+frac

    def test_random_crossover_mutate(self):
        from analysis.signal_fn_engine import SignalFnSearchEngine
        from analysis.percentile_engine import PercentileSignalFn
        eng = SignalFnSearchEngine(PercentileSignalFn())
        p1 = eng.random_encoding(None)
        p2 = eng.random_encoding(None)
        assert p1._engine == "percentile"
        child = eng.crossover_encoding(p1, p2)
        assert set(child.values) == set(p1.values)
        mut = eng.mutate_encoding(p1, None)
        assert set(mut.values) == set(p1.values)

    def test_human_readable(self):
        from analysis.signal_fn_engine import SignalFnSearchEngine
        from analysis.percentile_engine import PercentileSignalFn
        eng = SignalFnSearchEngine(PercentileSignalFn())
        h = eng.to_human_readable(eng.random_encoding(None), None)
        assert "分位评分" in h


class TestAdapterEvaluatesViaSignalFn:
    """evaluate_encoding 真正调用 signal_fn.evaluate → 共享流水线 → WindowStats。"""

    def test_evaluate_encoding_returns_windowstats(self):
        from analysis.signal_fn_engine import SignalFnSearchEngine
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.walk_forward import WalkForwardManager
        from analysis.fast_evaluator import FastEvaluator
        from analysis.optimizer_constraints import WindowStats

        stocks = _mk_stocks()
        constraints = _constraints()
        wf = WalkForwardManager(
            stocks, train_months=12, test_months=6, step_months=3,
            num_windows=2,
        )
        windows = list(wf.iter_windows())
        evaluator = FastEvaluator(initial_cash=100000.0)
        eng = SignalFnSearchEngine(PercentileSignalFn())
        params = eng.random_encoding(None)

        result = eng.evaluate_encoding(
            params, windows, constraints.discrete_search, constraints,
            evaluator, wf,
        )
        assert result is not None
        stats, score = result
        assert all(isinstance(s, WindowStats) for s in stats)
        assert isinstance(score, float)


class TestEndToEndPercentileSearch:
    """端到端：分位引擎经优化器完整搜索，产出真实分位参数 YAML。"""

    def test_full_percentile_optimize(self):
        from analysis.strategy_optimizer_v2 import StrategyOptimizerV2
        from analysis.percentile_engine import PercentileSignalFn
        from analysis.signal_fn_engine import SignalFnSearchEngine

        stocks = _mk_stocks(n=900)
        constraints = _constraints()
        sfn = PercentileSignalFn()
        opt = StrategyOptimizerV2(
            stocks, "a_share", engine=SignalFnSearchEngine(sfn), signal_fn=sfn,
        )
        opt.constraints = constraints
        opt.gs_cfg = constraints.genetic_search
        opt.wf_cfg = constraints.walk_forward
        opt.ds_cfg = constraints.discrete_search

        report = opt.run(stock_codes=list(stocks.keys()))
        assert report is not None
        if report.top_strategies:
            top = report.top_strategies[0]
            # 真实分位参数写入
            assert top.params.get("_engine") == "percentile"
            assert top.params.get("_mode") == "signal_score"
            assert "adx_pct_tau" in top.params
            # 引擎自定义规则名 + __signal_fn__ 标记
            assert any(r.condition == "__signal_fn__" for r in top.rules)
            assert any("分位" in r.label for r in top.rules)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
