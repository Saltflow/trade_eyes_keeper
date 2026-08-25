from datetime import date, timedelta

import numpy as np
import pytest

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.strategy.api import StrategyMarketData
from src.strategy.valuation_context_panel import (
    VALUATION_CONTEXT_PANEL_CONTRACT,
    attach_historical_valuation_context,
)


def _dataset():
    feature_dates = (date(2024, 1, 1), date(2024, 4, 1))
    names = ("valuation:observation_fraction",)
    return FundamentalPricingDataset(
        feature_dates=feature_dates,
        label_end_dates=tuple(item + timedelta(days=30) for item in feature_dates),
        symbols=("000001", "000001"),
        feature_names=names,
        values=np.asarray([[1.0], [0.5]]),
        availability_mask=np.asarray([[True], [True]]),
        forward_returns=np.asarray([0.0, 0.0]),
        excess_returns=np.asarray([0.0, 0.0]),
        metadata={"contract": "historical-valuation-industry-context-1"},
    )


def _market():
    return StrategyMarketData(
        indicator_matrix=np.ones((3, 1, 1)),
        dates=["2024-01-01", "2024-02-01", "2024-04-02"],
        symbols=["000001"],
        prices=np.ones((3, 1)),
    )


def test_historical_valuation_context_uses_as_of_join():
    panel = attach_historical_valuation_context(_market(), _dataset())
    assert panel.fundamental_feature_contract == VALUATION_CONTEXT_PANEL_CONTRACT
    np.testing.assert_allclose(panel.fundamental_features[:, 0, 0], [1.0, 1.0, 0.5])
    assert panel.fundamental_as_of_dates[2, 0] == date(2024, 4, 1)
    assert panel.fundamental_historical_walk_forward_eligible is True


def test_context_adapter_rejects_current_contract():
    dataset = _dataset()
    dataset = type(dataset)(
        **{**dataset.__dict__, "metadata": {"contract": "current-valuation-industry-features-1"}}
    )
    with pytest.raises(ValueError, match="historical valuation context"):
        attach_historical_valuation_context(_market(), dataset)
