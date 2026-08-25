from datetime import date, timedelta

import numpy as np
import pytest

from src.fundamental_embedding.api import FundamentalPricingSnapshot
from src.fundamental_embedding.industry import IndustryRelativeSnapshot
from src.fundamental_embedding.valuation_features import (
    CURRENT_VALUATION_FEATURE_CONTRACT,
    CurrentValuationFeatureBuilder,
    VALUATION_FEATURE_NAMES,
)


def _snapshots():
    symbols = ("000001", "000002", "000003", "000004")
    feature_date = date(2026, 8, 18)
    fundamental = FundamentalPricingSnapshot(
        feature_date=feature_date,
        symbols=symbols,
        feature_names=("earnings_yield",),
        values=np.ones((4, 1), dtype=float),
        availability_mask=np.ones((4, 1), dtype=bool),
    )
    industry = IndustryRelativeSnapshot(
        feature_date=feature_date,
        symbols=symbols,
        feature_names=("industry_relative:earnings_yield",),
        values=np.zeros((4, 1), dtype=float),
        availability_mask=np.ones((4, 1), dtype=bool),
        industry_codes=("C01", "C01", "C01", "C01"),
        peer_scopes=("industry", "industry", "industry", "industry"),
        peer_counts=np.full(4, 4, dtype=np.int64),
    )
    return fundamental, industry


def _rows(feature_date: date):
    return [
        {
            "symbol": "000001",
            "evaluation_date": feature_date.isoformat(),
            "beta": 0.8,
            "capm_cost_of_equity": 0.07,
            "market_cost_growth_status": "solved",
            "market_implied_growth_5y": 0.10,
            "fundamental_growth": 0.08,
        },
        {
            "symbol": "000002",
            "evaluation_date": feature_date.isoformat(),
            "beta": 1.0,
            "capm_cost_of_equity": 0.08,
            "market_cost_growth_status": "solved",
            "market_implied_growth_5y": 0.20,
            "fundamental_growth": 0.10,
        },
        {
            "symbol": "000003",
            "evaluation_date": feature_date.isoformat(),
            "beta": 1.2,
            "capm_cost_of_equity": 0.09,
            "market_cost_growth_status": "solved",
            "market_implied_growth_5y": 0.30,
            "fundamental_growth": 0.15,
        },
        {
            "symbol": "000004",
            "evaluation_date": feature_date.isoformat(),
            "beta": 1.4,
            "capm_cost_of_equity": 0.10,
            "market_cost_growth_status": "not_estimable_no_positive_equity_cash_flow",
        },
    ]


def test_current_valuation_features_separate_expectations_and_peer_ranks():
    fundamental, industry = _snapshots()
    result = CurrentValuationFeatureBuilder().build(
        fundamental, industry, _rows(fundamental.feature_date)
    )

    assert result.feature_names == VALUATION_FEATURE_NAMES
    assert result.metadata["contract"] == CURRENT_VALUATION_FEATURE_CONTRACT
    assert result.metadata["historical_walk_forward_eligible"] is False
    assert result.metadata["current_taxonomy_must_not_be_backfilled"] is True
    assert result.availability_mask[:, 0].all()
    assert result.availability_mask[:3, 2].all()
    assert not result.availability_mask[3, 2]
    np.testing.assert_allclose(result.values[:3, 4], [-1.0, 0.0, 1.0])
    np.testing.assert_allclose(result.values[:3, 5], [-1.0, 0.0, 1.0])


def test_current_valuation_features_reject_future_valuation_data():
    fundamental, industry = _snapshots()
    rows = _rows(fundamental.feature_date)
    rows[0]["evaluation_date"] = (
        fundamental.feature_date + timedelta(days=1)
    ).isoformat()

    with pytest.raises(ValueError, match="must equal feature_date"):
        CurrentValuationFeatureBuilder().build(fundamental, industry, rows)
