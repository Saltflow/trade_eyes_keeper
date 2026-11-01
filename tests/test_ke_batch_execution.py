"""Synthetic fixtures exercise the shared executor, never live market readiness."""

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import FastEvaluator, simulate_portfolio
from src.backtest.execution import (
    DEFAULT_FILL_PRICE_POLICY,
    CorporateActionSlice,
    ExecutionPriceSlice,
    build_corporate_action_schedule,
)
from src.data.in_kind_distributions import InKindDistributionTerms
from src.data.market_history import CorporateAction
from src.search.config import ExecutionConfig
from src.strategy import StrategyMarketData, TradePlan


def _case(entries, *, scores=None, exits=None, prices=None, symbols=None, **options):
    entries = np.asarray(entries, dtype=bool)
    rows, columns = entries.shape
    exits = np.zeros_like(entries) if exits is None else np.asarray(exits, bool)
    scores = np.full(entries.shape, 0.10) if scores is None else np.asarray(scores)
    prices = np.full(entries.shape, 10.0) if prices is None else np.asarray(prices)
    dates = pd.date_range("2026-01-01", periods=rows).strftime("%Y-%m-%d").tolist()
    symbols = symbols or [f"{index:06d}" for index in range(columns)]
    plan = TradePlan(
        buy_signals=entries,
        sell_signals=exits,
        buy_priority=np.where(entries, scores, -np.inf),
        sell_priority=np.where(exits, -scores, -np.inf),
        buy_cash_limit=0,
        sell_cash_limit=0,
        warmup_rows=0,
        dates=dates,
        symbols=symbols,
        entry_events=entries,
        exit_events=exits,
        conviction=np.asarray(scores, dtype=np.float64),
        execution={
            "model": "target_weight",
            "allocation_mode": "ke_equal_batch",
            "per_symbol_cap": 0.20,
            "total_exposure_cap": 1.0,
            "max_new_entries": 5,
            "min_holding_calendar_days": 0,
            **options,
        },
    )
    market = StrategyMarketData(
        indicator_matrix=np.zeros((rows, columns, 1), dtype=np.float32),
        dates=dates,
        symbols=symbols,
        prices=prices,
        tradable=np.ones(entries.shape, dtype=bool),
    )
    execution = ExecutionPriceSlice(prices, prices, prices, market.tradable)
    return plan, market, execution


def _run(case, *, cash=100_000, lot=1, fee=0, withholding=0):
    plan, market, prices = case
    return simulate_portfolio(
        plan,
        market,
        initial_cash=cash,
        lot_size=lot,
        commission_rate=fee,
        dividend_withholding_rate=withholding,
        execution_prices=prices,
    )


def test_existing_holding_does_not_consume_five_new_entry_slots_or_get_topped_up():
    entries = [[True] + [False] * 6, [True] * 7]
    scores = [[0.99] + [0.1] * 6, [0.99, 0.16, 0.15, 0.14, 0.13, 0.12, 0.11]]
    trace = _run(_case(entries, scores=scores))
    assert trace.final_shares.tolist() == [2000, 1600, 1600, 1600, 1600, 1600, 0]
    assert trace.total_trades == 6
    assert trace.final_cash == 0


def test_top_five_use_float64_ke_then_stable_code_independent_of_column_order():
    symbols = ["G", "F", "E", "D", "C", "B", "A"]
    scores = np.array([[0.2, 0.19, 0.18, 0.17, 0.1, 0.1000000001, 0.1]])
    case = _case(np.ones((1, 7), bool), scores=scores, symbols=symbols)
    trace = _run(case)
    selected = {code for code, shares in zip(symbols, trace.final_shares) if shares}
    assert selected == {"G", "F", "E", "D", "B"}
    # The fifth-place distinction disappears in float32: never downcast Ke.
    assert np.float32(scores[0, 4]) == np.float32(scores[0, 5])
    case = _case(np.ones((1, 7), bool), scores=np.full((1, 7), 0.1), symbols=symbols)
    trace = _run(case)
    assert {code for code, shares in zip(symbols, trace.final_shares) if shares} == {
        "A",
        "B",
        "C",
        "D",
        "E",
    }
    permuted = ["A", "C", "G", "B", "F", "D", "E"]
    trace = _run(_case(np.ones((1, 7), bool), symbols=permuted))
    assert {code for code, shares in zip(permuted, trace.final_shares) if shares} == {
        "A",
        "B",
        "C",
        "D",
        "E",
    }


def test_equal_fee_inclusive_budgets_obey_lots_and_leave_rounding_cash():
    prices = np.array([[9, 10, 11, 12, 13]], dtype=float)
    trace = _run(_case([[True] * 5], prices=prices), cash=1000, lot=10, fee=0.01)
    costs = trace.final_shares * prices[0] * 1.01
    assert trace.final_shares.tolist() == [20, 10, 10, 10, 10]
    assert np.all(costs <= 200)
    assert trace.final_cash == pytest.approx(1000 - costs.sum())
    assert trace.final_cash > 0
    one = _run(_case([[True]]), fee=0.005, lot=100)
    assert one.final_shares[0] == 1900
    assert one.final_cash == pytest.approx(100_000 - 1900 * 10 * 1.005)


