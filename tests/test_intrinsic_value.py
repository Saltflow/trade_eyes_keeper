from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pytest

from src.fundamental_embedding.intrinsic_evaluation import (
    IntrinsicValueIntervalEvaluator,
    IntrinsicValueWalkForwardEvaluator,
    PriceIntervalConfig,
    PriceIntervalParameters,
)
from src.fundamental_embedding.capital_cost import CapitalCostEstimate
from src.fundamental_embedding.intrinsic_value import (
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    SubjectiveRiskAdjustment,
    ValuationSnapshot,
)


def _snapshot(**overrides) -> ValuationSnapshot:
    values = {
        "symbol": "TEST",
        "evaluation_date": date(2024, 6, 28),
        "market_date": date(2024, 6, 28),
        "current_price": 10.0,
        "shares": 1_000_000.0,
        "revenue_per_share": 12.0,
        "earnings_per_share": 1.2,
        "free_cash_flow_per_share": 0.9,
        "book_value_per_share": 6.0,
        "dividend_per_share": 0.45,
        "roe": 0.18,
        "growth": 0.06,
        "payout_ratio": 0.375,
        "cash_conversion": 0.75,
        "earnings_stability": 0.85,
        "dividend_stability": 0.90,
        "fcf_history_count": 4,
        "dividend_history_count": 5,
        "financial_age_days": 70,
    }
    values.update(overrides)
    return ValuationSnapshot(**values)


def test_intrinsic_value_is_independent_of_market_price():
    engine = IntrinsicValueEngine()
    first = engine.estimate(_snapshot(current_price=10.0))
    second = engine.estimate(_snapshot(current_price=100.0))

    assert first.fair_value == pytest.approx(second.fair_value)
    assert first.buy_price == pytest.approx(second.buy_price)
    assert first.margin_of_safety != second.margin_of_safety
    assert (
        first.market_implied_growth["cash_flow_dcf"]
        != second.market_implied_growth["cash_flow_dcf"]
    )
    assert sum(first.gate.values()) == pytest.approx(1.0)
    assert sum(value > 0 for value in first.gate.values()) <= 2
    dcf = next(
        item for item in first.experts if item.expert_id == "cash_flow_dcf"
    )
    assert dcf.assumptions["discount_rates"] == pytest.approx(
        [0.09, 0.08, 0.07]
    )
    assert dcf.assumptions["discount_rate_kind"] == (
        "investor_hurdle_with_market_cost_floor"
    )
    assert first.reverse_dcf["cash_flow_dcf"][
        "feeds_intrinsic_value"
    ] is False
    assert first.reverse_dcf["cash_flow_dcf"][
        "market_implied_explicit_growth"
    ] != second.reverse_dcf["cash_flow_dcf"][
        "market_implied_explicit_growth"
    ]


def test_subjective_risk_changes_buy_price_not_cost_of_capital():
    engine = IntrinsicValueEngine()
    baseline = engine.estimate(_snapshot())
    conservative = engine.estimate(
        _snapshot(),
        SubjectiveRiskAdjustment(
            price_haircut=0.20,
            reason="unresolved restructuring and governance risk",
            expires_at=date(2025, 6, 30),
        ),
    )

    assert conservative.fair_value == pytest.approx(baseline.fair_value)
    assert conservative.buy_price < baseline.buy_price
    assert conservative.risk.reason


