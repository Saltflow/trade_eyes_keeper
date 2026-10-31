"""Synthetic account fixtures exercise the ledger, never live-source readiness."""

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import FastEvaluator, buy_and_hold_nav, simulate_portfolio
from src.backtest.execution import ExecutionPriceSlice, build_corporate_action_schedule
from src.backtest.in_kind import (
    advance_in_kind,
    in_kind_concentration,
    merge_in_kind_prices,
    tiered_allocation,
)
from src.core.ref_portfolio import Holding, RefPortfolio, RefPortfolioManager
from src.data.in_kind_distributions import InKindDistributionTerms
from src.data.market_history import CorporateAction
from src.search.config import ExecutionConfig
from src.strategy import StrategyMarketData, TradePlan


def _inputs(**overrides):
    terms = {
        "asset_code": "EXTERNAL",
        "asset_currency": "HKD",
        "allocation_rule": "linear_floor",
        "shares_per_parent_share": 0.1,
        "minimum_parent_shares": 0.0,
        "eligible": True,
        "account_election": "shares",
        "actual_delivery_date": date(2026, 1, 5),
        "actual_delivery_published_at": date(2026, 1, 5),
        "delivery_source_url": "fixture:delivery",
        "delivery_source_sha256": "b" * 64,
        "record_date": date(2026, 1, 3),
        "evidence_published_at": date(2026, 1, 1),
        "source_url": "fixture:declaration",
        "source_sha256": "a" * 64,
        "disposal_policy": "sell_on_delivery",
        "fractional_policy": "cash_in_lieu",
        "cash_in_lieu_per_asset_share": 20.0,
        "cash_in_lieu_gross_per_asset_share": 20.0,
        "cash_in_lieu_payment_date": date(2026, 1, 6),
        "cash_in_lieu_published_at": date(2026, 1, 6),
        "cash_in_lieu_source_url": "fixture:settlement",
        "cash_in_lieu_source_sha256": "c" * 64,
        "fee_per_asset_share": 0.0,
        "withholding_per_asset_share": 0.0,
        "minimum_cash_payment": 0.0,
    }
    terms.update(overrides)
    action = CorporateAction(
        code="PARENT",
        action_type="in_kind_distribution",
        ex_date=date(2026, 1, 2),
        published_at=date(2026, 1, 1),
        source="synthetic_fixture",
        in_kind_terms=InKindDistributionTerms(**terms),
    )
    dates = pd.date_range("2026-01-01", periods=7, freq="D")
    external = pd.DataFrame(
        {
            "date": dates,
            "raw_close": 20.0,
            "raw_low": 20.0,
            "fx_rate": 1.0,
            "tradable": True,
            "external_cash_dividend": 0.0,
            "external_share_multiplier": 1.0,
        }
    )
    return action, dates, {"EXTERNAL": external}


def _schedule(**overrides):
    action, dates, external = _inputs(**overrides)
    return build_corporate_action_schedule(
        [action], dates, ["PARENT"], external_prices=external
    )


def _plan(model="cash_cap", *, sell_day=None, buy_day=0):
    entries = np.zeros((7, 1), dtype=bool)
    exits = np.zeros_like(entries)
    entries[buy_day] = True
    if sell_day is not None:
        exits[sell_day] = True
    return TradePlan(
        buy_signals=entries,
        sell_signals=exits,
        buy_priority=np.ones((7, 1)),
        sell_priority=np.ones((7, 1)),
        buy_cash_limit=1_000,
        sell_cash_limit=1_000,
        warmup_rows=0,
        entry_events=entries,
        exit_events=exits,
        conviction=np.ones((7, 1)),
        target_weights=np.ones((7, 1)),
        date_ordinals=np.arange(7),
        execution={
            "model": model,
            "per_symbol_cap": 1.0,
            "total_exposure_cap": 1.0,
        },
    )


def test_ex_date_receivable_is_not_cash_until_actual_delivery():
    schedule = _schedule().in_kind
    quantity = np.zeros(1)
    fraction = np.zeros(1)
    parent = np.array([100.0])
    changes, values = [], []
    for row in range(7):
        cash, value = advance_in_kind(
            row, parent, *schedule.kernel_args(), quantity, fraction
        )
        changes.append(cash)
        values.append(value)
        if row == 1:
            # Selling the parent after capture cannot cancel the entitlement.
            parent[:] = 0.0
    assert changes == [0, 0, 0, 0, 200, 0, 0]
    assert values == [0, 200, 200, 200, 0, 0, 0]


