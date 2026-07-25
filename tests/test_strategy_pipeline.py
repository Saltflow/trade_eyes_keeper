"""策略无关性管线测试。

验证 evaluate_all_groups 对任意 SearchStrategy 实例均产出有效 EvaluationReport。
仅走真数据路径（DataSource 网络拉取），禁止本地文件/Mock。
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("LOG_LEVEL", "ERROR")

from src.analysis.strategies import get_strategy
from src.analysis.backtester import evaluate_all_groups
from src.analysis.config import get_execution_config
from src.analysis.search_interface import EvaluationReport

TEST_CODES = ["601088", "600938", "600795"]


def _load_real_data():
    """真数据源拉取（网络依赖）。不可用时标记 skip。"""
    from src.data.data_source import DataSource
    import yaml

    with open("config/config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    ds = DataSource(config)
    stocks_data = {}
    for code in TEST_CODES:
        try:
            df = ds.fetch_stock_data(code, days=730)
            if df is not None and not df.empty and "close" in df.columns:
                stocks_data[code] = df
        except Exception:
            pass

    if len(stocks_data) < 2:
        pytest.skip("网络不可用或数据不足")
    return stocks_data


class TestAllStrategiesPlugIntoPipeline:
    """evaluate_all_groups 对三种策略均产出有效 EvaluationReport"""

    @pytest.fixture(scope="class")
    def real_stocks_data(self):
        return _load_real_data()

    @pytest.mark.parametrize("strategy_name", ["percentile", "builder", "simplified"])
    def test_produces_valid_report(self, real_stocks_data, strategy_name):
        strategy = get_strategy(strategy_name)
        if strategy is None:
            pytest.skip(f"策略 {strategy_name} 不可用")

        params = strategy.random_params()
        reports = evaluate_all_groups(
            real_stocks_data,
            list(real_stocks_data.keys()),
            strategy,
            params,
            get_execution_config(),
        )
        assert len(reports) > 0, f"{strategy_name} 应至少产出 1 组报告"

        for gk, r in reports.items():
            assert isinstance(r, EvaluationReport)
            assert r.max_drawdown <= 0, f"{strategy_name}/{gk} 回撤应≤0，实际 {r.max_drawdown}"
            assert len(r.nav_series) >= 20, f"{strategy_name}/{gk} 净值序列应≥20点"
            assert r.trade_count >= 0, f"{strategy_name}/{gk} 交易笔数应≥0"
