from datetime import date

import numpy as np

from src.fundamental_embedding.api import (
    PRICING_FEATURE_NAMES,
    FundamentalPricingDataset,
)
from src.fundamental_embedding.industry_blend_evaluation import (
    IndustryBlendConfig,
    evaluate_industry_rank_blends,
)
from src.fundamental_embedding.industry_evaluation import IndustryRelativeDataset


def test_fixed_weights_are_reported_without_test_period_selection():
    feature_names = PRICING_FEATURE_NAMES + tuple(
        f"industry_relative:{name}" for name in PRICING_FEATURE_NAMES
    )
    values = np.arange(6 * len(feature_names), dtype=float).reshape(
        6, len(feature_names)
    )
    dataset = FundamentalPricingDataset(
        feature_dates=(
            date(2021, 1, 1),
            date(2021, 1, 1),
            date(2021, 1, 1),
            date(2021, 4, 1),
            date(2021, 4, 1),
            date(2021, 4, 1),
        ),
        label_end_dates=(
            date(2021, 2, 1),
            date(2021, 2, 1),
            date(2021, 2, 1),
            date(2021, 5, 1),
            date(2021, 5, 1),
            date(2021, 5, 1),
        ),
        symbols=("000001", "000002", "000003", "000001", "000002", "000003"),
        feature_names=feature_names,
        values=values,
        availability_mask=np.ones_like(values, dtype=bool),
        forward_returns=np.asarray([0.01, -0.01, 0.02, 0.02, -0.02, 0.03]),
        excess_returns=np.asarray([0.01, -0.01, 0.02, 0.02, -0.02, 0.03]),
    )
    relative = IndustryRelativeDataset(
        dataset=dataset,
        peer_context=np.ones(6, dtype=bool),
        coverage_by_date=(
            {
                "feature_date": "2021-01-01",
                "eligible_for_industry_evaluation": True,
            },
            {
                "feature_date": "2021-04-01",
                "eligible_for_industry_evaluation": True,
            },
        ),
    )

    report = evaluate_industry_rank_blends(
        relative,
        IndustryBlendConfig(
            weights=(0.0, 0.5), minimum_train_dates=1, minimum_train_rows=3
        ),
    )

    assert set(report["summaries"]) == {"0", "0.5"}
    assert len(report["quarterly_results"]) == 1
    assert report["summaries"]["0"]["rank_ic_quarters"] == 1
    assert report["acceptance"]["weights_fixed_before_test"] is True