def test_cash_flow_dcf_uses_equity_cost_without_net_debt_bridge():
    capital_cost = CapitalCostEstimate(
        evaluation_date=date(2024, 6, 28),
        assumptions_as_of=date(2024, 6, 28),
        risk_free_rate=0.02,
        market_risk_premium=0.05,
        raw_beta=0.8,
        adjusted_beta=0.8,
        beta_observations=200,
        cost_of_equity=0.06,
        pre_tax_cost_of_debt=0.03,
        effective_tax_rate=0.2,
        market_equity=9_000_000.0,
        interest_bearing_debt=1_000_000.0,
        available_cash=500_000.0,
        net_debt=500_000.0,
        equity_weight=0.9,
        debt_weight=0.1,
        wacc=0.055,
        discount_rate_kind="point_in_time_wacc",
    )
    estimate = IntrinsicValueEngine().estimate(
        _snapshot(capital_cost=capital_cost)
    )
    dcf = next(
        item for item in estimate.experts if item.expert_id == "cash_flow_dcf"
    )

    assert dcf.assumptions["discount_rate_kind"] == (
        "investor_hurdle_with_market_cost_floor"
    )
    earnings = next(
        item for item in estimate.experts
        if item.expert_id == "earnings_power_dcf"
    )
    assert earnings.assumptions["discount_rate_kind"] == (
        "investor_hurdle_with_market_cost_floor"
    )
    assert dcf.assumptions["discount_rates"] == pytest.approx(
        [0.08, 0.075, 0.07]
    )
    assert dcf.assumptions["net_debt_per_share"] is None
    assert dcf.assumptions["cash_flow_kind"] == (
        "fcfe_proxy_ttm_ocf_minus_capex_per_share"
    )
    assert "no net-debt bridge" in dcf.assumptions["cash_flow_warning"]


def test_low_beta_capm_is_diagnostic_and_personal_hurdle_sets_rates():
    capital_cost = CapitalCostEstimate(
        evaluation_date=date(2024, 6, 28),
        assumptions_as_of=date(2024, 6, 28),
        risk_free_rate=0.02,
        market_risk_premium=0.06,
        raw_beta=0.35,
        adjusted_beta=0.406,
        beta_observations=250,
        cost_of_equity=0.04436,
        pre_tax_cost_of_debt=0.03,
        effective_tax_rate=0.2,
        market_equity=9_000_000.0,
        interest_bearing_debt=1_000_000.0,
        available_cash=500_000.0,
        net_debt=500_000.0,
        equity_weight=0.9,
        debt_weight=0.1,
        wacc=0.041,
        discount_rate_kind="point_in_time_wacc",
    )

    estimate = IntrinsicValueEngine().estimate(
        _snapshot(capital_cost=capital_cost)
    )
    dcf = next(
        item for item in estimate.experts if item.expert_id == "cash_flow_dcf"
    )
    policy = estimate.required_return_policy

    assert dcf.assumptions["discount_rates"] == pytest.approx(
        [0.08, 0.075, 0.07]
    )
    assert policy["market_cost_of_equity"]["base"] == pytest.approx(0.04436)
    assert policy["investor_required_return"]["base"] == pytest.approx(0.075)
    assert policy["applied_required_return"]["base"] == pytest.approx(0.075)
    assert policy["terminal_growth"] == pytest.approx(0.02)
    assert estimate.reverse_dcf["cash_flow_dcf"]["required_return"] == (
        pytest.approx(0.075)
    )


def test_high_market_cost_remains_required_return_floor():
    capital_cost = CapitalCostEstimate(
        evaluation_date=date(2024, 6, 28),
        assumptions_as_of=date(2024, 6, 28),
        risk_free_rate=0.02,
        market_risk_premium=0.09,
        raw_beta=1.0,
        adjusted_beta=1.0,
        beta_observations=250,
        cost_of_equity=0.11,
        pre_tax_cost_of_debt=None,
        effective_tax_rate=None,
        market_equity=None,
        interest_bearing_debt=None,
        available_cash=None,
        net_debt=None,
        equity_weight=None,
        debt_weight=None,
        wacc=None,
        discount_rate_kind="point_in_time_cost_of_equity",
    )

    estimate = IntrinsicValueEngine().estimate(
        _snapshot(capital_cost=capital_cost)
    )
    dcf = next(
        item for item in estimate.experts if item.expert_id == "cash_flow_dcf"
    )

    assert dcf.assumptions["discount_rates"] == pytest.approx(
        [0.12, 0.11, 0.10]
    )


@pytest.mark.parametrize(
    "config",
    (
        IntrinsicValueConfig(
            investor_required_return_low=0.08,
            investor_required_return=0.075,
        ),
        IntrinsicValueConfig(
            investor_required_return_low=0.02,
            terminal_growth=0.02,
        ),
    ),
)
def test_invalid_investor_required_return_policy_fails_fast(config):
    with pytest.raises(ValueError, match="required return"):
        IntrinsicValueEngine(config)