def test_entry_cap_uses_current_nav_and_never_trims_appreciated_holdings():
    case = _case([[True, False], [True, True]], prices=[[10, 10], [20, 10]])
    trace = _run(case)
    assert trace.final_shares.tolist() == [2000, 2400]
    assert trace.final_cash == 56_000
    assert trace.total_trades == 2
    assert trace.daily_values[-1] == 120_000


def test_exposure_budget_deducts_actual_appreciated_holdings_before_equal_split():
    entries = [[True] + [False] * 5, [True] * 6]
    prices = [[10] * 6, [20] + [10] * 5]
    trace = _run(_case(entries, prices=prices, total_exposure_cap=0.5))
    # NAV=110k, market budget=55k, existing equity=20k, five share 35k.
    assert trace.final_shares.tolist() == [1000, 700, 700, 700, 700, 700]
    assert trace.final_cash == 55_000
    assert trace.total_trades == 6


def test_new_high_ke_cannot_replace_or_reduce_fully_funded_old_holdings():
    entries = [[True] * 5 + [False], [True] * 6]
    trace = _run(_case(entries, scores=[[0.1] * 6, [0.1] * 5 + [0.99]]))
    assert trace.final_shares.tolist() == [2000] * 5 + [0]
    assert trace.total_trades == 5


def test_unfilled_sell_does_not_release_cash_or_position_before_real_fill():
    entries = [[True] * 5 + [False], [False] * 5 + [True], [False] * 5 + [True]]
    exits = [[False] * 6, [True] + [False] * 5, [True] + [False] * 5]
    plan, market, prices = _case(entries, exits=exits)
    market.tradable[1, 0] = False
    trace = _run(
        (
            plan.sliced(0, 2),
            replace(
                market,
                dates=market.dates[:2],
                prices=market.prices[:2],
                tradable=market.tradable[:2],
                indicator_matrix=market.indicator_matrix[:2],
            ),
            ExecutionPriceSlice(
                prices.valuation_prices[:2],
                prices.buy_prices[:2],
                prices.sell_prices[:2],
                market.tradable[:2],
            ),
        )
    )
    assert trace.final_shares.tolist() == [2000] * 5 + [0]
    trace = _run((plan, market, prices))
    assert trace.final_shares.tolist() == [0] + [2000] * 5
    assert trace.total_trades == 7


def test_split_odd_lot_residual_remains_held_and_is_not_topped_up():
    case = _case([[True], [False], [True]], exits=[[False], [True], [False]])
    plan, market, prices = case
    multipliers = np.ones((3, 1))
    multipliers[1, 0] = 1.005
    schedule = CorporateActionSlice(np.zeros((3, 1)), multipliers)
    trace = _run((plan, market, replace(prices, corporate_actions=schedule)), lot=100)
    assert trace.final_shares[0] == pytest.approx(10)
    assert trace.total_trades == 2
    assert trace.final_cash == 100_000


def test_persistent_exit_waits_for_calendar_holding_lock():
    entries = np.zeros((32, 1), bool)
    entries[0] = True
    exits = np.ones_like(entries)
    exits[0] = False
    plan, market, prices = _case(entries, exits=exits, min_holding_calendar_days=30)
    prices = replace(prices, sell_prices=np.arange(1, 33).reshape(-1, 1))
    trace = _run((plan, market, prices))
    assert trace.total_trades == 2
    assert trace.final_shares[0] == 0
    assert trace.final_cash == 80_000 + 2000 * 31
    assert trace.daily_values[29] == 100_000


def test_cash_dividend_tax_is_preserved_without_topping_up_the_old_holding():
    plan, market, prices = _case([[True, False], [True, True]])
    dividends = np.array([[0, 0], [1, 0]], dtype=float)
    schedule = CorporateActionSlice(dividends, np.ones((2, 2)))
    trace = _run(
        (plan, market, replace(prices, corporate_actions=schedule)), withholding=0.2
    )
    assert trace.final_shares.tolist() == [2000, 2032]
    assert trace.gross_dividend_cash == 2000
    assert trace.dividend_tax_cost == 400
    assert trace.net_dividend_cash == 1600
    # Shared corporate-action arrays retain their existing float32 precision.
    assert trace.daily_values[-1] == pytest.approx(101_600, abs=0.0001)


def test_conservative_stress_fills_and_last_row_pending_buy_are_preserved():
    entries = np.zeros((40, 1), bool)
    entries[1] = entries[-1] = True
    exits = np.zeros_like(entries)
    exits[32] = True
    plan, market, _ = _case(entries, exits=exits, min_holding_calendar_days=30)
    market.highs = np.full((40, 1), 11.0)
    market.highs[2] = 13
    market.lows = np.full((40, 1), 9.0)
    prices = DEFAULT_FILL_PRICE_POLICY.build(market.prices, market.highs, market.lows)
    trace = _run((plan, market, prices), lot=100, fee=0.005)
    assert trace.total_trades == 2
    assert trace.final_shares[0] == 0
    assert trace.pending_order_count == 1
    assert trace.final_cash == pytest.approx(
        100_000 - 1500 * 13 * 1.005 + 1500 * 9 * 0.995
    )