def test_cash_target_batch_static_and_reference_nav_reconcile():
    action, dates, external = _inputs()
    actions = _schedule()
    prices = np.array([[10.0]] + [[8.0]] * 6)
    execution = ExecutionPriceSlice(
        prices, prices, prices, np.ones((7, 1), bool), actions
    )
    evaluator = FastEvaluator(
        ExecutionConfig(
            initial_capital=1000,
            commission_rate=0,
            min_holding_days=0,
            lot_sizes={"hk": 1},
            fx_rates={"hk": 1},
            withholding_rates={"hk": 0.2},
        ),
        "hk",
    )
    inputs = {
        "indicator_matrix": np.zeros((7, 1, 1)),
        "price_matrix": prices,
        "cash_baseline": np.full(7, 1000.0),
        "execution_prices": execution,
    }
    plans = [_plan(), _plan("target_weight")]
    results = [evaluator.evaluate(trade_plan=p, **inputs) for p in plans]
    results.extend(evaluator.evaluate_batch([plans[0], plans[0]], workers=1, **inputs))
    for result in results:
        assert result.final_asset == pytest.approx(1000)
        assert result.final_cash == pytest.approx(200)
        assert result.final_shares.tolist() == [100]
    static = buy_and_hold_nav(prices, prices, 1000, 1, 0, corporate_actions=actions)
    np.testing.assert_allclose(static, 1000)

    pf = RefPortfolio(inception_date="2026-01-01", cash=0)
    pf.holdings["PARENT"] = Holding("PARENT", 100, 10)
    manager = RefPortfolioManager()
    for day in dates[1:]:
        pf, _ = manager.apply_corporate_actions(
            pf,
            [action],
            day.date(),
            withholding_rate=0.2,
            external_prices=external,
        )
        assert pf.nav({"PARENT": 8.0, "EXTERNAL": 20.0}) == pytest.approx(1000)
        pf = RefPortfolio.from_dict(pf.to_dict())
    assert pf.cash == pytest.approx(200)
    replay, changes = manager.apply_corporate_actions(
        pf,
        [action],
        dates[-1].date(),
        withholding_rate=0.2,
        external_prices=external,
    )
    assert replay.cash == pf.cash
    assert changes == []


@pytest.mark.parametrize(
    "change,match",
    [
        ({"actual_delivery_date": None}, "actual_delivery_date"),
        ({"eligible": None}, "eligibility"),
        ({"account_election": "cash"}, "election"),
        ({"allocation_rule": "tiered_ads_election"}, "tiered terms"),
        ({"cash_in_lieu_per_asset_share": None}, "cash_in_lieu_per_asset_share"),
        ({"fee_per_asset_share": None}, "fee_per_asset_share"),
        ({"withholding_per_asset_share": None}, "withholding_per_asset_share"),
        ({"minimum_cash_payment": None}, "minimum_cash_payment"),
        ({"evidence_published_at": date(2026, 1, 3)}, "causal publication"),
        (
            {"actual_delivery_published_at": date(2026, 1, 6)},
            "contemporaneous evidence",
        ),
        ({"cash_in_lieu_published_at": date(2026, 1, 7)}, "contemporaneous evidence"),
    ],
)
def test_unresolved_or_future_evidence_never_becomes_executable(change, match):
    with pytest.raises(ValueError, match=match):
        _schedule(**change)


def test_fractional_cash_threshold_fees_and_tax_are_explicit():
    schedule = _schedule(
        fee_per_asset_share=1.0,
        withholding_per_asset_share=2.0,
        minimum_cash_payment=11.0,
    ).in_kind
    quantities, fractions = np.zeros(1), np.zeros(1)
    cash = 0
    values = []
    for row in range(7):
        addition, value = advance_in_kind(
            row, np.array([105.0]), *schedule.kernel_args(), quantities, fractions
        )
        cash += addition
        values.append(value)
    # 10 delivered shares sold for 200, less explicit 30 costs; fractional
    # net 10 is below the independently declared cash threshold.
    assert cash == pytest.approx(170)
    assert values[1] == pytest.approx(210)
    assert fractions[0] == 0


def test_sliced_windows_do_not_inherit_pre_window_entitlements_or_future_prices():
    actions = _schedule()
    sliced = actions.sliced(2, 7).in_kind
    quantities, fractions = np.zeros(1), np.zeros(1)
    for row in range(5):
        cash, value = advance_in_kind(
            row, np.array([100.0]), *sliced.kernel_args(), quantities, fractions
        )
        assert cash == value == 0
    action, dates, external = _inputs()
    external["EXTERNAL"].loc[0:1, ["raw_close", "fx_rate"]] = np.nan
    with pytest.raises(ValueError, match="valuation/FX coverage gap"):
        build_corporate_action_schedule(
            [action], dates, ["PARENT"], external_prices=external
        )


