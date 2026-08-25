from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.api import (
    PRICING_FEATURE_NAMES,
    FundamentalPricingDataset,
)
from src.fundamental_embedding.industry_evaluation import IndustryRelativeDataset
from src.fundamental_embedding.industry_factor_evaluation import (
    INDUSTRY_FACTOR_NAMES,
    industry_relative_factors,
)


def test_industry_relative_factors_keep_fixed_economic_directions():
    feature_names = PRICING_FEATURE_NAMES + tuple(
        f"industry_relative:{name}" for name in PRICING_FEATURE_NAMES
    )
    rows = 2
    values = np.zeros((rows, len(feature_names)), dtype=float)
    base_count = len(PRICING_FEATURE_NAMES)
    values[:, base_count:] = 1.0
    dataset = FundamentalPricingDataset(
        feature_dates=(date(2021, 4, 30), date(2021, 4, 30)),
        label_end_dates=(date(2021, 7, 30), date(2021, 7, 30)),
        symbols=("000001", "000002"),
        feature_names=feature_names,
        values=values,
        availability_mask=np.ones_like(values, dtype=bool),
        forward_returns=np.asarray([0.01, -0.01]),
        excess_returns=np.asarray([0.01, -0.01]),
    )
    relative = IndustryRelativeDataset(
        dataset=dataset,
        peer_context=np.asarray([True, True]),
        coverage_by_date=(
            {
                "feature_date": "2021-04-30",
                "eligible_for_industry_evaluation": True,
            },
        ),
    )

    factors, mask = industry_relative_factors(relative)

    assert factors.shape == (rows, len(INDUSTRY_FACTOR_NAMES))
    assert mask.all()
    assert np.allclose(factors[:, 0], 1.0)
    assert np.allclose(factors[:, 1], 0.6)  # CAPEX intensity is a negative input.
    assert np.allclose(factors[:, 2:], 1.0)
