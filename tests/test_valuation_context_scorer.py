from datetime import date

import numpy as np

from src.strategy.fundamental_context import FundamentalStrategyMarketData
from src.strategy.valuation_context_panel import VALUATION_CONTEXT_PANEL_CONTRACT
from src.strategy.valuation_context_scorer import ValuationContextScorer


def test_scorer_is_signed_and_quality_weighted():
    names = (
        "valuation:beta_cheap_rank",
        "valuation:capm_cost_cheap_rank",
        "valuation:implied_growth_cheap_rank",
        "valuation:growth_gap_cheap_rank",
        "valuation:fundamental_growth_rank",
        "valuation:observation_fraction",
        "valuation:growth_solved",
        "industry_relative:implied_growth_cheap_rank",
        "industry_relative:growth_gap_cheap_rank",
        "industry_relative:observation_fraction",
    )
    panel = FundamentalStrategyMarketData(
        indicator_matrix=np.ones((1, 2, 1)),
        dates=["2024-01-01"],
        symbols=["A", "B"],
        prices=np.ones((1, 2)),
        fundamental_features=np.asarray([[[-1, -1, 1, 1, 1, 1, 1, 1, 1, 1], [1, 1, -1, -1, -1, 0.25, 1, -1, -1, 0.25]]], dtype=float),
        fundamental_availability_mask=np.ones((1, 2, 10), dtype=bool),
        fundamental_feature_names=names,
        fundamental_feature_contract=VALUATION_CONTEXT_PANEL_CONTRACT,
        fundamental_as_of_dates=np.asarray([[date(2024, 1, 1), date(2024, 1, 1)]], dtype=object),
    )
    result = ValuationContextScorer().score(panel)
    assert result.usable_mask.tolist() == [True, True]
    assert result.scores[0] > result.scores[1]
    assert result.observation_fraction.tolist() == [1.0, 0.25]
