"""Bridge historical valuation context into the strategy fundamental panel."""

from __future__ import annotations

from dataclasses import replace

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.valuation_context import (
    HISTORICAL_VALUATION_CONTEXT_CONTRACT,
)

from .api import StrategyMarketData
from .fundamental_context import FundamentalStrategyMarketData
from .fundamental_panel import attach_historical_fundamental_dataset

VALUATION_CONTEXT_PANEL_CONTRACT = "historical-valuation-context-panel-1"


def attach_historical_valuation_context(
    market_data: StrategyMarketData,
    dataset: FundamentalPricingDataset,
) -> FundamentalStrategyMarketData:
    """Attach only a validated historical valuation context to daily rows."""

    dataset.validate()
    if dataset.metadata.get("contract") != HISTORICAL_VALUATION_CONTEXT_CONTRACT:
        raise ValueError("dataset is not a historical valuation context")
    bridged = replace(
        dataset,
        metadata={
            **dataset.metadata,
            "upstream_contract": HISTORICAL_VALUATION_CONTEXT_CONTRACT,
            "contract": "quarterly-fundamental-pricing-data-valuation-context-1",
        },
    )
    return attach_historical_fundamental_dataset(
        market_data,
        bridged,
        feature_contract=VALUATION_CONTEXT_PANEL_CONTRACT,
    )
