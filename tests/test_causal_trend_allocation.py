"""Synthetic mechanism/causality tests, never a real-market search or trial."""

import json
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import FastEvaluator, simulate_portfolio
from src.backtest.execution import DEFAULT_FILL_PRICE_POLICY, CorporateActionSlice
from src.search import create_solver
from src.search.contracts import EvaluationBatch, GateDecision, SearchProblem
from src.strategy import Params, StrategyMarketData, get_strategy, list_strategy_ids


def _params(**changes):
    strategy = get_strategy("causal_trend_allocation")
    values = {dim.name: 0 for dim in strategy.param_space.dims}
    values.update(selection_count=1, total_exposure_cap=2, per_symbol_cap=3)
    values.update(changes)
    return Params(values, _engine=strategy.name)


def _market(rows=430, columns=2):
    index = np.arange(rows, dtype=np.float64)
    close = np.stack(
        [
            100
            * np.exp(
                (0.002 + 0.002 * column) * index
                + (0.002 + 0.018 * column) * np.sin(index * np.pi / 2)
            )
            for column in range(columns)
        ],
        axis=1,
    )
    matrix = np.zeros((rows, columns, 22), dtype=np.float32)
    matrix[:, :, 0] = close
    close = matrix[:, :, 0].copy()
    dates = pd.bdate_range("2023-01-02", periods=rows).strftime("%Y-%m-%d").tolist()
    return StrategyMarketData(
        indicator_matrix=matrix,
        dates=dates,
        symbols=[f"S{column}" for column in range(columns)],
        prices=close,
        highs=close * 1.01,
        lows=close * 0.99,
        tradable=np.ones(close.shape, dtype=bool),
    )


def _slice(market, start, end=None, order=None):
    selection = slice(start, end)
    order = np.arange(len(market.symbols)) if order is None else np.asarray(order)
    return StrategyMarketData(
        indicator_matrix=market.indicator_matrix[selection][:, order].copy(),
        prices=market.prices[selection][:, order].copy(),
        highs=market.highs[selection][:, order].copy(),
        lows=market.lows[selection][:, order].copy(),
        tradable=market.tradable[selection][:, order].copy(),
        dates=market.dates[selection],
        symbols=[market.symbols[index] for index in order],
        observation_counts=(
            market.observation_counts[selection][:, order].copy()
            if market.observation_counts is not None
            else None
        ),
    )


def _simulate(plan, market):
    return simulate_portfolio(
        plan,
        market,
        initial_cash=100_000,
        lot_size=1,
        commission_rate=0.005,
        min_holding_days=30,
        execution_prices=DEFAULT_FILL_PRICE_POLICY.build(
            market.prices, market.highs, market.lows
        ),
    )


def _assert_same(left, right, indexes=slice(None)):
    for key in (
        "entry_events",
        "exit_events",
        "buy_priority",
        "sell_priority",
        "target_weights",
        "conviction",
    ):
        np.testing.assert_allclose(
            getattr(left, key)[indexes], getattr(right, key), atol=1e-12
        )


def test_registered_as_new_six_dimensional_strategy_without_replacing_old_plugins():
    assert {
        "causal_trend_allocation",
        "technical_ensemble",
        "regime_pullback",
        "percentile",
    } <= set(list_strategy_ids())
    strategy = get_strategy("causal_trend_allocation")
    assert strategy.param_space.flat_size() == 6
    assert strategy.param_space.total_levels() == 3600
    assert strategy.parameter_schema_id == "causal-trend-allocation/1"
    assert strategy.window_state_scope == "train"
    assert strategy.feature_dependencies == ("close",)
    assert strategy.fundamental_feature_dependencies == ()


def test_separate_risk_allocation_has_no_hidden_trades_between_membership_changes():
    strategy, market = get_strategy("causal_trend_allocation"), _market()
    plan = strategy.make_signals(_params(), market)
    entry = int(np.flatnonzero(plan.entry_events[:, 0])[0])
    assert entry >= strategy.warmup_rows + 1
    assert not plan.entry_events[: strategy.warmup_rows].any()
    assert plan.target_weights[entry, 0] > plan.target_weights[entry, 1]
    assert plan.target_weights[entry].sum() == pytest.approx(1.0)
    np.testing.assert_allclose(
        plan.target_weights[entry:],
        np.broadcast_to(plan.target_weights[entry], plan.target_weights[entry:].shape),
    )
    assert plan.strategy_metadata["rebalance_policy"] == "membership_change_only"
    assert plan.strategy_metadata["event_semantics"] == "target_requests_not_fills"
    assert "每月" in strategy.to_human_readable(_params())
    assert (
        _simulate(plan, market).total_trades == 2
    )  # Monthly requests do not force sell/buy churn.


