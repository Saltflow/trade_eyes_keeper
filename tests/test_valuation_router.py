from __future__ import annotations

import pytest

from src.fundamental_embedding.valuation_router import (
    ValuationModel,
    residual_income_per_share,
    route_valuation_model,
    summarize_cash_flow_history,
)


def test_financial_industry_uses_residual_income_not_ocf_minus_capex():
    route = route_valuation_model(
        industry_code="J66",
        industry_name="monetary financial services",
        cash_flow_history=summarize_cash_flow_history((1.0, 2.0, 3.0)),
    )

    assert route.model == ValuationModel.RESIDUAL_INCOME
    assert route.reason == "financial_industry_ocf_not_fcfe"


def test_cyclical_industry_and_cash_peak_use_normalized_fcfe():
    industry_route = route_valuation_model(
        industry_code="G55",
        industry_name="water transport",
        cash_flow_history=summarize_cash_flow_history((1.0, 1.1, 1.2)),
    )
    peak_route = route_valuation_model(
        industry_code="C39",
        industry_name="other manufacturing",
        cash_flow_history=summarize_cash_flow_history((1.0, 1.1, 1.0, 3.0)),
        peak_ratio_threshold=1.75,
    )

    assert industry_route.model == ValuationModel.NORMALIZED_FCFE_DCF
    assert peak_route.model == ValuationModel.NORMALIZED_FCFE_DCF
    assert peak_route.reason == "cash_flow_peak_normalized"


def test_residual_income_retains_a_negative_terminal_residual():
    value = residual_income_per_share(
        book_value_per_share=10.0,
        roe=0.15,
        payout_ratio=0.40,
        cost_of_equity=0.08,
        terminal_growth=0.02,
        projection_years=5,
        roe_fade=0.50,
        minimum_discount_spread=0.0025,
    )

    assert value is not None
    assert value > 10.0
    assert (
        residual_income_per_share(
            book_value_per_share=10.0,
            roe=0.15,
            payout_ratio=0.40,
            cost_of_equity=0.021,
            terminal_growth=0.02,
            projection_years=5,
            roe_fade=0.50,
            minimum_discount_spread=0.0025,
        )
        is None
    )
