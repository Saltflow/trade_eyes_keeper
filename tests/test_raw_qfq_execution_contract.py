from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import Backtester, FastEvaluator, WalkForwardManager
from src.backtest.execution import (
    CorporateActionSlice,
    ExecutionPriceSlice,
    build_corporate_action_schedule,
)
from src.search.config import ExecutionConfig, StrategyConstraints
from src.search.workflow import (
    _partition_window_indexes,
    _prepare_wf_evaluation_contexts,
)
from src.strategy import StrategyMarketData, TradePlan
from src.data.market_history import CorporateAction, PriceHistoryBundle


def _plan(model: str = "cash_cap") -> TradePlan:
    return TradePlan(
        buy_signals=np.array([[True], [False], [False]], dtype=bool),
        sell_signals=np.array([[False], [False], [True]], dtype=bool),
        buy_priority=np.ones((3, 1), dtype=np.float32),
        sell_priority=np.ones((3, 1), dtype=np.float32),
        buy_cash_limit=1_000.0,
        sell_cash_limit=1_000.0,
        execution=(
            {"model": "target_weight", "per_symbol_cap": 1.0,
             "total_exposure_cap": 1.0}
            if model == "target_weight"
            else {"model": "cash_cap"}
        ),
        entry_events=np.array([[True], [False], [False]], dtype=bool)
        if model == "target_weight"
        else None,
        exit_events=np.array([[False], [False], [True]], dtype=bool)
        if model == "target_weight"
        else None,
        conviction=np.ones((3, 1), dtype=np.float32)
        if model == "target_weight"
        else None,
        target_weights=np.array([[1.0], [1.0], [0.0]], dtype=np.float32)
        if model == "target_weight"
        else None,
        warmup_rows=0,
        date_ordinals=np.array([1, 2, 3], dtype=np.int64),
    )


def _execution() -> ExecutionPriceSlice:
    actions = CorporateActionSlice(
        cash_dividends=np.array([[0.0], [1.0], [0.0]]),
        share_multipliers=np.array([[1.0], [2.0], [1.0]]),
    )
    # Raw price halves on the split date.  qfq would remain 10, but is not
    # used by the execution matrices in this contract test.
    return ExecutionPriceSlice(
        valuation_prices=np.array([[10.0], [5.0], [5.0]]),
        buy_prices=np.array([[10.0], [np.nan], [np.nan]]),
        sell_prices=np.array([[np.nan], [np.nan], [5.0]]),
        tradable=np.ones((3, 1), dtype=bool),
        corporate_actions=actions,
    )


def test_cash_dividend_split_updates_cash_shares_cost_and_nav():
    cfg = ExecutionConfig(
        initial_capital=1_000.0,
        commission_rate=0.0,
        min_holding_days=0,
        lot_sizes={"a_share": 1},
    )
    stats = FastEvaluator(cfg, "a_share").evaluate(
        np.zeros((3, 1, 1), dtype=np.float32),
        np.array([[10.0], [10.0], [10.0]], dtype=np.float32),
        np.full(3, 1_000.0),
        trade_plan=_plan(),
        execution_prices=_execution(),
    )

    assert stats.final_asset == 1_100.0
    assert stats.final_cash == 1_100.0
    assert stats.final_shares[0] == 0.0
    assert stats.final_position_pct == 0.0


def test_target_weight_and_cash_cap_share_the_same_action_contract():
    cfg = ExecutionConfig(
        initial_capital=1_000.0,
        commission_rate=0.0,
        min_holding_days=0,
        lot_sizes={"a_share": 1},
    )
    evaluator = FastEvaluator(cfg, "a_share")
    cash = evaluator.evaluate(
        np.zeros((3, 1, 1), dtype=np.float32),
        np.array([[10.0], [10.0], [10.0]], dtype=np.float32),
        np.full(3, 1_000.0),
        trade_plan=_plan("cash_cap"),
        execution_prices=_execution(),
    )
    target = evaluator.evaluate(
        np.zeros((3, 1, 1), dtype=np.float32),
        np.array([[10.0], [10.0], [10.0]], dtype=np.float32),
        np.full(3, 1_000.0),
        trade_plan=_plan("target_weight"),
        execution_prices=_execution(),
    )

    assert target.final_asset == cash.final_asset == 1_100.0