def test_expired_subjective_risk_is_not_applied():
    engine = IntrinsicValueEngine()
    baseline = engine.estimate(_snapshot())
    expired = engine.estimate(
        _snapshot(),
        SubjectiveRiskAdjustment(
            price_haircut=0.50,
            reason="old risk",
            expires_at=date(2023, 12, 31),
        ),
    )

    assert expired.fair_value == pytest.approx(baseline.fair_value)
    assert expired.buy_price == pytest.approx(baseline.buy_price)
    assert expired.risk.reason == "configured_subjective_risk_expired"


def test_missing_fcf_dividend_and_book_falls_back_to_earnings_power():
    engine = IntrinsicValueEngine()
    estimate = engine.estimate(_snapshot(
        free_cash_flow_per_share=None,
        book_value_per_share=None,
        dividend_per_share=None,
        cash_conversion=None,
        payout_ratio=None,
        fcf_history_count=0,
        dividend_history_count=0,
    ))

    assert estimate.buy_price is not None
    assert estimate.gate["earnings_power_dcf"] == pytest.approx(1.0)
    assert estimate.gate["cash_flow_dcf"] == 0.0
    assert estimate.gate["dividend_discount"] == 0.0
    assert estimate.gate["residual_income"] == 0.0


def test_no_economic_anchor_produces_no_value_instead_of_mocking():
    engine = IntrinsicValueEngine()
    empty = replace(
        _snapshot(),
        earnings_per_share=None,
        free_cash_flow_per_share=None,
        book_value_per_share=None,
        dividend_per_share=None,
        roe=None,
    )
    estimate = engine.estimate(empty)

    assert estimate.fair_value is None
    assert estimate.buy_price is None
    assert "no_available_valuation_expert" in estimate.diagnostics


def test_long_horizon_metric_uses_cross_sectional_margin_ranking():
    evaluator = IntrinsicValueWalkForwardEvaluator(builder=None)
    rows = []
    for index in range(12):
        score = float(index) / 11.0
        rows.append({
            "horizon": 504,
            "evaluation_date": "2022-12-30",
            "forward_return": score * 0.8 - 0.2,
            "margin_of_safety": score,
            "fair_value_gap": score,
            "earnings_yield": score,
            "book_yield": 1.0 - score,
            "dividend_yield": score,
            "confidence": 0.8,
            "fair_value_reached": index >= 6,
        })

    metrics = evaluator._metrics(rows, 504)

    assert metrics["scores"]["margin_of_safety"]["mean_rank_ic"] == pytest.approx(
        1.0
    )
    assert metrics["scores"]["book_yield"]["mean_rank_ic"] == pytest.approx(-1.0)
    assert metrics["scores"]["margin_of_safety"][
        "mean_top_bottom_spread"
    ] > 0


def _interval_episode(
    *,
    beta: float = 1.0,
    risk: SubjectiveRiskAdjustment | None = None,
    path: np.ndarray | None = None,
) -> dict:
    return {
        "symbol": "TEST",
        "evaluation_date": date(2023, 4, 30),
        "market_date": date(2023, 4, 28),
        "next_report_date": date(2023, 8, 30),
        "current_price": 100.0,
        "fair_value_low": 90.0,
        "fair_value": 100.0,
        "fair_value_high": 110.0,
        "beta": beta,
        "cost_of_equity": 0.065,
        "risk": risk or SubjectiveRiskAdjustment(),
        "path": (
            path
            if path is not None
            else np.asarray([99.0, 100.0, 101.0], dtype=np.float64)
        ),
    }


