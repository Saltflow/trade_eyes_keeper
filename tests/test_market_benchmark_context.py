from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.search.config import StrategyConstraints
from src.search.workflow import _prepare_wf_evaluation_contexts


@pytest.mark.parametrize(
    ("group", "symbols", "controls"),
    [
        ("a_share", ["601088"], ["510880", "510300", "risk_free"]),
        ("hk", ["00883"], ["VOO", "BRK.B", "risk_free"]),
        ("us", ["GOOG"], ["VOO", "BRK.B", "risk_free"]),
    ],
)
def test_optimizer_builds_exact_configured_market_controls(
    group, symbols, controls
):
    constraints = StrategyConstraints(
        {
            "benchmarks": {
                group: controls,
                "risk_free_rates": {group: 0.02},
            },
            "execution_params": {
                "initial_capital": 100_000,
                "commission_rate": 0.005,
                "lot_sizes": {"a_share": 100, "hk": 100, "us": 1},
                "fx_rates": {"a_share": 1.0, "hk": 0.9, "us": 7.0},
            },
        }
    )
    constraints.set_group(group)
    dates = pd.bdate_range("2026-01-01", periods=6)
    close = np.linspace(10.0, 12.5, len(dates), dtype=np.float64)
    manager = SimpleNamespace(
        dates=dates,
        stock_codes=symbols,
        market_group=group,
        indicator_matrix=np.zeros((len(dates), 1, 22), dtype=np.float32),
        price_matrix=close.reshape(-1, 1),
        price_high_matrix=(close + 0.2).reshape(-1, 1),
        price_low_matrix=(close - 0.2).reshape(-1, 1),
        benchmark_series={
            code: close + index
            for index, code in enumerate(controls)
            if code != "risk_free"
        },
        benchmark_high_series={
            code: close + index + 0.2
            for index, code in enumerate(controls)
            if code != "risk_free"
        },
    )
    evaluator = SimpleNamespace(
        initial_cash=100_000.0,
        commission_rate=0.005,
        lot_size=constraints.execution.lot_sizes[group],
        fx_rate=constraints.execution.fx_rates[group],
    )
    window = SimpleNamespace(train_start=0, test_start=1, test_end=5)

    prepared = _prepare_wf_evaluation_contexts(
        [window], "train", constraints, evaluator, manager
    )
    context = prepared["windows"][0]

    assert set(context["benchmark_series"]) == set(controls)
    assert set(context["benchmark_initial_values"]) == set(controls)
    assert set(context["benchmark_raw_returns"]) == set(controls)
    assert "universe_equal_weight" not in context["benchmark_series"]
