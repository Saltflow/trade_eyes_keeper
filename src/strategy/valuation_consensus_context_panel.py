"""Bridge quality-gated reverse-DCF consensus into strategy rows.

The consensus builder has a distinct upstream contract from the historical
valuation context builder.  This adapter deliberately reuses the existing
fundamental panel join and scorer contract after validating that distinction;
no scorer or strategy-specific valuation logic is duplicated here.
"""

from __future__ import annotations

from dataclasses import replace

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.valuation_consensus_context import (
    VALUATION_CONSENSUS_CONTEXT_CONTRACT,
)

from .api import StrategyMarketData
from .fundamental_context import FundamentalStrategyMarketData
from .fundamental_panel import attach_historical_fundamental_dataset
from .valuation_context_panel import VALUATION_CONTEXT_PANEL_CONTRACT

VALUATION_CONSENSUS_CONTEXT_PANEL_CONTRACT = (
    "historical-valuation-consensus-context-panel-1"
)


def attach_historical_valuation_consensus_context(
    market_data: StrategyMarketData,
    dataset: FundamentalPricingDataset,
) -> FundamentalStrategyMarketData:
    """Attach a validated consensus context with a causal daily as-of join.

    The returned panel intentionally exposes the existing valuation-context
    feature contract so :class:`ValuationContextScorer` can consume the five
    consensus features without changing its scoring semantics.  The panel
    metadata remains available on the dataset boundary for audit tooling.
    """

    dataset.validate()
    if dataset.metadata.get("contract") != VALUATION_CONSENSUS_CONTEXT_CONTRACT:
        raise ValueError("dataset is not a historical valuation consensus context")
    bridged = replace(
        dataset,
        metadata={
            **dataset.metadata,
            "upstream_contract": VALUATION_CONSENSUS_CONTEXT_CONTRACT,
            "contract": "quarterly-fundamental-pricing-data-valuation-consensus-context-1",
        },
    )
    return attach_historical_fundamental_dataset(
        market_data,
        bridged,
        feature_contract=VALUATION_CONTEXT_PANEL_CONTRACT,
    )