def test_reference_requires_capture_and_external_valuation():
    action, _, external = _inputs(disposal_policy="hold")
    pf = RefPortfolio(inception_date="2026-01-01", cash=0)
    pf.holdings["PARENT"] = Holding("PARENT", 100, 10)
    manager = RefPortfolioManager()
    with pytest.raises(ValueError, match="holdings reconciliation"):
        manager.apply_corporate_actions(
            pf,
            [action],
            date(2026, 1, 3),
            withholding_rate=0.2,
            external_prices=external,
        )
    captured, _ = manager.apply_corporate_actions(
        pf,
        [action],
        date(2026, 1, 2),
        withholding_rate=0.2,
        external_prices=external,
    )
    assert captured.nav({"PARENT": 8.0}) is None
    assert captured.missing_price_codes({"PARENT": 8.0}) == ["EXTERNAL"]
    assert pf.in_kind_positions == {}


def test_nonzero_fees_never_deduct_confirmed_fractional_net_cash_twice():
    schedule = _schedule(
        shares_per_parent_share=0.15,
        cash_in_lieu_per_asset_share=10.0,
        fee_per_asset_share=1.0,
        withholding_per_asset_share=2.0,
    ).in_kind
    quantities, fractions = np.zeros(1), np.zeros(1)
    audit = np.zeros((1, 14))
    cash = 0.0
    for row in range(7):
        addition, _ = advance_in_kind(
            row,
            np.array([10.0]),
            *schedule.kernel_args(),
            quantities,
            fractions,
            audit,
        )
        cash += addition
    assert cash == pytest.approx(20 - 1 - 2 + 0.5 * 10)
    details = schedule.describe(quantities, fractions, audit)
    flows = details["in_kind_cashflows"]
    assert sum(item["amount"] for item in flows if item["affects_cash"]) == cash
    assert {item["kind"]: item["amount"] for item in flows} == {
        "delivery_fee": -1,
        "delivery_tax": -2,
        "fractional_net_cash": 5,
        "disposal_proceeds": 20,
    }


def test_known_fractional_net_receivable_is_fixed_before_actual_payment():
    action, dates, external = _inputs(
        cash_in_lieu_per_asset_share=10.0,
        cash_in_lieu_published_at=date(2026, 1, 4),
    )
    external["EXTERNAL"].loc[3:, "raw_close"] = 200.0
    schedule = build_corporate_action_schedule(
        [action], dates, ["PARENT"], external_prices=external
    ).in_kind
    quantities, fractions = np.zeros(1), np.zeros(1)
    cash, values = 0.0, []
    for row in range(7):
        addition, value = advance_in_kind(
            row, np.array([5.0]), *schedule.kernel_args(), quantities, fractions
        )
        cash += addition
        values.append(value)
        if row == 3:
            assert cash == 0.0
    assert values[1:5] == [10, 10, 5, 5]
    assert cash == 5.0


def test_external_dividend_or_split_with_no_settlement_contract_remains_blocked():
    for column, value in (
        ("external_cash_dividend", 1.0),
        ("external_share_multiplier", 2.0),
    ):
        action, dates, external = _inputs(disposal_policy="hold")
        external["EXTERNAL"].loc[2, column] = value
        with pytest.raises(ValueError, match="external .* is not resolved"):
            build_corporate_action_schedule(
                [action], dates, ["PARENT"], external_prices=external
            )
        # A future action after both the actual sale and fractional payment
        # cannot retroactively invalidate a resolved distribution.
        action.in_kind_terms.disposal_policy = "sell_on_delivery"
        external["EXTERNAL"].loc[2, column] = 0 if "cash" in column else 1
        external["EXTERNAL"].loc[6, column] = value
        build_corporate_action_schedule(
            [action], dates, ["PARENT"], external_prices=external
        )


def test_reference_terms_cannot_change_after_capture_even_on_same_day():
    action, _, external = _inputs(disposal_policy="hold")
    pf = RefPortfolio(inception_date="2026-01-01", cash=0)
    pf.holdings["PARENT"] = Holding("PARENT", 100, 10)
    manager = RefPortfolioManager()
    captured, _ = manager.apply_corporate_actions(
        pf,
        [action],
        date(2026, 1, 2),
        withholding_rate=0.2,
        external_prices=external,
    )
    action.in_kind_terms.fee_per_asset_share = 2.0
    with pytest.raises(ValueError, match="terms changed; reconciliation"):
        manager.apply_corporate_actions(
            captured,
            [action],
            date(2026, 1, 2),
            withholding_rate=0.2,
            external_prices=external,
        )


def test_parent_same_day_bonus_does_not_recalculate_captured_rights():
    action, dates, external = _inputs(disposal_policy="hold")
    bonus = SimpleNamespace(
        code="PARENT",
        action_type="bonus",
        ex_date=date(2026, 1, 2),
        share_multiplier=2.0,
    )
    schedule = build_corporate_action_schedule(
        [bonus, action], dates, ["PARENT"], external_prices=external
    )
    prices = np.array([[10.0]] + [[4.0]] * 6)
    nav = buy_and_hold_nav(prices, prices, 1000, 1, 0, corporate_actions=schedule)
    np.testing.assert_allclose(nav, 1000)


