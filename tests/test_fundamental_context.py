from datetime import date

import numpy as np
import pytest

from src.fundamental_embedding.api import FundamentalPricingSnapshot
from src.strategy.api import StrategyMarketData
from src.strategy.fundamental_context import (
    FundamentalStrategyMarketData,
    attach_current_fundamental_snapshot,
)


def _market(symbols=("000002", "000001")) -> StrategyMarketData:
    return StrategyMarketData(
        indicator_matrix=np.zeros((3, len(symbols), 1), dtype=np.float32),
        dates=["2026-08-17", "2026-08-18", "2026-08-19"],
        symbols=list(symbols),
        prices=np.ones((3, len(symbols)), dtype=np.float32),
    )


def _snapshot() -> FundamentalPricingSnapshot:
    return FundamentalPricingSnapshot(
        feature_date=date(2026, 8, 18),
        symbols=("000001", "000002"),
        feature_names=("valuation:beta", "valuation:market_implied_growth_5y"),
        values=np.asarray([[0.8, 0.1], [1.2, 0.2]], dtype=float),
        availability_mask=np.asarray([[True, True], [True, False]], dtype=bool),
        metadata={
            "contract": "current-valuation-industry-features-1",
            "historical_walk_forward_eligible": False,
        },
    )


def test_current_snapshot_is_visible_only_on_or_after_its_feature_date():
    panel = attach_current_fundamental_snapshot(_market(), _snapshot())

    assert isinstance(panel, FundamentalStrategyMarketData)
    assert not panel.fundamental_availability_mask[0].any()
    assert panel.fundamental_availability_mask[1, 0].tolist() == [True, False]
    assert panel.fundamental_availability_mask[1, 1].tolist() == [True, True]
    assert panel.fundamental_as_of_dates[1, 1] == date(2026, 8, 18)
    assert panel.fundamental_features[1, 1].tolist() == [0.8, 0.1]
    with pytest.raises(ValueError, match="cannot enter walk-forward search"):
        panel.require_historical_walk_forward_eligibility()


def test_fundamental_context_rejects_a_future_source_date():
    panel = attach_current_fundamental_snapshot(_market(), _snapshot())
    panel.fundamental_availability_mask[0, 0, 0] = True
    panel.fundamental_as_of_dates[0, 0] = date(2026, 8, 18)

    with pytest.raises(ValueError, match="future data"):
        panel.validate_fundamental_panel()