def test_price_interval_objective_penalizes_width_instead_of_rewarding_it():
    evaluator = IntrinsicValueIntervalEvaluator(builder=None)
    episode = _interval_episode()
    narrow = PriceIntervalParameters(0.0, 0.02, 0.0, 0.0)
    wide = PriceIntervalParameters(0.0, 0.22, 0.0, 0.0)

    narrow_result = evaluator._episode_result(episode, narrow)
    wide_result = evaluator._episode_result(episode, wide)

    assert narrow_result["daily_coverage"] == 1.0
    assert wide_result["daily_coverage"] == 1.0
    assert narrow_result["relative_width"] < wide_result["relative_width"]
    assert narrow_result["objective_score"] > wide_result["objective_score"]


def test_price_interval_width_penalty_is_scaled_by_absolute_beta():
    evaluator = IntrinsicValueIntervalEvaluator(builder=None)
    parameters = PriceIntervalParameters(0.0, 0.08, 0.0, 0.0)

    low_beta = evaluator._episode_result(
        _interval_episode(beta=0.5), parameters
    )
    high_beta = evaluator._episode_result(
        _interval_episode(beta=1.5), parameters
    )

    assert low_beta["daily_coverage"] == high_beta["daily_coverage"]
    assert low_beta["beta_adjusted_width"] > high_beta[
        "beta_adjusted_width"
    ]
    assert low_beta["objective_score"] < high_beta["objective_score"]


def test_subjective_event_risk_moves_center_and_widens_downside_only():
    evaluator = IntrinsicValueIntervalEvaluator(builder=None)
    parameters = PriceIntervalParameters(0.5, 0.06, 0.0, 0.0)
    baseline = evaluator._forecast_values(
        _interval_episode(), parameters
    )
    stressed = evaluator._forecast_values(
        _interval_episode(
            risk=SubjectiveRiskAdjustment(
                adverse_event_probability=0.25,
                adverse_event_loss=0.40,
                uncertainty_multiplier=1.0,
                reason="explicit scenario",
                effective_from=date(2023, 1, 1),
            )
        ),
        parameters,
    )

    assert stressed[0] < baseline[0]
    assert stressed[1] < baseline[1]
    assert stressed[2] < baseline[2]
    stressed_half_up = stressed[2] / stressed[0] - 1.0
    stressed_half_down = 1.0 - stressed[1] / stressed[0]
    assert stressed_half_down > stressed_half_up


def test_historical_subjective_risk_requires_effective_date():
    evaluator = IntrinsicValueIntervalEvaluator(builder=None)
    evaluation_date = date(2024, 6, 30)
    undated = SubjectiveRiskAdjustment(
        price_haircut=0.20, reason="current opinion only"
    )
    dated = SubjectiveRiskAdjustment(
        price_haircut=0.20,
        reason="known at the time",
        effective_from=date(2024, 1, 1),
        expires_at=date(2024, 12, 31),
    )

    excluded = evaluator._historical_risk(undated, evaluation_date)
    included = evaluator._historical_risk(dated, evaluation_date)

    assert excluded.price_haircut == 0.0
    assert excluded.reason == "undated_subjective_risk_excluded_from_history"
    assert included == dated


def test_forced_interval_baseline_cannot_read_dcf_value():
    evaluator = IntrinsicValueIntervalEvaluator(builder=None)
    parameters = list(evaluator._parameter_grid(baseline=True))

    assert parameters
    assert all(item.valuation_weight == 0.0 for item in parameters)
    assert all(
        item.value_uncertainty_weight == 0.0 for item in parameters
    )


def test_interval_split_holds_out_latest_report_dates():
    evaluator = IntrinsicValueIntervalEvaluator(
        builder=None,
        interval_config=PriceIntervalConfig(validation_fraction=0.25),
    )
    episodes = [
        {
            **_interval_episode(),
            "evaluation_date": date(2020 + year, month, 1),
        }
        for year, month in (
            (0, 1),
            (0, 4),
            (0, 7),
            (0, 10),
            (1, 1),
            (1, 4),
            (1, 7),
            (1, 10),
        )
    ]

    calibration, validation, cutoff = evaluator._split_episodes(
        episodes, 0.25
    )

    assert len(calibration) == 6
    assert len(validation) == 2
    assert cutoff == "2021-07-01"
    assert max(item["evaluation_date"] for item in calibration) < min(
        item["evaluation_date"] for item in validation
    )
