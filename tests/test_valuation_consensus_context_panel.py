from datetime import date, timedelta

import numpy as np
import pytest

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.valuation_consensus_context import (
    VALUATION_CONSENSUS_CONTEXT_CONTRACT,
)
from src.strategy.api import StrategyMarketData
from src.strategy.valuation_consensus_context_panel import (
    attach_historical_valuation_consensus_context,
)
from src.strategy.valuation_context_panel import VALUATION_CONTEXT_PANEL_CONTRACT
from src.strategy.valuation_context_scorer import ValuationContextScorer


def _dataset(contract=VALUATION_CONSENSUS_CONTEXT_CONTRACT):
    feature_dates = (date(2024, 1, 1), date(2024, 4, 1))
    names = (
        "valuation:consensus_fair_value_ratio",
        "valuation:consensus_implied_growth",
    )
    return FundamentalPricingDataset(
        feature_dates=feature_dates,
        label_end_dates=tuple(item + timedelta(days=30) for item in feature_dates),
        symbols=("000001", "000001"),
        feature_names=names,
        values=np.asarray([[1.0, 0.05], [0.5, 0.02]]),
        availability_mask=np.asarray([[True, True], [True, True]]),
        forward_returns=np.asarray([0.0, 0.0]),
        excess_returns=np.asarray([0.0, 0.0]),
        metadata={"contract": contract},
    )


def _market():
    return StrategyMarketData(
        indicator_matrix=np.ones((3, 1, 1)),
        dates=["2024-01-01", "2024-02-01", "2024-04-02"],
        symbols=["000001"],
        prices=np.ones((3, 1)),
    )


def test_consensus_context_uses_causal_as_of_join():
    panel = attach_historical_valuation_consensus_context(_market(), _dataset())
    assert panel.fundamental_feature_contract == VALUATION_CONTEXT_PANEL_CONTRACT
    np.testing.assert_allclose(
        panel.fundamental_features[:, 0, :],
        [[1.0, 0.05], [1.0, 0.05], [0.5, 0.02]],
    )
    assert panel.fundamental_as_of_dates[2, 0] == date(2024, 4, 1)
    assert panel.fundamental_historical_walk_forward_eligible is True


def test_consensus_context_rejects_unrelated_contract():
    with pytest.raises(ValueError, match="historical valuation consensus context"):
        attach_historical_valuation_consensus_context(
            _market(), _dataset("current-valuation-features-1")
        )


def test_consensus_context_panel_keeps_existing_scorer_contract():
    names = (*ValuationContextScorer._SIGNAL_NAMES, "valuation:observation_fraction")
    values = np.ones((2, len(names)), dtype=float)
    values[:, -1] = 0.81
    dataset = FundamentalPricingDataset(
        feature_dates=(date(2024, 1, 1), date(2024, 4, 1)),
        label_end_dates=(date(2024, 2, 1), date(2024, 5, 1)),
        symbols=("000001", "000001"),
        feature_names=names,
        values=values,
        availability_mask=np.ones_like(values, dtype=bool),
        forward_returns=np.zeros(2),
        excess_returns=np.zeros(2),
        metadata={"contract": VALUATION_CONSENSUS_CONTEXT_CONTRACT},
    )
    score = ValuationContextScorer().score(
        attach_historical_valuation_consensus_context(_market(), dataset)
    )
    assert score.usable_mask.tolist() == [True]
    np.testing.assert_allclose(score.scores, [0.9])
