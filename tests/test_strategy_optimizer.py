"""tests for strategy_optimizer.py"""

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from pathlib import Path
import yaml

from src.analysis.strategy_optimizer import (
    StrategyOptimizer, StrategyTrial, OptimizationReport,
    CONDITION_BUILDERS, BUY_BUILDERS, SELL_BUILDERS,
    build_condition,
)


# ── 构建器测试 ──

class TestSignalBuilders:
    def test_all_builders_defined(self):
        for name in BUY_BUILDERS + SELL_BUILDERS:
            assert name in CONDITION_BUILDERS

    def test_deviation_cross_buy(self):
        cond, reset = build_condition("deviation_cross", 0.5, "buy")
        assert "deviation <=" in cond
        assert "prev_deviation" in cond
        assert "shares" not in cond
        assert "deviation > 0" in reset

    def test_deviation_cross_sell(self):
        cond, reset = build_condition("deviation_cross", 0.5, "sell")
        assert "deviation >=" in cond
        assert "shares > 0" in cond

    def test_rsi_signal_buy(self):
        cond, _ = build_condition("rsi_signal", 0.3, "buy")
        assert "rsi <" in cond
        assert "shares" not in cond

    def test_rsi_signal_sell(self):
        cond, _ = build_condition("rsi_signal", 0.7, "sell")
        assert "rsi >" in cond

    def test_bollinger_signal_buy(self):
        cond, _ = build_condition("bollinger_signal", 0.2, "buy")
        assert "boll_pct_b <" in cond

    def test_bollinger_signal_sell(self):
        cond, _ = build_condition("bollinger_signal", 0.8, "sell")
        assert "boll_pct_b >" in cond

    def test_volume_spike_buy(self):
        cond, _ = build_condition("volume_spike", 0.5, "buy")
        assert "vol_ratio >" in cond

    def test_volume_spike_sell_fallback(self):
        cond, _ = build_condition("volume_spike", 0.5, "sell")
        # Sell with volume should fallback to just shares check
        assert "shares" in cond.lower() or "false" in cond.lower()

    def test_deviation_absolute_buy(self):
        cond, _ = build_condition("deviation_absolute", 0.3, "buy")
        assert "deviation <=" in cond
        assert "prev_deviation" not in cond  # no cross requirement

    def test_deviation_absolute_sell(self):
        cond, _ = build_condition("deviation_absolute", 0.7, "sell")
        assert "deviation >=" in cond

    def test_trend_follow_buy(self):
        cond, _ = build_condition("trend_follow", 0.5, "buy")
        assert "adx >" in cond
        assert "macd_hist >" in cond

    def test_trend_follow_sell(self):
        cond, _ = build_condition("trend_follow", 0.5, "sell")
        assert "adx >" in cond

    def test_none_builder(self):
        cond, reset = build_condition("none", 0.5, "buy")
        assert cond == "False"
        assert reset == "True"


# ── 数据类测试 ──

class TestStrategyTrial:
    def test_fitness_returns_train_return(self):
        t = StrategyTrial(
            params={}, rules=[], train_return=5.0, train_drawdown=-2.0,
            test_return=8.0, test_drawdown=-3.0,
            sharpe=1.5, trade_count=10,
        )
        assert t.fitness == 5.0


class TestOptimizationReport:
    def test_default_fields(self):
        r = OptimizationReport(
            report_id="test", group="a_share",
            timestamp="2026-01-01", iterations=10,
        )
        assert r.top_strategies == []
        assert r.convergence == []
        assert r.benchmarks == {}
        assert r.elapsed_seconds == 0.0


# ── Optimizer 核心测试 ──

@pytest.fixture
def mock_optimizer_yaml(tmp_path):
    """Create a minimal optimizer.yaml in a temp dir"""
    content = {
        "strategy_template": {
            "rules": {
                "buy_1": {
                    "type": "buy", "priority": 1, "label": "买入1",
                    "builders": ["deviation_cross", "rsi_signal", "none"],
                    "budget_pool": "buy",
                },
                "buy_2": {
                    "type": "buy", "priority": 2, "label": "买入2",
                    "builders": ["volume_spike", "none"],
                    "budget_pool": "buy",
                },
                "sell_1": {
                    "type": "sell", "priority": 3, "label": "卖出1",
                    "builders": ["deviation_cross", "none"],
                    "budget_pool": "sell",
                },
                "sell_2": {
                    "type": "sell", "priority": 4, "label": "卖出2",
                    "builders": ["none"],
                    "budget_pool": "sell",
                },
                "sell_3": {
                    "type": "sell", "priority": 5, "label": "卖出3",
                    "builders": ["none"],
                    "budget_pool": "sell",
                },
            },
        },
        "search_params": {
            "threshold_range": [0.0, 1.0],
            "buy_frac_range": [0.02, 0.30],
            "sell_frac_range": [0.10, 0.50],
        },
        "constraints": {
            "max_drawdown_pct": -25,
            "drawdown_penalty_weight": 2.0,
            "iterations": 150,
            "random_starts": 20,
        },
        "output": {"top_n": 10, "save_dir": "data/optimizer"},
    }
    f = tmp_path / "optimizer.yaml"
    f.write_text(yaml.dump(content))
    return f


