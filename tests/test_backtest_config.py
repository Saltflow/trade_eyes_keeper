"""tests for backtest_config.py"""

from src.analysis.backtest_config import (
    BacktestConfig, elapsed_months,
    make_training_config, make_default_optimizer_config,
)


class TestBacktestConfig:
    def test_defaults(self):
        cfg = BacktestConfig()
        assert cfg.observe_end_month == 6
        assert cfg.trade_end_month == 18
        assert cfg.initial_capital == 100000.0
        assert cfg.monthly_buy_limit == float("inf")

    def test_get_phase_observe(self):
        cfg = BacktestConfig(observe_end_month=6)
        assert cfg.get_phase(0) == "observe"
        assert cfg.get_phase(3) == "observe"
        assert cfg.get_phase(5.9) == "observe"

    def test_get_phase_trade(self):
        cfg = BacktestConfig(observe_end_month=6, trade_end_month=18)
        assert cfg.get_phase(6) == "trade"
        assert cfg.get_phase(12) == "trade"
        assert cfg.get_phase(17.9) == "trade"

    def test_get_phase_hold(self):
        cfg = BacktestConfig(trade_end_month=18)
        assert cfg.get_phase(18) == "hold"
        assert cfg.get_phase(24) == "hold"

    def test_can_trade(self):
        cfg = BacktestConfig(observe_end_month=6, trade_end_month=18)
        assert not cfg.can_trade(3)
        assert cfg.can_trade(10)
        assert not cfg.can_trade(20)

    def test_get_injection(self):
        cfg = BacktestConfig(capital_injections={6: 20000, 8: 15000})
        assert cfg.get_injection(6) == 20000
        assert cfg.get_injection(8) == 15000
        assert cfg.get_injection(7) == 0.0

    def test_get_lot_size_override(self):
        cfg = BacktestConfig(lot_size_override={"510880": 1})
        assert cfg.get_lot_size("510880", 100) == 1
        assert cfg.get_lot_size("601728", 100) == 100

    def test_elapsed_months_same_year(self):
        months = elapsed_months("2024-06-15", "2024-01-01")
        assert 5 < months < 6

    def test_elapsed_months_cross_year(self):
        months = elapsed_months("2025-01-15", "2024-06-01")
        assert 7 < months < 8

    def test_make_training_config(self):
        cfg = make_training_config()
        assert cfg.observe_end_month == 6
        assert cfg.trade_end_month == 12
        assert cfg.monthly_buy_limit == float("inf")
        assert 6 in cfg.capital_injections

    def test_make_default_optimizer_config(self):
        cfg = make_default_optimizer_config()
        assert cfg.trade_end_month == 18
        assert cfg.initial_capital == 100000.0

    def test_injection_schedule(self):
        cfg = make_default_optimizer_config()
        total = sum(cfg.capital_injections.values())
        assert total == 140000  # 7 months × 20000


class TestBacktestConfigEdgeCases:
    def test_zero_observe(self):
        cfg = BacktestConfig(observe_end_month=0)
        assert cfg.get_phase(0) == "trade"

    def test_negative_month(self):
        cfg = BacktestConfig(observe_end_month=6)
        assert cfg.get_phase(-1) == "observe"

    def test_empty_injections(self):
        cfg = BacktestConfig()
        assert cfg.capital_injections == {}
