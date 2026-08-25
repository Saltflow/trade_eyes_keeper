from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.industry_evaluation import IndustryRelativeDataset
from src.fundamental_embedding.valuation_context import (
    VALUATION_CONTEXT_FEATURE_NAMES,
    build_historical_valuation_context,
)


def test_valuation_context_ranks_and_quality_fields_preserve_missingness():
    dates = tuple(date(2024, 1, 1) + timedelta(days=90 * i) for i in range(2))
    feature_dates = tuple(item for item in dates for _ in range(3))
    labels = tuple(item + timedelta(days=30) for item in feature_dates)
    symbols = tuple(f"S{i:02d}" for i in range(6))
    names = tuple(f"f{i}" for i in range(19)) + (
        "valuation:beta",
        "valuation:capm_cost_of_equity",
        "valuation:market_implied_growth_5y",
        "valuation:market_vs_fundamental_growth",
        "valuation:fundamental_growth",
    )
    names += tuple(f"industry_relative:{name}" for name in names[19:])
    values = np.zeros((6, len(names)), dtype=float)
    values[:, :19] = 1.0
    values[:, 19:24] = np.arange(30, dtype=float).reshape(6, 5)
    values[:, 24:] = values[:, 19:24]
    mask = np.ones_like(values, dtype=bool)
    mask[0, 21] = False
    mask[0, 26] = False
    dataset = FundamentalPricingDataset(
        feature_dates=feature_dates,
        label_end_dates=labels,
        symbols=symbols,
        feature_names=names,
        values=values,
        availability_mask=mask,
        forward_returns=np.zeros(6),
        excess_returns=np.zeros(6),
    ).validate()
    result = build_historical_valuation_context(
        IndustryRelativeDataset(
            dataset=dataset,
            peer_context=np.ones(6, dtype=bool),
            coverage_by_date=tuple(
                {
                    "feature_date": item.isoformat(),
                    "eligible_for_industry_evaluation": True,
                }
                for item in dates
            ),
        )
    )
    assert result.feature_names[19:] == VALUATION_CONTEXT_FEATURE_NAMES
    assert not bool(result.availability_mask[0, 21])
    assert result.values[0, 25] == 0.0
    assert result.availability_mask[:, 24].all()
    assert result.metadata["missing_values_are_not_median_imputed"] is True