def test_batch_and_scalar_paths_use_identical_action_schedule():
    cfg = ExecutionConfig(
        initial_capital=1_000.0,
        commission_rate=0.0,
        min_holding_days=0,
        lot_sizes={"a_share": 1},
    )
    evaluator = FastEvaluator(cfg, "a_share")
    plan = _plan()
    inputs = {
        "indicator_matrix": np.zeros((3, 1, 1), dtype=np.float32),
        "price_matrix": np.array([[10.0], [10.0], [10.0]], dtype=np.float32),
        "cash_baseline": np.full(3, 1_000.0),
        "execution_prices": _execution(),
    }
    scalar = evaluator.evaluate(trade_plan=plan, **inputs)
    batch = evaluator.evaluate_batch([plan, plan], workers=1, **inputs)
    assert [item.final_asset for item in batch] == [scalar.final_asset] * 2
    assert [item.final_cash for item in batch] == [scalar.final_cash] * 2


def test_backtester_scales_benchmark_dividend_cash_with_fx():
    cfg = ExecutionConfig(
        initial_capital=1_000.0,
        commission_rate=0.0,
        min_holding_days=0,
        lot_sizes={"a_share": 1, "us": 1},
        fx_rates={"a_share": 1.0, "us": 7.0},
    )
    dates = pd.date_range("2026-01-01", periods=3, freq="D")
    prices = np.full((3, 1), 10.0)
    market_data = StrategyMarketData(
        indicator_matrix=np.zeros((3, 1, 1), dtype=np.float32),
        dates=[str(item.date()) for item in dates],
        symbols=["000001"],
        prices=prices,
        highs=prices,
        lows=prices,
    )
    benchmark_prices = pd.DataFrame(
        {
            "date": dates,
            "raw_open": [10.0] * 3,
            "raw_high": [10.0] * 3,
            "raw_low": [10.0] * 3,
            "raw_close": [10.0] * 3,
            "qfq_open": [10.0] * 3,
            "qfq_high": [10.0] * 3,
            "qfq_low": [10.0] * 3,
            "qfq_close": [10.0] * 3,
            "qfq_factor": [1.0] * 3,
            "volume": [1000] * 3,
            "tradable": [True] * 3,
        }
    )
    benchmark = PriceHistoryBundle(
        code="GOOG",
        prices=benchmark_prices,
        actions=[
            CorporateAction(
                code="GOOG",
                action_type="cash_dividend",
                ex_date=dates[1].date(),
                cash_per_share=1.0,
                source="test",
                currency="USD",
            )
        ],
        source="test",
        currency="USD",
    ).validate()

    report = Backtester(cfg, "a_share").run(
        _plan(),
        market_data,
        benchmark_bundles={"GOOG": benchmark},
        benchmark_codes=["GOOG"],
        risk_free_rate=0.0,
    )

    assert report.benchmark_details["GOOG"]["benchmark_return"] == 9.8