@pytest.mark.parametrize("cutoff", [265, 310, 390])
def test_prefix_invariance_and_future_mutations_cannot_change_history(cutoff):
    strategy, market = get_strategy("causal_trend_allocation"), _market()
    full = strategy.make_signals(_params(), market)
    prefix = strategy.make_signals(_params(), _slice(market, 0, cutoff))
    _assert_same(full, prefix, slice(0, cutoff))
    future = deepcopy(market)
    future.indicator_matrix[cutoff:, :, 0] *= 20
    future.prices[cutoff:] *= 0.1
    future.highs[cutoff:] *= 100
    future.tradable[cutoff:] = False
    changed = strategy.make_signals(_params(), future)
    _assert_same(changed, prefix, slice(0, cutoff))


def test_column_permutation_and_equal_score_tie_break_are_identity_based():
    strategy, market = get_strategy("causal_trend_allocation"), _market(columns=3)
    market.indicator_matrix[:, :, 0] = market.indicator_matrix[:, :1, 0]
    market.prices[:] = market.prices[:, :1]
    plan = strategy.make_signals(_params(selection_count=0), market)
    order = [2, 0, 1]
    permuted = strategy.make_signals(
        _params(selection_count=0), _slice(market, 0, order=order)
    )
    for key in ("entry_events", "exit_events", "target_weights", "conviction"):
        np.testing.assert_allclose(
            getattr(plan, key)[:, order], getattr(permuted, key), atol=1e-12
        )
    assert plan.entry_events[:, 0].any() and not plan.entry_events[:, 1:].any()


def test_single_batch_and_scan_use_the_same_calendar_plan():
    strategy, market = get_strategy("causal_trend_allocation"), _market(columns=1)
    selected = _params()
    one = strategy.make_signals(selected, market)
    prepared = strategy.prepare(market)
    batch = prepared.evaluate_batch([selected, _params(momentum_lookback=3)])
    _assert_same(batch[0], one)
    _assert_same(strategy.evaluate_one(selected, market), one)
    month = np.asarray(market.dates, dtype="datetime64[M]")
    index = next(
        index for index in range(275, len(month)) if month[index] != month[index - 1]
    )
    prefix = _slice(market, 0, index + 1)
    history = pd.DataFrame(
        {
            "date": prefix.dates,
            "close": prefix.prices[:, 0],
            "open": prefix.prices[:, 0],
            "high": prefix.highs[:, 0],
            "low": prefix.lows[:, 0],
            "volume": np.full(len(prefix.dates), 1000.0),
        }
    )
    assert one.entry_events[index, 0]
    assert strategy.scan_today(selected, {}, history)[0]["side"] == "buy"


@pytest.mark.parametrize("missing", ["nan", "halt", "zero_raw"])
def test_missing_selected_member_keeps_its_slot_and_target_budget(missing):
    strategy, market = get_strategy("causal_trend_allocation"), _market()
    params = _params(selection_count=0)
    baseline = strategy.make_signals(params, market)
    held = int(np.argmax(baseline.target_weights[290]))
    other = 1 - held
    if missing == "nan":
        market.indicator_matrix[300:350, held, 0] = np.nan
    elif missing == "zero_raw":
        market.prices[300:350, held] = 0
    else:
        market.tradable[300:350, held] = False
    plan = strategy.make_signals(params, market)
    np.testing.assert_allclose(
        plan.target_weights[300:350],
        np.broadcast_to(plan.target_weights[299], plan.target_weights[300:350].shape),
    )
    assert not plan.entry_events[300:350, held].any()
    assert not plan.exit_events[300:350, held].any()
    assert not plan.entry_events[300:350, other].any()


def test_newly_listed_or_untradable_candidates_cannot_crowd_out_an_observed_member():
    strategy, market = get_strategy("causal_trend_allocation"), _market()
    market.indicator_matrix[:300, 1, 0] = np.nan
    market.prices[:300, 1] = np.nan
    market.indicator_matrix[300:, 1, 0] *= 100
    plan = strategy.make_signals(_params(selection_count=0), market)
    assert not plan.entry_events[:, 1].any()
    assert plan.target_weights[300:, 0].min() > 0


def test_reallocating_another_member_never_spends_a_halted_members_reserved_budget():
    strategy, market = get_strategy("causal_trend_allocation"), _market(columns=3)
    # Use identical returns and lexical ordering to choose S0/S1 initially.
    market.indicator_matrix[:, :, 0] = market.indicator_matrix[:, :1, 0]
    market.prices[:] = market.prices[:, :1]
    market.highs[:] = market.prices * 1.01
    market.lows[:] = market.prices * 0.99
    market.tradable[300:370, 0] = False
    market.indicator_matrix[300:, 1, 0] *= 0.4
    plan = strategy.make_signals(_params(), market)
    entrants = np.flatnonzero(plan.entry_events[300:370, 2]) + 300
    assert len(entrants) > 0
    np.testing.assert_allclose(
        plan.target_weights[300:370, 0], plan.target_weights[299, 0]
    )
    assert (plan.target_weights[300:370].sum(axis=1) <= 1 + 1e-12).all()


