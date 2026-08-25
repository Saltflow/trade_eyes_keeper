"""Opt-in, causal enrichers for strategy market data.

Search and backtest code should not know how a fundamental dataset is joined.
This module provides a small picklable callable that owns the join adapter,
quality contract, and content fingerprint.  Legacy technical strategies do
not install an enricher and therefore retain their exact input path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingDataset

from .api import StrategyMarketData
from .fundamental_context import FundamentalStrategyMarketData
from .fundamental_panel import attach_historical_fundamental_dataset
from .valuation_consensus_context_panel import (
    attach_historical_valuation_consensus_context,
)

PanelAdapter = Callable[
    [StrategyMarketData, FundamentalPricingDataset], FundamentalStrategyMarketData
]


def _generic_historical_adapter(
    market_data: StrategyMarketData, dataset: FundamentalPricingDataset
) -> FundamentalStrategyMarketData:
    return attach_historical_fundamental_dataset(market_data, dataset)


def _consensus_historical_adapter(
    market_data: StrategyMarketData, dataset: FundamentalPricingDataset
) -> FundamentalStrategyMarketData:
    return attach_historical_valuation_consensus_context(market_data, dataset)


def _dataset_hash(dataset: FundamentalPricingDataset) -> str:
    """Fingerprint values, masks, row order, and the point-in-time contract."""

    dataset.validate()
    digest = hashlib.sha256()
    for values in (
        np.asarray(dataset.values),
        np.asarray(dataset.availability_mask),
        np.asarray(dataset.forward_returns),
        np.asarray(dataset.excess_returns),
    ):
        contiguous = np.ascontiguousarray(values)
        digest.update(str(contiguous.shape).encode("ascii"))
        digest.update(str(contiguous.dtype).encode("ascii"))
        digest.update(contiguous.view(np.uint8))
    digest.update(
        json.dumps(
            {
                "feature_dates": [str(value) for value in dataset.feature_dates],
                "label_end_dates": [str(value) for value in dataset.label_end_dates],
                "symbols": list(dataset.symbols),
                "feature_names": list(dataset.feature_names),
                "metadata": dataset.metadata,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    )
    return digest.hexdigest()


@dataclass(frozen=True)
class HistoricalDatasetEnricher:
    """Attach a dated dataset to any ``StrategyMarketData`` instance.

    ``adapter`` is a module-level function so the object can safely cross the
    process-evaluation boundary.  Required names are checked before a strategy
    sees the panel, which turns a missing/partial context into a clear error
    instead of silently changing the score.
    """

    dataset: FundamentalPricingDataset
    adapter: PanelAdapter
    required_feature_names: tuple[str, ...] = ()
    name: str = "historical-fundamental-dataset"

    def __post_init__(self) -> None:
        self.dataset.validate()
        if not callable(self.adapter):
            raise TypeError("context adapter must be callable")

    @property
    def contract(self) -> str:
        return str(self.dataset.metadata.get("contract", ""))

    @property
    def contract_hash(self) -> str:
        payload = {
            "dataset_hash": _dataset_hash(self.dataset),
            "adapter": (
                self.adapter.__module__,
                self.adapter.__qualname__,
            ),
            "name": self.name,
            "required_feature_names": self.required_feature_names,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def __call__(self, market_data: StrategyMarketData) -> FundamentalStrategyMarketData:
        panel = self.adapter(market_data, self.dataset)
        panel.validate_fundamental_panel()
        panel.require_historical_walk_forward_eligibility()
        names = set(panel.fundamental_feature_names)
        missing = [name for name in self.required_feature_names if name not in names]
        if missing:
            raise ValueError(
                "historical context is missing required features: "
                + ", ".join(missing)
            )
        return panel


def make_historical_dataset_enricher(
    dataset: FundamentalPricingDataset,
    *,
    required_feature_names: tuple[str, ...] = (),
) -> HistoricalDatasetEnricher:
    """Build a generic causal enricher for any quarterly dataset contract."""

    return HistoricalDatasetEnricher(
        dataset=dataset,
        adapter=_generic_historical_adapter,
        required_feature_names=tuple(required_feature_names),
    )


def make_consensus_context_enricher(
    dataset: FundamentalPricingDataset,
    *,
    required_feature_names: tuple[str, ...] = (),
) -> HistoricalDatasetEnricher:
    """Build the strict reverse-DCF consensus context enricher."""

    return HistoricalDatasetEnricher(
        dataset=dataset,
        adapter=_consensus_historical_adapter,
        required_feature_names=tuple(required_feature_names),
        name="historical-valuation-consensus-context",
    )