def test_donated_equity_consumes_target_exposure_budget_and_hhi():
    action, dates, external = _inputs(disposal_policy="hold")
    schedule = build_corporate_action_schedule(
        [action], dates, ["PARENT", "OTHER"], external_prices=external
    )
    prices = np.column_stack(([10.0] + [8.0] * 6, np.full(7, 10.0)))
    entries, exits = np.zeros((7, 2), bool), np.zeros((7, 2), bool)
    entries[0, 0] = True
    entries[2, 1] = True
    weights = np.full((7, 2), 0.4)
    weights[:2] = [0.8, 0]
    plan = TradePlan(
        buy_signals=entries,
        sell_signals=exits,
        buy_priority=np.ones((7, 2)),
        sell_priority=np.ones((7, 2)),
        buy_cash_limit=1000,
        sell_cash_limit=1000,
        entry_events=entries,
        exit_events=exits,
        conviction=np.ones((7, 2)),
        target_weights=weights,
        date_ordinals=np.arange(7),
        warmup_rows=0,
        execution={
            "model": "target_weight",
            "per_symbol_cap": 1.0,
            "total_exposure_cap": 0.8,
        },
    )
    evaluator = FastEvaluator(
        ExecutionConfig(
            initial_capital=1000,
            commission_rate=0,
            min_holding_days=0,
            lot_sizes={"hk": 1},
            fx_rates={"hk": 1},
            withholding_rates={"hk": 0.2},
        ),
        "hk",
    )
    result = evaluator.evaluate(
        trade_plan=plan,
        indicator_matrix=np.zeros((7, 2, 1)),
        price_matrix=prices,
        cash_baseline=np.full(7, 1000.0),
        execution_prices=ExecutionPriceSlice(
            prices, prices, prices, np.ones((7, 2), bool), schedule
        ),
    )
    donated = sum(item["value"] for item in result.in_kind_positions)
    assert donated == 160
    assert result.final_position_pct <= 80
    assert result.final_cash >= 200
    assert result.final_cash + prices[-1].dot(result.final_shares) + donated == 1000
    single = _schedule(disposal_policy="hold").in_kind
    assert in_kind_concentration(
        np.array([100.0]),
        np.array([8.0]),
        single.events,
        single.prices,
        single.fx_rates,
        np.array([10.0]),
        np.zeros(1),
        2,
        1000.0,
    ) == pytest.approx(0.8**2 + 0.2**2)


def test_truncated_external_history_is_not_extended_into_future_exposure():
    action, dates, external = _inputs(disposal_policy="hold")
    external["EXTERNAL"] = external["EXTERNAL"].iloc[:3]
    with pytest.raises(ValueError, match="history ends before exposure"):
        build_corporate_action_schedule(
            [action], dates, ["PARENT"], external_prices=external
        )


def test_multiple_parent_histories_merge_complementary_dates_without_mutation():
    _, _, external = _inputs()
    original = external["EXTERNAL"].copy(deep=True)
    left = {"EXTERNAL": original.iloc[:4].copy()}
    right = {"EXTERNAL": original.iloc[2:].copy()}
    merged = merge_in_kind_prices(left, right)
    pd.testing.assert_frame_equal(merged["EXTERNAL"], original)
    pd.testing.assert_frame_equal(left["EXTERNAL"], original.iloc[:4])
    pd.testing.assert_frame_equal(right["EXTERNAL"], original.iloc[2:])
    # Complete rows from one parent may fill explicitly missing observations.
    left["EXTERNAL"].loc[2, "fx_rate"] = np.nan
    pd.testing.assert_frame_equal(
        merge_in_kind_prices(right, left)["EXTERNAL"], original
    )


@pytest.mark.parametrize(
    "column,value",
    [
        ("raw_close", 21.0),
        ("fx_rate", 1.1),
        ("external_cash_dividend", 1.0),
        ("external_share_multiplier", 2.0),
    ],
)
def test_conflicting_external_histories_never_silently_overwrite(column, value):
    _, _, external = _inputs()
    conflicting = external["EXTERNAL"].iloc[2:].copy()
    conflicting.loc[2, column] = value
    with pytest.raises(ValueError, match=f"conflicting .* EXTERNAL .* {column}"):
        merge_in_kind_prices(external, {"EXTERNAL": conflicting})


