"""
投资组合评估模块测试
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime

from src.analysis.portfolio_evaluator import (
    _detect_stock_group,
    _detect_fine_group,
    _get_lot_size,
    get_skip_search,
    get_skip_signals,
    EvaluationReport,
)


class TestStockGroupDetection:
    def test_a_share_six_digit(self):
        assert _detect_stock_group("601728") == "a_share"
        assert _detect_stock_group("000958") == "a_share"
        assert _detect_stock_group("600938") == "a_share"

    def test_non_a_share(self):
        assert _detect_stock_group("GOOG") == "non_a_share"
        assert _detect_stock_group("00883") == "non_a_share"
        assert _detect_stock_group("C38U.SI") == "non_a_share"

    def test_fine_group(self):
        assert _detect_fine_group("601728") == "a_share"
        assert _detect_fine_group("00883") == "hk"
        assert _detect_fine_group("GOOG") == "us"

    def test_lot_size(self):
        assert _get_lot_size("601728") == 100
        assert _get_lot_size("GOOG") == 1
        assert _get_lot_size("00883") == 100
        assert _get_lot_size("C38U.SI") == 1


class TestSkipSettings:
    def test_get_skip_search(self):
        config = {"skip_search": ["000001", "000002"]}
        skip = get_skip_search(config)
        assert "000001" in skip
        assert "000002" in skip

    def test_get_skip_signals(self):
        config = {"skip_signals": ["600000"]}
        skip = get_skip_signals(config)
        assert "600000" in skip


class TestEvaluationReport:
    def test_report_creation(self):
        r = EvaluationReport(
            group="a_share",
            engine_name="percentile",
            strategy_label="分位评分",
            timestamp="2026-01-01T00:00:00",
            total_return=15.0,
            excess_return=10.0,
            max_drawdown=-5.0,
            sharpe_ratio=1.5,
            trade_count=20,
            avg_cash_pct=30.0,
            benchmark_returns={"510300": 8.0, "risk_free": 2.0},
            composition=["601088"],
        )
        assert r.group == "a_share"
        assert r.total_return == 15.0

    def test_to_cache_dict(self):
        r = EvaluationReport(
            group="a_share",
            engine_name="pct",
            strategy_label="分位",
            timestamp="abc",
            total_return=10.0,
            excess_return=5.0,
            max_drawdown=-3.0,
            sharpe_ratio=1.2,
            trade_count=10,
            avg_cash_pct=20.0,
            benchmark_returns={"510300": 4.0},
            composition=["601728"],
        )
        d = r.to_cache_dict()
        assert d["total_return"] == 10.0
        assert d["excess_return"] == 5.0
        assert d["dd"] == -3.0
        assert d["sharpe"] == 1.2
        assert d["trades"] == 10
        assert "510300" in d["benchmark_returns"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