@pytest.fixture
def sample_stocks_data():
    """Generate 2 stocks with 500 trading days of data"""
    np.random.seed(42)
    n = 500
    trend1 = 50 + np.cumsum(np.random.randn(n) * 0.3)
    trend2 = 80 + np.cumsum(np.random.randn(n) * 0.4)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    def make_df(trend):
        df = pd.DataFrame({
            "date": dates,
            "open": trend + np.random.randn(n) * 0.2,
            "high": trend + np.abs(np.random.randn(n)) * 2,
            "low": trend - np.abs(np.random.randn(n)) * 2,
            "close": np.maximum(trend, 1),
            "volume": np.random.randint(1000, 50000, n),
        })
        df["high"] = df[["high", "close", "open"]].max(axis=1)
        df["low"] = df[["low", "close", "open"]].min(axis=1)
        return df

    return {"000001": make_df(trend1), "600036": make_df(trend2)}


class TestOptimizerInit:
    def test_loads_optimizer_yaml(self, sample_stocks_data, mock_optimizer_yaml):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        assert opt.group == "a_share"
        rules = opt.opt_config["strategy_template"]["rules"]
        assert len(rules) == 5

    def test_get_rule_specs_order(self, sample_stocks_data, mock_optimizer_yaml):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        specs = opt._get_rule_specs()
        # Should be sorted by priority
        assert specs[0]["id"] == "buy_1"
        assert specs[1]["id"] == "buy_2"

    def test_build_dimensions(self, sample_stocks_data, mock_optimizer_yaml):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        dims = opt._build_dimensions(["000001", "600036"])
        # 5 rules × 3 + 2 stock switches = 17
        assert len(dims) == 17

    def test_params_to_rules(self, sample_stocks_data, mock_optimizer_yaml):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        # Generate a param vector: buy_1(0,0.5,0.2) buy_2(0,0.3,0.1) sell_1(0,0.6,0.2) sell_2(0,0,0.1) sell_3(0,0,0.1) + 2 stock flags(1,0)
        param_vec = [
            0, 0.5, 0.2,  # buy_1: deviation_cross[0], t=0.5, frac=0.2
            0, 0.3, 0.1,  # buy_2: volume_spike[0], t=0.3, frac=0.1
            0, 0.6, 0.2,  # sell_1: deviation_cross[0], t=0.6, frac=0.2
            0, 0.0, 0.1,  # sell_2: none[0], t=0, frac=0.1
            0, 0.0, 0.1,  # sell_3: none[0], t=0, frac=0.1
            1.0, 0.0,     # include 000001, exclude 600036
        ]
        # Call _build_dimensions first to set _num_rule_dims
        opt._build_dimensions(["000001", "600036"])
        rules, included = opt._params_to_rules(
            param_vec, ["000001", "600036"]
        )
        assert len(rules) == 5
        assert "000001" in included
        assert "600036" not in included

    def test_prefilter_builders_preserves_none(
        self, sample_stocks_data, mock_optimizer_yaml,
    ):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        opt.indicators = sample_stocks_data  # mock indicator data
        buy, sell = opt._prefilter_builders(["000001"], observe_days=10)
        assert "none" in buy
        assert "none" in sell

    def test_print_report_produces_string(
        self, sample_stocks_data, mock_optimizer_yaml,
    ):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        report = OptimizationReport(
            report_id="test", group="a_share",
            timestamp="2026-01-01", iterations=10,
            top_strategies=[
                StrategyTrial(
                    params={"_stocks": "000001"},
                    rules=[], train_return=5.0, train_drawdown=-2.0,
                    test_return=8.0, test_drawdown=-3.0,
                    sharpe=1.5, trade_count=10,
                )
            ],
        )
        text = opt.print_report(report)
        assert "test" in text
        assert "a_share" in text


class TestEdgeCases:
    def test_empty_stocks_data_graceful(self, mock_optimizer_yaml):
        opt = StrategyOptimizer(
            {}, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        specs = opt._get_rule_specs()
        assert len(specs) == 5  # specs still work with empty data

    def test_single_builder_rule(self, sample_stocks_data, mock_optimizer_yaml):
        opt = StrategyOptimizer(
            sample_stocks_data, "a_share",
            template_path=str(mock_optimizer_yaml),
        )
        dims = opt._build_dimensions(["000001"])
        # Rule sell_3 has only 1 builder ("none") — should still create valid dim
        assert len(dims) == 16  # 5×3 + 1 stock