def test_fast_evaluator_and_shared_trace_agree_on_actual_batch_execution():
    case = _case([[True] * 6, [True] * 6], scores=[[0.1] * 6, [0.99] * 6])
    plan, market, prices = case
    trace = _run(case, lot=100, fee=0.005)
    config = ExecutionConfig(
        initial_capital=100_000,
        commission_rate=0.005,
        min_holding_days=0,
        lot_sizes={"a_share": 100},
        fx_rates={"a_share": 1},
        withholding_rates={"a_share": 0},
    )
    stats = FastEvaluator(config).evaluate(
        market.indicator_matrix,
        market.prices,
        np.full(2, 100_000.0),
        trade_plan=plan,
        execution_prices=prices,
        tradable=market.tradable,
    )
    np.testing.assert_array_equal(stats.final_shares, trace.final_shares)
    assert stats.final_cash == trace.final_cash
    assert stats.total_trades == trace.total_trades
    assert stats.final_asset == pytest.approx(trace.daily_values[-1], abs=0.005)


def test_in_kind_receivable_counts_in_nav_but_cannot_fund_entries_before_delivery():
    entries = np.zeros((7, 7), bool)
    entries[0, 0] = True
    entries[1, 1:6] = True
    entries[2:, 6] = True
    exits = np.zeros_like(entries)
    exits[1, 0] = True
    values = np.ones((7, 7))
    values[:, 0] = [10] + [8] * 6
    plan, market, prices = _case(
        entries,
        exits=exits,
        prices=values,
        symbols=["PARENT", "A", "B", "C", "D", "E", "F"],
    )
    terms = InKindDistributionTerms(
        asset_code="EXTERNAL",
        asset_currency="HKD",
        allocation_rule="linear_floor",
        shares_per_parent_share=0.1,
        minimum_parent_shares=0.0,
        eligible=True,
        account_election="shares",
        actual_delivery_date=date(2026, 1, 5),
        actual_delivery_published_at=date(2026, 1, 5),
        delivery_source_url="fixture:delivery",
        delivery_source_sha256="b" * 64,
        record_date=date(2026, 1, 3),
        evidence_published_at=date(2026, 1, 1),
        source_url="fixture:declaration",
        source_sha256="a" * 64,
        disposal_policy="sell_on_delivery",
        fractional_policy="cash_in_lieu",
        cash_in_lieu_per_asset_share=20.0,
        cash_in_lieu_gross_per_asset_share=20.0,
        cash_in_lieu_payment_date=date(2026, 1, 6),
        cash_in_lieu_published_at=date(2026, 1, 6),
        cash_in_lieu_source_url="fixture:settlement",
        cash_in_lieu_source_sha256="c" * 64,
        fee_per_asset_share=0.0,
        withholding_per_asset_share=0.0,
        minimum_cash_payment=0.0,
    )
    action = CorporateAction(
        code="PARENT",
        action_type="in_kind_distribution",
        ex_date=date(2026, 1, 2),
        published_at=date(2026, 1, 1),
        source="synthetic_fixture",
        in_kind_terms=terms,
    )
    external = pd.DataFrame(
        {
            "date": pd.to_datetime(market.dates),
            "raw_close": 20.0,
            "raw_low": 20.0,
            "fx_rate": 1.0,
            "tradable": True,
            "external_cash_dividend": 0.0,
            "external_share_multiplier": 1.0,
        }
    )
    actions = build_corporate_action_schedule(
        [action], market.dates, market.symbols, external_prices={"EXTERNAL": external}
    )
    trace = _run((plan, market, replace(prices, corporate_actions=actions)), cash=1000)
    assert trace.final_shares.tolist() == [0] + [192] * 5 + [40]
    np.testing.assert_allclose(trace.daily_values, 1000)
    assert trace.final_cash == 0
    assert trace.in_kind_cashflows[0]["date"] == "2026-01-05"


@pytest.mark.parametrize(
    "options",
    [
        {"max_new_entries": 6},
        {"max_new_entries": True},
        {"per_symbol_cap": 0.21},
        {"total_exposure_cap": np.nan},
        {"allocation_mode": "unknown"},
        {"model": "cash_cap"},
    ],
)
def test_invalid_explicit_mode_configuration_fails_closed(options):
    with pytest.raises(ValueError):
        _run(_case([[True]], **options))


def test_ke_does_not_accept_precomputed_weights_or_forced_early_exits():
    plan, market, prices = _case([[True]])
    plan.target_weights = np.ones((1, 1))
    with pytest.raises(ValueError, match="actual filled holdings"):
        _run((plan, market, prices))
    plan.target_weights = None
    plan.force_exit_signals = np.ones((1, 1), bool)
    with pytest.raises(ValueError, match="holding lock"):
        _run((plan, market, prices))