def test_monthly_requests_do_not_reset_holding_age_and_executor_enforces_actual_fill_lock():
    strategy, market = (
        get_strategy("causal_trend_allocation"),
        _market(rows=350, columns=1),
    )
    before = strategy.make_signals(_params(), market)
    first = int(np.flatnonzero(before.entry_events[:, 0])[0])
    # First membership entry is 2023-12-29; Jan-01 is a calendar reaffirmation.
    month = np.asarray(market.dates, dtype="datetime64[M]")
    reaffirm = next(
        index
        for index in range(first + 1, len(month))
        if month[index] != month[index - 1]
    )
    market.indicator_matrix[reaffirm + 4 :, 0, 0] *= 0.3
    market.prices[reaffirm + 4 :, 0] *= 0.3
    market.highs[reaffirm + 4 :, 0] *= 0.3
    market.lows[reaffirm + 4 :, 0] *= 0.3
    plan = strategy.make_signals(_params(), market)
    dates = np.asarray(market.dates, dtype="datetime64[D]").astype(int)
    exit_row = int(np.flatnonzero(plan.exit_events[:, 0])[0])
    assert plan.entry_events[reaffirm, 0]
    assert dates[exit_row] - dates[first] >= 30
    assert (
        dates[exit_row] - dates[reaffirm] < 30
    )  # Logical membership age was preserved.
    assert not plan.force_exit_signals.any()
    # A genuinely empty evaluation account buys at the monthly request. Its
    # later ordinary exit is a standing request, admitted only at 30 actual days.
    locked_end = int(np.searchsorted(dates, dates[reaffirm] + 30))
    short_market = _slice(market, reaffirm, locked_end)
    short = _simulate(plan.sliced(reaffirm, locked_end), short_market)
    assert short.total_trades == 1 and short.final_shares[0] > 0
    full = _simulate(
        plan.sliced(reaffirm, locked_end + 2), _slice(market, reaffirm, locked_end + 2)
    )
    assert full.total_trades == 2 and full.final_shares[0] == 0
    assert (
        full.final_cash < 100_000
    )  # Real conservative fills/fees, no fabricated profit.


def test_qfq_decisions_are_separate_from_raw_stress_prices_and_costs():
    strategy, market = (
        get_strategy("causal_trend_allocation"),
        _market(rows=310, columns=1),
    )
    plan = strategy.make_signals(_params(per_symbol_cap=1), market)
    raw = deepcopy(market)
    raw.prices[:] = 20
    raw.highs[:] = 22
    raw.lows[:] = 19
    entry = int(np.flatnonzero(plan.entry_events[:, 0])[0])
    raw.highs[entry + 1, 0] = 25
    _assert_same(strategy.make_signals(_params(per_symbol_cap=1), raw), plan)
    trace = _simulate(plan, raw)
    assert trace.cost_basis[0] == pytest.approx(25)
    assert trace.final_shares[0] == 2500  # Declared 50% at raw Close=20.
    assert trace.final_cash == pytest.approx(100_000 - 2500 * 25 * 1.005)
    assert trace.total_trades == 1


def test_execution_snapshot_is_preserved_and_incompatible_contracts_are_rejected():
    strategy = get_strategy("causal_trend_allocation")
    params = _params()
    params.execution_snapshot = {
        "model": "target_weight",
        "per_symbol_cap": 0.37,
        "total_exposure_cap": 0.73,
        "min_holding_calendar_days": 30,
        "buy_price_model": "max_high_t_minus_1_t_t_plus_1",
        "sell_price_model": "trigger_day_low",
    }
    snapshot = deepcopy(params.execution_snapshot)
    assert strategy.execution_params(params) == snapshot
    assert params.execution_snapshot == snapshot
    params.execution_snapshot["min_holding_calendar_days"] = 10
    with pytest.raises(ValueError, match="30-calendar"):
        strategy.make_signals(params, _market())


def test_dates_and_parameter_levels_fail_closed():
    strategy = get_strategy("causal_trend_allocation")
    market = _market()
    market.dates = []
    with pytest.raises(ValueError, match="calendar dates"):
        strategy.make_signals(_params(), market)
    with pytest.raises(ValueError, match="parameter level"):
        strategy.execution_params(_params(momentum_lookback=4))


