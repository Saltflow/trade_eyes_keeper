"""Manual walk-forward evaluations retain full shared execution semantics."""

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import (
    FastEvaluator,
    WalkForwardManager,
    WindowSlice,
    _compute_stats,
    simulate_portfolio,
)
from src.search.config import ExecutionConfig, StrategyConstraints, WindowStats
from src.search.workflow import _compute_ranking_wf_score, _partition_window_indexes
from src.strategy import StrategyMarketData, TradePlan


def _constraints() -> StrategyConstraints:
    constraints = StrategyConstraints(
        {
            "benchmarks": {
                "a_share": ["510880", "510300", "risk_free"],
                "risk_free_rates": {"a_share": 0.02},
            },
            "walk_forward": {
                "num_windows": 3,
                "validation_windows": 1,
                "window_weights": [1.0, 2.0],
                "stability_penalty": 0.5,
            },
        }
    )
    constraints.set_group("a_share")
    return constraints


def test_ranking_score_excludes_held_out_window_and_extends_weights():
    constraints = _constraints()
    controls = {"510880": 6.0, "510300": 4.0, "risk_free": 2.0}
    stats = [
        WindowStats(strategy_return=8.0, benchmark_returns=controls),
        WindowStats(strategy_return=14.0, benchmark_returns=controls),
        WindowStats(strategy_return=-999.0, benchmark_returns=controls),
    ]

    ranking, validation = stats[:2], stats[2:]

    assert ranking == stats[:2]
    assert validation == stats[2:]
    # Median-control excess is 4 and 10. Weighted mean is 8; the
    # best-minus-worst range is 6, so lambda=0.5 produces a score of 5.
    assert _compute_ranking_wf_score(ranking, constraints) == pytest.approx(5.0)
    assert constraints.walk_forward.ranking_window_count == 2
    # A short legacy weight array must not silently discard a historical window.
    assert constraints.walk_forward.ranking_weights(4) == [1.0, 2.0, 2.0, 2.0]


def test_walk_forward_holdout_is_anchored_to_the_newest_market_date():
    constraints = StrategyConstraints(
        {
            "walk_forward": {
                "state_lookback_months": 12,
                "test_months": 24,
                "step_months": 12,
                "num_windows": 5,
                "validation_windows": 1,
            }
        }
    )
    manager = object.__new__(WalkForwardManager)
    manager.wf_config = constraints.walk_forward
    # Includes extra rows for rolling indicators.  The newest held-out window
    # must still end at the most recent row, rather than before that buffer.
    manager.T = 2_500
    manager.dates = pd.bdate_range("2016-01-01", periods=manager.T)

    windows = manager.iter_windows()

    assert len(windows) == 5
    assert windows[-1].test_end == manager.T
    assert pd.Timestamp(windows[0].train_start_date) + pd.DateOffset(
        months=12
    ) == pd.Timestamp(windows[0].test_start_date)
    assert pd.Timestamp(windows[-1].test_start_date) + pd.DateOffset(
        months=24
    ) - pd.Timedelta(days=1) == pd.Timestamp(windows[-1].test_end_date)


def test_overlapping_windows_are_purged_from_strict_holdout_ranking():
    constraints = StrategyConstraints(
        {
            "walk_forward": {
                "num_windows": 5,
                "validation_windows": 1,
                "purge_overlapping_windows": True,
            }
        }
    )
    windows = [
        WindowSlice(0, 0, 500 + index * 252, 1004 + index * 252, index)
        for index in range(5)
    ]

    ranking, purged, validation = _partition_window_indexes(windows, constraints)

    assert ranking == [0, 1, 2]
    assert purged == [3]
    assert validation == [4]
    validation_start = windows[validation[0]].test_start
    assert all(windows[index].test_end <= validation_start for index in ranking)
    assert all(windows[index].test_end > validation_start for index in purged)


def test_fast_evaluator_scores_primary_benchmark_excess_return():
    stats = _compute_stats(
        np.array([100.0, 110.0]),
        np.array([[10.0], [11.0]]),
        np.array([100.0, 100.1]),
        trade_count=1,
        signal_count=0,
        benchmark_series={"510880": np.array([100.0, 105.0])},
    )

    assert stats.strategy_return == pytest.approx(10.0)
    assert stats.benchmark_returns == {"510880": 5.0}
    assert stats.test_excess_return == pytest.approx(5.0)


def test_manual_execution_matches_shared_portfolio_execution():
    prices = np.array(
        [[10.0], [11.0], [12.0], [11.0], [10.0], [12.0]], dtype=np.float32
    )
    buy = np.array([[True], [False], [False], [False], [True], [False]])
    sell = np.array([[False], [False], [False], [True], [False], [False]])
    execution = ExecutionConfig(
        initial_capital=100000.0,
        monthly_buy_limit=15000.0,
        commission_rate=0.005,
        min_holding_days=30,
        lot_sizes={"a_share": 100},
    )
    evaluator = FastEvaluator(execution, "a_share")
    plan = TradePlan(
        buy_signals=buy,
        sell_signals=sell,
        buy_priority=np.where(buy, 1.0, -np.inf),
        sell_priority=np.where(sell, 1.0, -np.inf),
        buy_cash_limit=45_000.0,
        sell_cash_limit=45_000.0,
        warmup_rows=0,
    )
    fast = evaluator.evaluate(
        np.zeros((len(prices), 1, 16), dtype=np.float32),
        prices,
        np.full(len(prices), 100000.0),
        trade_plan=plan,
    )
    daily = simulate_portfolio(
        plan,
        StrategyMarketData(
            indicator_matrix=np.empty((*prices.shape, 0), dtype=np.float32),
            dates=[
                f"2026-01-{index + 1:02d}" for index in range(len(prices))
            ],
            symbols=["000001"],
            prices=prices,
            tradable=np.ones_like(prices, dtype=bool),
        ),
        initial_cash=100000.0,
        lot_size=100,
        commission_rate=0.005,
        min_holding_days=30,
    )

    assert fast.total_trades == daily.total_trades
    assert fast.strategy_return == pytest.approx(daily.total_return_pct)
    assert fast.max_drawdown_pct == pytest.approx(daily.max_drawdown_pct)
