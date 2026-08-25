"""Causal bridge from quarterly fundamentals to strategy market rows.

The fundamental embedding builders operate on one observation per company and
quarter, while a strategy consumes daily market rows.  This module performs
the only allowed temporal join: a quarterly observation becomes visible on
its feature date and is carried forward until the next available observation.
It never fills a date from a future quarterly row and it records the source
date for every available company row.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingDataset

from .api import StrategyMarketData
from .fundamental_context import FundamentalStrategyMarketData

HISTORICAL_FUNDAMENTAL_PANEL_CONTRACT = "historical-fundamental-panel-1"


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _ordered_unique(values: Iterable[object]) -> tuple[date, ...]:
    result = tuple(_as_date(value) for value in values)
    if any(left > right for left, right in zip(result, result[1:])):
        raise ValueError("strategy market dates must be chronological")
    if len(set(result)) != len(result):
        raise ValueError("strategy market dates must be unique")
    return result


def attach_historical_fundamental_dataset(
    market_data: StrategyMarketData,
    dataset: FundamentalPricingDataset,
    *,
    feature_contract: str | None = None,
) -> FundamentalStrategyMarketData:
    """Join a causal quarterly dataset to daily strategy rows.

    ``FundamentalPricingDataset.feature_dates`` are the first dates on which
    the corresponding as-of snapshot may be used.  The function therefore
    selects the latest row with ``feature_date <= market_date`` independently
    for every symbol.  It rejects duplicate company-quarter rows and refuses
    to mark a current-only or otherwise uncontracted dataset as eligible for
    walk-forward search.
    """

    dataset.validate()
    if not str(dataset.metadata.get("contract", "")).startswith(
        "quarterly-fundamental-pricing-data-"
    ):
        raise ValueError("dataset lacks the quarterly point-in-time contract")
    dates = _ordered_unique(market_data.dates)
    symbols = tuple(str(item) for item in market_data.symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError("strategy market data contains duplicate symbols")
    feature_names = tuple(dataset.feature_names)
    feature_dates = tuple(_as_date(item) for item in dataset.feature_dates)
    rows: dict[tuple[date, str], int] = {}
    for index, (feature_date, symbol) in enumerate(
        zip(feature_dates, dataset.symbols)
    ):
        key = (feature_date, str(symbol))
        if key in rows:
            raise ValueError(f"duplicate fundamental row: {feature_date}/{symbol}")
        rows[key] = index

    values = np.zeros(
        (len(dates), len(symbols), len(feature_names)), dtype=np.float64
    )
    available = np.zeros_like(values, dtype=bool)
    source_dates = np.empty((len(dates), len(symbols)), dtype=object)
    source_dates[:, :] = None
    per_symbol = {
        symbol: sorted(
            (feature_date, index)
            for (feature_date, row_symbol), index in rows.items()
            if row_symbol == symbol
        )
        for symbol in symbols
    }
    for symbol_index, symbol in enumerate(symbols):
        history = per_symbol[symbol]
        cursor = 0
        selected_index: int | None = None
        selected_date: date | None = None
        for day_index, market_date in enumerate(dates):
            while cursor < len(history) and history[cursor][0] <= market_date:
                selected_date, selected_index = history[cursor]
                cursor += 1
            if selected_index is None or selected_date is None:
                continue
            values[day_index, symbol_index] = dataset.values[selected_index]
            available[day_index, symbol_index] = dataset.availability_mask[
                selected_index
            ]
            source_dates[day_index, symbol_index] = selected_date

    panel = FundamentalStrategyMarketData(
        indicator_matrix=market_data.indicator_matrix,
        dates=list(market_data.dates),
        symbols=list(market_data.symbols),
        prices=market_data.prices,
        highs=market_data.highs,
        lows=market_data.lows,
        tradable=market_data.tradable,
        date_ordinals=market_data.date_ordinals,
        market=market_data.market,
        observation_counts=market_data.observation_counts,
        fundamental_features=values,
        fundamental_availability_mask=available,
        fundamental_feature_names=feature_names,
        fundamental_feature_contract=(
            feature_contract or HISTORICAL_FUNDAMENTAL_PANEL_CONTRACT
        ),
        fundamental_as_of_dates=source_dates,
        fundamental_historical_walk_forward_eligible=True,
    )
    return panel.validate_fundamental_panel()
