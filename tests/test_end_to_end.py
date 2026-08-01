"""End-to-end tests for unified optimization and daily simulation."""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import INDICATOR_NAMES, simulate_portfolio
from src.search.config import StrategyConstraints, get_execution_config
from src.search.workflow import run_optimizer
from src.strategy import Params, StrategyMarketData
from src.strategy import get_strategy
from src.data.technical_indicators import compute_all

os.environ["LOG_LEVEL"] = "ERROR"

TEST_CODES = ["601088", "600938", "600795"]


def _load_stocks() -> dict[str, pd.DataFrame]:
    data = {}
    for code in TEST_CODES:
        path = os.path.join("data", f"{code}_history.csv")
        if not os.path.exists(path):
            pytest.skip(f"local real-data fixture is unavailable: {path}")
        frame = pd.read_csv(path)
        frame["date"] = pd.to_datetime(frame["date"])
        frame.set_index("date", inplace=True)
        data[code] = frame
    return data


def _market_data(stocks_data: dict[str, pd.DataFrame]) -> StrategyMarketData:
    computed = compute_all(stocks_data)
    date_sets = [
        set(frame.index)
        for frame in computed.values()
        if frame is not None and not frame.empty
    ]
    if not date_sets:
        raise AssertionError("technical indicator calculation returned no data")
    common = sorted(date_sets[0].intersection(*date_sets[1:]))
    if not common:
        raise AssertionError("configured symbols have no common trading dates")

    dates = pd.DatetimeIndex(common)
    rows = len(dates)
    columns = len(TEST_CODES)
    indicators = np.full(
        (rows, columns, len(INDICATOR_NAMES)),
        np.nan,
        dtype=np.float32,
    )
    prices = np.full((rows, columns), np.nan, dtype=np.float32)
    highs = np.full_like(prices, np.nan)
    lows = np.full_like(prices, np.nan)

    for symbol_index, code in enumerate(TEST_CODES):
        frame = computed[code].reindex(dates)
        for feature_index, name in enumerate(INDICATOR_NAMES):
            if name in frame:
                indicators[:, symbol_index, feature_index] = (
                    frame[name].to_numpy(dtype=np.float32)
                )
        prices[:, symbol_index] = frame["close"].to_numpy(dtype=np.float32)
        highs[:, symbol_index] = frame["high"].to_numpy(dtype=np.float32)
        lows[:, symbol_index] = frame["low"].to_numpy(dtype=np.float32)

    return StrategyMarketData(
        indicator_matrix=indicators,
        dates=[date.strftime("%Y-%m-%d") for date in dates],
        symbols=list(TEST_CODES),
        prices=prices,
        highs=highs,
        lows=lows,
        tradable=np.isfinite(prices) & (prices > 0),
        market="a_share",
    )


def _smoke_constraints() -> StrategyConstraints:
    return StrategyConstraints(
        {
            "walk_forward": {
                "train_months": 3,
                "test_months": 3,
                "step_months": 1,
                "num_windows": 1,
                "validation_windows": 0,
            },
            "genetic_search": {
                "phase1_random_samples": 50,
                "phase1_top_keep": 10,
                "num_generations": 0,
                "population_size": 10,
                "offspring_size": 0,
                "sensitivity_top_candidates": 1,
                "sensitivity_samples": 1,
                "min_weighted_strategy_return": -999.0,
                "min_positive_return_windows": 0,
            },
            "search": {
                "solver_id": "random",
                "gate_profile": "smoke",
                "batch_size": 16,
                "workers": 1,
                "checkpoint": False,
                "solvers": {"random": {"budget": 50, "random_seed": 7}},
            },
            "gate_profiles": {
                "smoke": {
                    "activation_eligible": False,
                    "rules": [],
                }
            },
            "hard_constraints": {
                "min_avg_position_pct": 0.0,
                "max_drawdown_pct": -50.0,
                "max_return_std_pct": 100.0,
                "min_trades_per_month": 0,
                "max_trades_per_month": 50,
            },
            "benchmarks": {
                "a_share": [],
                "risk_free_rates": {"a_share": 0.02},
            },
            "execution_params": {
                "initial_capital": 100000,
                "commission_rate": 0.005,
                "min_holding_days": 5,
                "lot_sizes": {"a_share": 100},
                "fx_rates": {"a_share": 1.0},
            },
        }
    )


def _percentile_params() -> Params:
    return Params(
        values={
            "adx_pct_tau": 5,
            "adx_pct_w": 3,
            "rsi_pct_tau": 5,
            "rsi_pct_w": 3,
            "deviation_pct_tau": 6,
            "deviation_pct_w": 2,
            "vol_ratio_pct_tau": 5,
            "vol_ratio_pct_w": 2,
            "ma200_dev_pct_tau": 3,
            "ma200_dev_pct_w": 1,
            "buy_score_thresh": 5,
            "sell_score_thresh": 5,
            "buy_cash_tier": 2,
            "sell_cash_tier": 2,
        },
        _engine="percentile",
    )


def test_optimizer_produces_valid_results(tmp_path):
    stocks_data = _load_stocks()
    strategy = get_strategy("percentile")
    assert strategy is not None
    constraints = _smoke_constraints()
    constraints.set_group("a_share")

    results, _ = run_optimizer(
        strategy,
        stocks_data,
        TEST_CODES,
        "a_share",
        _constraints=constraints,
        output_dir=tmp_path,
    )

    assert results
    selected = results[0]
    assert selected.parameters
    assert selected.all_stats
    assert -30 < selected.objective_score < 30

    stats = selected.all_stats[0]
    assert 0 <= stats.total_trades <= 30
    assert -50 <= stats.max_drawdown_pct <= 0
    assert 0 <= stats.avg_position_pct <= 100
    assert -10 < stats.sharpe_ratio < 10

    params = Params(
        values=dict(selected.parameters),
        _engine=strategy.name,
    )
    plan = strategy.make_signals(params, _market_data(stocks_data))
    assert plan.buy_signals.shape == plan.sell_signals.shape
    assert plan.buy_signals.dtype == bool
    assert plan.sell_signals.dtype == bool


def test_daily_report_pipeline_uses_canonical_trade_plan():
    stocks_data = _load_stocks()
    strategy = get_strategy("percentile")
    assert strategy is not None
    market_data = _market_data(stocks_data)
    trade_plan = strategy.make_signals(_percentile_params(), market_data)
    execution = get_execution_config()

    trace = simulate_portfolio(
        trade_plan,
        market_data,
        float(execution.initial_capital),
        execution.lot_sizes.get("a_share", 100),
        float(execution.commission_rate),
        min_holding_days=int(execution.min_holding_days),
    )

    assert trace.total_trades >= 0
    assert abs(trace.total_return_pct) < 200
    assert -100 <= trace.max_drawdown_pct <= 0
    assert trace.quarterly_holdings
    assert isinstance(trace.nav_series, list)
    assert trace.nav_series