def test_reference_status_separates_rights_and_cashflows_and_reconciles_nav():
    action, dates, external = _inputs()
    pf = RefPortfolio(inception_date="2026-01-01", cash=0)
    pf.holdings["PARENT"] = Holding("PARENT", 100, 10)
    manager = RefPortfolioManager()
    pf, _ = manager.apply_corporate_actions(
        pf,
        [action],
        dates[1].date(),
        withholding_rate=0.2,
        external_prices=external,
    )
    status = manager.get_status(pf, {"PARENT": 8, "EXTERNAL": 20})
    assert status["nav"] == status["cash"] + sum(
        item["market_value"] for item in status["holdings"]
    ) + sum(item["value"] for item in status["in_kind_positions"])
    assert [item["code"] for item in status["holdings"]] == ["PARENT"]
    position = status["in_kind_positions"][0]
    assert position["pending_quantity"] == 10
    assert position["delivered"] is False
    assert position["ex_date"] == "2026-01-02"
    assert position["delivery_date"] == "2026-01-05"
    assert status["in_kind_cashflows"] == []
    missing = manager.get_status(pf, {"PARENT": 8})
    assert missing["nav"] is None
    assert missing["in_kind_positions"][0]["value"] is None
    pf, _ = manager.apply_corporate_actions(
        pf,
        [action],
        dates[-1].date(),
        withholding_rate=0.2,
        external_prices=external,
    )
    settled = manager.get_status(pf, {"PARENT": 8})
    assert settled["nav"] == 1000
    assert settled["in_kind_positions"][0]["quantity"] == 0
    assert settled["in_kind_cashflows"][0]["amount"] == 200


def _tiered_inputs(*, eligible=True, election="cash", share_history=False):
    action, dates, external = _inputs(
        allocation_rule="tiered_ads_election",
        shares_per_parent_share=1 / 3900,
        minimum_parent_shares=3900,
        minimum_cash_parent_shares=1500,
        declared_cash_election_per_unit=101,
        cash_parent_lot=500,
        cash_per_lot=13,
        minimum_cash_payment=39,
        cash_currency="HKD",
        cash_fx_rate=1.0,
        cash_fx_published_at=date(2026, 1, 1),
        cash_fx_source_url="fixture:cash-fx",
        cash_fx_source_sha256="d" * 64,
        eligible=eligible,
        account_election=election,
        cash_settlement_date=date(2026, 1, 6),
        cash_settlement_published_at=date(2026, 1, 1),
        cash_settlement_source_url="fixture:cash-payment",
        cash_settlement_source_sha256="e" * 64,
        cash_settlement_fee=1.0,
        cash_settlement_withholding_amount=2.0,
        minimum_sale_lock_days=40,
        lock_anchor="last_distribution_issuance",
        last_distribution_issuance_date=date(2026, 1, 3),
        last_distribution_issuance_published_at=date(2026, 1, 3),
        last_distribution_issuance_source_url="fixture:issuance",
        last_distribution_issuance_source_sha256="f" * 64,
        tradable_from_date=date(2026, 2, 12),
        tradable_from_published_at=date(2026, 1, 3),
        tradable_from_source_url="fixture:release",
        tradable_from_source_sha256="1" * 64,
    )
    if share_history:
        dates = pd.date_range("2026-01-01", periods=50, freq="D")
        external["EXTERNAL"] = external["EXTERNAL"].iloc[:1].reindex(range(50)).ffill()
        external["EXTERNAL"]["date"] = dates
        external["EXTERNAL"]["fx_rate"] = 7.8
    else:
        action.in_kind_terms.actual_delivery_date = None
        action.in_kind_terms.actual_delivery_published_at = None
        action.in_kind_terms.delivery_source_url = None
        action.in_kind_terms.delivery_source_sha256 = None
        external = {}
    return action, dates, external


@pytest.mark.parametrize(
    "quantity,eligible,election,shares,cash",
    [
        (1499, True, "shares", 0, 0),
        (1500, True, "shares", 0, 39),
        (1999, True, "cash", 0, 39),
        (2000, True, "cash", 0, 52),
        (3899, True, "shares", 0, 91),
        (3900, True, "shares", 1, 0),
        (4399, True, "shares", 1, 0),
        (4400, True, "shares", 1, 13),
        (4400, True, "cash", 0, 114),
        (7800, True, "cash", 0, 202),
        (3900, False, "cash", 0, 91),
        (4400, False, "cash", 0, 104),
        (10000, False, "shares", 0, 260),
    ],
)
def test_tiered_disclosed_formula_is_not_linear_or_globally_thresholded(
    quantity,
    eligible,
    election,
    shares,
    cash,
):
    action, _, _ = _tiered_inputs(eligible=eligible, election=election)
    assert tiered_allocation(quantity, action.in_kind_terms) == (shares, cash)