def test_declining_or_unobserved_histories_do_not_produce_positions():
    strategy, market = get_strategy("causal_trend_allocation"), _market()
    market.indicator_matrix[:, 0, 0] = 100 * np.exp(
        -0.002 * np.arange(len(market.dates))
    )
    market.tradable[:, 1] = False
    plan = strategy.make_signals(_params(), market)
    assert not plan.entry_events.any()
    assert not plan.exit_events.any()
    assert np.count_nonzero(plan.target_weights) == 0


def test_new_strategy_uses_existing_gross_tax_net_execution_accounting_once():
    strategy, market = (
        get_strategy("causal_trend_allocation"),
        _market(rows=310, columns=1),
    )
    plan = strategy.make_signals(_params(per_symbol_cap=1), market)
    entry = int(np.flatnonzero(plan.entry_events[:, 0])[0])
    dividend = np.zeros(market.prices.shape)
    dividend[entry + 10, 0] = 1.0
    actions = CorporateActionSlice(dividend, np.ones_like(dividend))
    execution = DEFAULT_FILL_PRICE_POLICY.build(
        market.prices,
        market.highs,
        market.lows,
        corporate_actions=actions,
    )
    trace = simulate_portfolio(
        plan,
        market,
        initial_cash=100_000,
        lot_size=1,
        commission_rate=0.005,
        min_holding_days=30,
        dividend_withholding_rate=0.20,
        execution_prices=execution,
    )
    assert trace.gross_dividend_cash == pytest.approx(trace.final_shares[0])
    assert trace.dividend_tax_cost == pytest.approx(trace.final_shares[0] * 0.20)
    assert trace.net_dividend_cash == pytest.approx(trace.final_shares[0] * 0.80)


@pytest.mark.parametrize("group", ["a_share", "hk", "us"])
def test_single_and_batch_execution_consume_identical_declared_targets(group):
    strategy, market = get_strategy("causal_trend_allocation"), _market(rows=310)
    market.market = group
    params = [_params(), _params(per_symbol_cap=1)]
    plans = strategy.prepare(market).evaluate_batch(params)
    evaluator = FastEvaluator(
        SimpleNamespace(
            initial_capital=100_000,
            lot_sizes={group: 1},
            commission_rate=0.005,
            min_holding_days=30,
            fx_rates={group: 1.0},
            withholding_rates={group: 0.20 if group == "hk" else 0.0},
        ),
        group,
    )
    inputs = {
        "indicator_matrix": market.indicator_matrix,
        "price_matrix": market.prices,
        "cash_baseline": np.full(len(market.dates), 100_000),
        "tradable": market.tradable,
        "execution_prices": DEFAULT_FILL_PRICE_POLICY.build(
            market.prices, market.highs, market.lows
        ),
    }
    scalar = [evaluator.evaluate(trade_plan=plan, **inputs) for plan in plans]
    batched = evaluator.evaluate_batch(plans, workers=2, **inputs)
    for left, right in zip(scalar, batched):
        assert left.strategy_return == right.strategy_return
        assert left.final_asset == right.final_asset
        assert left.total_trades == right.total_trades
        assert left.signal_event_count == right.signal_event_count
        assert left.net_dividend_cash == right.net_dividend_cash


def test_local_genetic_safely_stops_with_real_schema_and_large_budget_using_fake_receipts():
    strategy = get_strategy("causal_trend_allocation")
    problem = SearchProblem(
        strategy.parameter_schema,
        "synthetic-only",
        "synthetic-only",
        155000,
        "synthetic-data",
        "synthetic-execution",
        "synthetic-window",
        "close",
    )
    solver = create_solver("local_genetic")
    solver.initialize(
        problem,
        {
            "random_seed": 20260726,
            "phase1_random_samples": 30000,
            "duplicate_retry_limit": 64,
        },
    )
    seen = set()
    for _iteration in range(100):
        if solver.should_stop():
            break
        batch = solver.ask(128)
        for index in range(len(batch)):
            key = tuple(sorted(batch.parameters_at(index).items()))
            assert key not in seen
            seen.add(key)
        solver.tell(
            EvaluationBatch(
                candidate_ids=batch.candidate_ids,
                raw_metrics=tuple({} for _ in range(len(batch))),
                objective_scores=np.zeros(len(batch)),
                gate_decisions=tuple(GateDecision(True) for _ in range(len(batch))),
                feasible=np.ones(len(batch), dtype=bool),
                failure_reasons=tuple(() for _ in range(len(batch))),
            )
        )
    assert solver.should_stop()
    assert solver.stop_reason == "search_stalled"
    assert solver.total_issued == len(seen) <= 3600 < problem.budget
    print(
        json.dumps(
            {
                "synthetic_solver_probe": True,
                "budget": problem.budget,
                "issued": solver.total_issued,
                "space": 3600,
                "stop_reason": solver.stop_reason,
            }
        )
    )
