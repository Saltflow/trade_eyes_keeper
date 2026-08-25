from datetime import date

import numpy as np
import pytest

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.strategy.api import StrategyMarketData
from src.strategy.fundamental_panel import (
    HISTORICAL_FUNDAMENTAL_PANEL_CONTRACT,
    attach_historical_fundamental_dataset,
)


def _market_data() -> StrategyMarketData:
    return StrategyMarketData(
        indicator_matrix=np.ones((4, 2, 2), dtype=np.float32),
        dates=["2021-01-01", "2021-03-31", "2021-04-01", "2021-06-30"],
        symbols=["AAA", "BBB"],
        prices=np.ones((4, 2), dtype=np.float64),
    )


def _dataset(*, contract: str = "quarterly-fundamental-pricing-data-1"):
    values = np.asarray([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]])
    return FundamentalPricingDataset(
        feature_dates=(
            date(2021, 3, 31),
            date(2021, 3, 31),
            date(2021, 6, 30),
        ),
        label_end_dates=(
            date(2021, 6, 30),
            date(2021, 6, 30),
            date(2021, 9, 30),
        ),
        symbols=("AAA", "BBB", "AAA"),
        feature_names=("earnings_yield", "roe_ttm"),
        values=values,
        availability_mask=np.ones_like(values, dtype=bool),
        forward_returns=np.asarray([0.1, 0.2, 0.3]),
        excess_returns=np.asarray([0.0, 0.1, 0.2]),
        metadata={"contract": contract},
    )


def test_quarterly_values_are_not_visible_before_feature_date():
    panel = attach_historical_fundamental_dataset(_market_data(), _dataset())

    assert panel.fundamental_feature_contract == HISTORICAL_FUNDAMENTAL_PANEL_CONTRACT
    assert not panel.fundamental_availability_mask[0].any()
    np.testing.assert_allclose(panel.fundamental_features[1, 0], [1.0, 10.0])
    np.testing.assert_allclose(panel.fundamental_features[2, 0], [1.0, 10.0])
    np.testing.assert_allclose(panel.fundamental_features[3, 0], [3.0, 30.0])
    assert panel.fundamental_as_of_dates[1, 0] == date(2021, 3, 31)
    assert panel.fundamental_as_of_dates[3, 0] == date(2021, 6, 30)
    assert panel.fundamental_as_of_dates[3, 1] == date(2021, 3, 31)


def test_uncontracted_dataset_is_rejected():
    with pytest.raises(ValueError, match="quarterly point-in-time contract"):
        attach_historical_fundamental_dataset(
            _market_data(), _dataset(contract="current-snapshot")
        )


def test_future_market_date_order_is_rejected():
    market = _market_data()
    market.dates = ["2021-03-31", "2021-01-01", "2021-04-01", "2021-06-30"]
    with pytest.raises(ValueError, match="chronological"):
        attach_historical_fundamental_dataset(market, _dataset())