@pytest.mark.parametrize("eligible,gross", [(True, 114), (False, 104)])
def test_tiered_native_cash_leg_reconciles_scalar_batch_benchmark_and_reference(
    eligible,
    gross,
):
    action, dates, external = _tiered_inputs(eligible=eligible)
    actions = build_corporate_action_schedule(
        [action], dates, ["PARENT"], external_prices=external
    )
    # The native cash is HKD although the declared ADS is quoted in USD.
    # Account FX=2 must scale HKD cash exactly once, without needing USD quotes.
    initial = 4400 * 10 * 2
    prices = np.array([[10.0]] + [[10.0 - gross / 4400]] * 6)
    execution = ExecutionPriceSlice(
        prices, prices, prices, np.ones((7, 1), bool), actions
    )
    evaluator = FastEvaluator(
        ExecutionConfig(
            initial_capital=initial,
            commission_rate=0,
            min_holding_days=0,
            lot_sizes={"hk": 1},
            fx_rates={"hk": 2},
            withholding_rates={"hk": 0.2},
        ),
        "hk",
    )
    plan = _plan()
    plan.buy_cash_limit = initial
    plan.sell_cash_limit = initial
    inputs = {
        "indicator_matrix": np.zeros((7, 1, 1)),
        "price_matrix": prices,
        "cash_baseline": np.full(7, initial),
        "execution_prices": execution,
    }
    results = [evaluator.evaluate(trade_plan=plan, **inputs)]
    results += evaluator.evaluate_batch([plan], workers=1, **inputs)
    for result in results:
        assert result.final_cash == pytest.approx((gross - 3) * 2)
        assert result.final_asset == pytest.approx(initial - 6, abs=0.01)
        assert (
            sum(
                item["amount"]
                for item in result.in_kind_cashflows
                if item["affects_cash"]
            )
            == result.final_cash
        )
    static = buy_and_hold_nav(
        prices * 2,
        prices * 2,
        initial,
        1,
        0,
        corporate_actions=actions.scaled(2),
    )
    assert static[-1] == pytest.approx(initial - 6)
    pf = RefPortfolio(inception_date="2026-01-01", cash=0)
    pf.holdings["PARENT"] = Holding("PARENT", 4400, 20)
    manager = RefPortfolioManager()
    for day in dates[1:]:
        pf, _ = manager.apply_corporate_actions(
            pf,
            [action],
            day.date(),
            withholding_rate=0.2,
            fx_rate=2,
            external_prices=external,
        )
        status = manager.get_status(pf, {"PARENT": prices[-1, 0] * 2})
        assert status["nav"] == pytest.approx(initial - 6, abs=0.01)
        pf = RefPortfolio.from_dict(pf.to_dict())
    assert pf.cash == pytest.approx((gross - 3) * 2)


def test_tiered_share_and_small_residual_have_independent_currency_and_sale_lock():
    action, dates, external = _tiered_inputs(election="shares", share_history=True)
    actions = build_corporate_action_schedule(
        [action], dates, ["PARENT"], external_prices=external
    ).in_kind
    quantities, fractions = np.zeros(1), np.zeros(1)
    audit = np.zeros((1, 14))
    cash = 0.0
    for row in range(len(dates)):
        addition, _ = advance_in_kind(
            row,
            np.array([4400.0]),
            *actions.kernel_args(),
            quantities,
            fractions,
            audit,
        )
        cash += addition
        if dates[row] < pd.Timestamp("2026-02-12") and row >= 1:
            assert quantities[0] == 1
    # HK13 residual is paid even though below the HK39 small-holder threshold.
    assert audit[0, 12] == 10
    assert audit[0, 6] == pytest.approx(20 * 7.8)
    assert cash == pytest.approx(20 * 7.8 + 10)
    assert quantities[0] == 0
    action.in_kind_terms.tradable_from_date = date(2026, 1, 6)
    with pytest.raises(ValueError, match="40|lock"):
        build_corporate_action_schedule(
            [action], dates, ["PARENT"], external_prices=external
        )


def test_issuer_cash_threshold_is_checked_before_account_withholding():
    action, dates, external = _inputs(
        minimum_cash_payment=100,
        cash_in_lieu_gross_per_asset_share=200,
        cash_in_lieu_per_asset_share=160,
        withholding_per_asset_share=40,
    )
    schedule = build_corporate_action_schedule(
        [action], dates, ["PARENT"], external_prices=external
    ).in_kind
    quantities, fractions = np.zeros(1), np.zeros(1)
    audit = np.zeros((1, 14))
    cash = 0
    for row in range(7):
        added, _ = advance_in_kind(
            row,
            np.array([5.0]),
            *schedule.kernel_args(),
            quantities,
            fractions,
            audit,
        )
        cash += added
    assert quantities[0] == 0
    assert cash == 80
    assert audit[0, 4] == 80
    assert audit[0, 5] == 0


