from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.industry_evaluation import IndustryRelativeDataset
from src.fundamental_embedding.valuation_industry_evaluation import (
    ValuationIndustryEvaluationConfig,
    evaluate_valuation_industry_increment,
)


def test_nested_valuation_industry_evaluation_keeps_model_slices_separate():
    rng = np.random.default_rng(7)
    dates = tuple(date(2022, 1, 1) + timedelta(days=90 * i) for i in range(3))
    feature_dates = tuple(item for item in dates for _ in range(12))
    labels = tuple(item + timedelta(days=30) for item in feature_dates)
    symbols = tuple(f"S{i:02d}" for i in range(len(feature_dates)))
    values = rng.normal(size=(len(symbols), 48))
    mask = np.ones_like(values, dtype=bool)
    dataset = FundamentalPricingDataset(
        feature_dates=feature_dates,
        label_end_dates=labels,
        symbols=symbols,
        feature_names=tuple(f"f{i}" for i in range(48)),
        values=values,
        availability_mask=mask,
        forward_returns=rng.normal(size=len(symbols)),
        excess_returns=rng.normal(size=len(symbols)),
    ).validate()
    relative = IndustryRelativeDataset(
        dataset=dataset,
        peer_context=np.ones(len(symbols), dtype=bool),
        coverage_by_date=tuple(
            {
                "feature_date": item.isoformat(),
                "eligible_for_industry_evaluation": True,
            }
            for item in dates
        ),
    )
    report = evaluate_valuation_industry_increment(
        relative,
        ValuationIndustryEvaluationConfig(
            minimum_train_dates=1,
            minimum_train_rows=1,
        ),
    )
    assert set(report["summaries"]) == {
        "base",
        "valuation",
        "industry",
        "valuation_industry",
    }
    assert len(report["quarterly_results"]) == 2