def test_search_context_scales_benchmark_dividend_cash_with_fx():
    cfg = StrategyConstraints(
        {
            "benchmarks": {"a_share": ["GOOG"]},
            "execution_params": {
                "initial_capital": 1_000.0,
                "commission_rate": 0.0,
                "lot_sizes": {"a_share": 1, "us": 1},
                "fx_rates": {"a_share": 1.0, "us": 7.0},
            },
            "walk_forward": {"num_windows": 1, "test_months": 1},
        }
    )
    cfg.set_group("a_share")
    dates = pd.date_range("2026-01-01", periods=3, freq="D")
    prices = np.full((3, 1), 10.0)
    benchmark_actions = CorporateActionSlice(
        cash_dividends=np.array([[0.0], [1.0], [0.0]]),
        share_multipliers=np.ones((3, 1)),
    )
    manager = SimpleNamespace(
        dates=dates,
        stock_codes=["000001"],
        market_group="a_share",
        indicator_matrix=np.zeros((3, 1, 1), dtype=np.float32),
        price_matrix=prices,
        price_high_matrix=prices,
        price_low_matrix=prices,
        raw_price_matrix=prices,
        raw_price_high_matrix=prices,
        raw_price_low_matrix=prices,
        action_schedule=CorporateActionSlice.empty(3, 1),
        benchmark_series={"GOOG": prices[:, 0]},
        benchmark_high_series={"GOOG": prices[:, 0]},
        benchmark_low_series={"GOOG": prices[:, 0]},
        benchmark_action_schedules={"GOOG": benchmark_actions},
    )
    evaluator = SimpleNamespace(
        initial_cash=1_000.0,
        commission_rate=0.0,
        lot_size=1,
        fx_rate=1.0,
    )
    window = SimpleNamespace(train_start=0, test_start=0, test_end=3)

    prepared = _prepare_wf_evaluation_contexts(
        [window], "train", cfg, evaluator, manager
    )

    assert prepared["windows"][0]["benchmark_series"]["GOOG"][-1] == pytest.approx(
        1_098.0
    )


def test_schedule_rejects_unresolved_rights_issue():
    action = CorporateAction(
        code="000001",
        action_type="rights",
        ex_date=pd.Timestamp("2026-01-02").date(),
        rights_price=5.0,
        source="test",
    )
    with np.testing.assert_raises(ValueError):
        build_corporate_action_schedule(
            [action], pd.date_range("2026-01-01", periods=2), ["000001"]
        )


def test_walk_forward_contract_is_22_16_2_4():
    constraints = StrategyConstraints(
        {
            "benchmarks": {"a_share": ["risk_free"]},
            "walk_forward": {
                "state_lookback_months": 12,
                "test_months": 9,
                "step_months": 3,
                "num_windows": 22,
                "validation_windows": 4,
                "purge_overlapping_windows": True,
            },
            "execution_params": {"initial_capital": 1_000.0},
        }
    )
    dates = pd.date_range("2017-01-02", periods=2_200, freq="B")
    values = np.linspace(10.0, 20.0, len(dates))
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": values,
            "high": values + 0.2,
            "low": values - 0.2,
            "close": values,
            "volume": 1000,
        }
    )
    manager = WalkForwardManager(
        {"000001": frame}, constraints, ["000001"]
    )
    windows = manager.iter_windows()
    ranking, purged, holdout = _partition_window_indexes(windows, constraints)
    assert len(windows) == 22
    assert len(ranking) == 16
    assert len(purged) == 2
    assert len(holdout) == 4
    assert set(ranking).isdisjoint(holdout)


def test_point_in_time_manager_separates_qfq_signals_from_raw_execution():
    dates = pd.date_range("2024-01-02", periods=4, freq="B")
    raw = np.array([10.0, 10.0, 5.0, 5.0])
    qfq = np.array([20.0, 20.0, 10.0, 10.0])
    frame = pd.DataFrame(
        {
            "date": dates,
            "raw_open": raw,
            "raw_high": raw + 1,
            "raw_low": raw - 1,
            "raw_close": raw,
            "qfq_open": qfq,
            "qfq_high": qfq + 1,
            "qfq_low": qfq - 1,
            "qfq_close": qfq,
            "qfq_factor": qfq / raw,
            "volume": 1000,
            "tradable": True,
        }
    )
    bundle = PriceHistoryBundle(code="000001", prices=frame).validate()
    constraints = StrategyConstraints(
        {
            "benchmarks": {"a_share": ["risk_free"]},
            "walk_forward": {"num_windows": 1, "test_months": 1},
        }
    )
    manager = WalkForwardManager(
        {"000001": pd.DataFrame()},
        constraints,
        ["000001"],
        market_bundles={"000001": bundle},
    )
    assert manager.price_matrix[0, 0] == 20.0
    assert manager.raw_price_matrix[0, 0] == 10.0
    assert manager.raw_price_high_matrix[0, 0] == 11.0