@pytest.mark.parametrize("quantity", [100, 4400])
def test_estimated_stock_connect_cash_policy_is_explicit_and_taxed_once(quantity):
    action, dates, external = _tiered_inputs(eligible=False)
    terms = action.in_kind_terms
    terms.asset_currency = "USD"
    terms.settlement_basis = "stock_connect_estimate"
    terms.estimation_policy_id = "stock_connect_estimate_v1:" + "a" * 64
    terms.estimation_parent_currency = "HKD"
    terms.estimation_assumptions = [
        "synthetic test account: estimated payment; 20% withholding"
    ]
    terms.estimation_sources = ["fixture:declared-terms", "fixture:account-policy"]
    terms.estimated_cash_settlement_date = date(2026, 1, 6)
    terms.estimated_cash_settlement_fee = 0.0
    terms.estimated_cash_settlement_withholding_rate = 0.2
    terms.estimated_cash_fx_rate = 1.0
    terms.estimated_nominee_cash_per_parent_share = 101 / 3900
    terms.estimated_minimum_cash_payment = 0.0
    terms.cash_settlement_date = None
    terms.cash_settlement_published_at = None
    terms.cash_settlement_source_url = None
    terms.cash_settlement_source_sha256 = None
    terms.cash_settlement_fee = None
    terms.cash_settlement_withholding_amount = None
    actions = build_corporate_action_schedule(
        [action], dates, ["PARENT"], external_prices=external
    )
    gross = quantity * 101 / 3900
    if quantity == 100:
        assert gross == pytest.approx(2.58974358, abs=2e-8)
    initial = quantity * 10 * 2
    prices = np.array([[10.0]] + [[10 - gross / quantity]] * 6)
    execution = ExecutionPriceSlice(
        prices, prices, prices, np.ones((7, 1), bool), actions
    )
    evaluator = FastEvaluator(
        ExecutionConfig(
            initial_capital=initial,
            commission_rate=0,
            min_holding_days=0,
            lot_sizes={"hk": 1},
            fx_rates={"hk": 2},
            withholding_rates={"hk": 0.2},
        ),
        "hk",
    )
    results = []
    for model in ("cash_cap", "target_weight"):
        plan = _plan(model)
        plan.buy_cash_limit = initial
        result = evaluator.evaluate(
            trade_plan=plan,
            indicator_matrix=np.zeros((7, 1, 1)),
            price_matrix=prices,
            cash_baseline=np.full(7, initial),
            execution_prices=execution,
        )
        results.append(result)
        assert result.final_cash == pytest.approx(gross * 0.8 * 2)
        assert result.final_asset == pytest.approx(initial - gross * 0.2 * 2, abs=0.01)
        assert (
            result.in_kind_positions[0]["settlement_basis"] == "stock_connect_estimate"
        )
        assert result.in_kind_cashflows[-1]["policy_hash"] == terms.estimation_policy_id
        assert result.in_kind_positions[0]["allocation_rule"] == "nominee_pro_rata_cash"
        assert terms.allocation_rule == "tiered_ads_election"
        if model == "cash_cap":
            batched = evaluator.evaluate_batch(
                [plan],
                workers=1,
                indicator_matrix=np.zeros((7, 1, 1)),
                price_matrix=prices,
                cash_baseline=np.full(7, initial),
                execution_prices=execution,
            )[0]
            assert batched.final_cash == result.final_cash
            assert batched.final_asset == result.final_asset
        trace = simulate_portfolio(
            plan,
            StrategyMarketData(
                indicator_matrix=np.zeros((7, 1, 1)),
                prices=prices,
                dates=[str(day.date()) for day in dates],
                symbols=["PARENT"],
            ),
            initial,
            1,
            0,
            execution_price_scale=2,
            dividend_withholding_rate=0.2,
            execution_prices=execution,
        )
        assert trace.final_cash == result.final_cash
        assert trace.nav_series[-1] == result.final_asset
        assert trace.total_return_pct == trace.selected_basket_hold_return
    assert terms.actual_delivery_date is None
    assert terms.cash_settlement_date is None
    pf = RefPortfolio(inception_date="2026-01-01", cash=0)
    pf.holdings["PARENT"] = Holding("PARENT", quantity, 20)
    manager = RefPortfolioManager()
    for day in dates[1:]:
        pf, _ = manager.apply_corporate_actions(
            pf,
            [action],
            day.date(),
            withholding_rate=0.2,
            fx_rate=2,
            external_prices=external,
        )
        assert pf.nav({"PARENT": prices[-1, 0] * 2}) == pytest.approx(
            initial - gross * 0.2 * 2
        )
    assert pf.cash == pytest.approx(gross * 0.8 * 2)


