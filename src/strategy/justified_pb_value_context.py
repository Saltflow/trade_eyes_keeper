# -*- coding: utf-8 -*-
"""Point-in-time valuation context for the justified-PB value strategy.

The strategy consumes a causally joined quarterly fundamental panel built from
the same PIT store that supplies raw/qfq market bundles.  The enricher is the
single bridge used by the shared backtest/search pipeline: it refuses
current-only snapshots and carries quarterly observations forward only after
their publication date.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from src.fundamental_embedding.dataset import QuarterlyPricingDatasetBuilder
from src.strategy.context_enrichment import (
    HistoricalDatasetEnricher,
    make_historical_dataset_enricher,
)

#: Feature names consumed by the strategy (percentage ROE and book yield).
REQUIRED_FEATURES: tuple[str, ...] = ("roe_ttm", "book_yield")

#: Contract identifier for readability/diagnostics.
CONTEXT_CONTRACT = "justified-pb-value-context-1"


def make_justified_pb_value_context(
    config: dict,
    *,
    market: str = "a_share",
    symbols: Iterable[str] = (),
) -> HistoricalDatasetEnricher:
    """Build the dated A-share valuation enricher for one stock pool.

    The dataset contains one row per company-quarter and is joinable to any
    daily ``StrategyMarketData`` through the historical adapter.  Missing or
    current-only source data fails closed with an explicit error; missing
    fundamentals for a single symbol simply remain unavailable (masked).
    """
    if str(market) != "a_share":
        raise ValueError("justified_pb_value is an A-share-only strategy")
    settings = config.get("point_in_time_data", {}) or {}
    root = str(settings.get("output_dir", "data/point_in_time"))
    if not (Path(root) / "market").is_dir():
        raise ValueError(
            "point-in-time market store is missing: %s/market" % root
        )
    if not (Path(root) / "fundamentals").is_dir():
        raise ValueError(
            "point-in-time fundamental store is missing: %s/fundamentals" % root
        )

    selected = tuple(
        sorted({str(code) for code in symbols if str(code).strip()})
    ) or None
    builder = QuarterlyPricingDatasetBuilder(root, market="a_share")
    dataset = builder.build(symbols=selected)
    if len(dataset.symbols) == 0:
        raise ValueError(
            "no A-share point-in-time fundamentals for the requested pool"
        )
    return make_historical_dataset_enricher(
        dataset, required_feature_names=REQUIRED_FEATURES
    )