def test_no_event_concentration_retains_allocation_free_fast_path(monkeypatch):
    import src.backtest.in_kind as ledger

    shares = np.array([80.0, 20.0])
    prices = np.array([10.0, 10.0])
    events = np.empty((0, 30))
    empty = np.empty((1, 0))
    empty_quantities = np.empty(0)

    def unexpected_allocation(*args, **kwargs):
        raise AssertionError("ordinary markets must not allocate per-day HHI state")

    monkeypatch.setattr(ledger.np, "zeros", unexpected_allocation)
    assert in_kind_concentration(
        shares,
        prices,
        events,
        empty,
        empty,
        empty_quantities,
        empty_quantities,
        0,
        1000.0,
    ) == pytest.approx(0.8**2 + 0.2**2)


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("distribution_ratio", [0.1, 2.0])
def test_target_symbol_cap_includes_same_security_donated_rights(
    declared,
    distribution_ratio,
):
    action, dates, external = _inputs(
        disposal_policy="hold",
        shares_per_parent_share=distribution_ratio,
    )
    schedule = build_corporate_action_schedule(
        [action],
        dates,
        ["PARENT", "EXTERNAL"],
        external_prices=external,
    )
    prices = np.column_stack(([10.0] + [8.0] * 6, np.full(7, 20.0)))
    entries, exits = np.zeros((7, 2), bool), np.zeros((7, 2), bool)
    entries[0, 0], entries[1, 1] = True, True
    weights = np.full((7, 2), 0.2)
    weights[0, 1] = 0
    labels = [str(day.date()) for day in dates]
    plan = TradePlan(
        buy_signals=entries,
        sell_signals=exits,
        buy_priority=np.ones((7, 2)),
        sell_priority=np.ones((7, 2)),
        buy_cash_limit=1000,
        sell_cash_limit=1000,
        entry_events=entries,
        exit_events=exits,
        conviction=np.ones((7, 2)),
        target_weights=weights if declared else None,
        date_ordinals=np.arange(7),
        warmup_rows=0,
        dates=labels,
        symbols=["PARENT", "EXTERNAL"],
        execution={
            "model": "target_weight",
            "per_symbol_cap": 0.2,
            "total_exposure_cap": 0.8,
        },
    )
    market = StrategyMarketData(
        indicator_matrix=np.zeros((7, 2, 1)),
        prices=prices,
        dates=labels,
        symbols=["PARENT", "EXTERNAL"],
        highs=prices,
        lows=prices,
        tradable=np.ones((7, 2), bool),
    )
    evaluator = FastEvaluator(
        ExecutionConfig(
            initial_capital=1000,
            commission_rate=0,
            min_holding_days=0,
            lot_sizes={"hk": 1},
            fx_rates={"hk": 1},
            withholding_rates={"hk": 0.2},
        ),
        "hk",
    )
    result = evaluator.evaluate(
        trade_plan=plan,
        indicator_matrix=market.indicator_matrix,
        price_matrix=prices,
        cash_baseline=np.full(7, 1000.0),
        execution_prices=ExecutionPriceSlice(
            prices,
            prices,
            prices,
            np.ones((7, 2), bool),
            schedule,
        ),
    )
    forced_shares = 20 * distribution_ratio
    forced_value = forced_shares * 20
    direct_value = result.final_shares[1] * 20
    active_budget = max(result.final_asset * 0.2 - forced_value, 0)
    assert direct_value <= active_budget + 1e-8
    assert result.in_kind_positions[0]["quantity"] == forced_shares
    if distribution_ratio == 0.1:
        assert (
            direct_value == 160
        )  # Previously bought 200, yielding a 24% total weight.
    else:
        assert forced_value > result.final_asset * 0.2
        assert direct_value == 0  # A passive breach cannot authorize another buy.

    pf = RefPortfolio(
        inception_date="2026-01-01",
        cash=800,
        market_group="hk",
        strategy_run_id="run",
        strategy_id="strategy",
        last_rebalance_date="2026-01-01",
    )
    pf.holdings["PARENT"] = Holding("PARENT", 20, 10, "2026-01-01")
    manager = RefPortfolioManager()
    captured, _ = manager.apply_corporate_actions(
        pf,
        [action],
        "2026-01-02",
        withholding_rate=0.2,
        external_prices=external,
    )
    updated, trades = manager.rebalance_plan(
        captured,
        plan,
        market,
        "2026-01-02",
        run_id="run",
        strategy_id="strategy",
        lot_size=1,
        commission_rate=0,
        min_holding_days=0,
        fx_rate=1,
        force=True,
    )
    status = manager.get_status(updated, {"PARENT": 8, "EXTERNAL": 20})
    direct = updated.holdings.get("EXTERNAL")
    assert (direct.shares * 20 if direct else 0) <= max(
        status["nav"] * 0.2 - forced_value, 0
    ) + 1e-8
    assert status["in_kind_positions"][0]["quantity"] == forced_shares
    assert status["in_kind_positions"][0]["delivered"] is False
    assert not any(
        trade.action == "sell" and trade.code == "EXTERNAL" for trade in trades
    )
