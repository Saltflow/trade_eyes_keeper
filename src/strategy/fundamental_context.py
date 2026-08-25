"""Point-in-time fundamental panels compatible with strategy market input.

The technical strategies intentionally do not consume this module.  It is the
single bridge for a future fundamental strategy, and makes current-only
research snapshots harmless: their values are unavailable before their feature
date and they are explicitly rejected for historical walk-forward search.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np

from src.fundamental_embedding.api import FundamentalPricingSnapshot

from .api import StrategyMarketData


def _date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@dataclass
class FundamentalStrategyMarketData(StrategyMarketData):
    """A strategy input whose fundamental matrix has names and provenance."""

    fundamental_feature_names: tuple[str, ...] = ()
    fundamental_feature_contract: str = ""
    fundamental_as_of_dates: np.ndarray | None = None
    fundamental_historical_walk_forward_eligible: bool = False

    def validate_fundamental_panel(self) -> "FundamentalStrategyMarketData":
        if self.fundamental_features is None or self.fundamental_availability_mask is None:
            raise ValueError("fundamental feature matrix and mask are both required")
        values = np.asarray(self.fundamental_features)
        available = np.asarray(self.fundamental_availability_mask)
        expected = (
            len(self.dates),
            len(self.symbols),
            len(self.fundamental_feature_names),
        )
        if values.shape != expected or available.shape != expected:
            raise ValueError("fundamental panel shape must match dates, symbols and names")
        if available.dtype != bool:
            raise ValueError("fundamental availability mask must be boolean")
        if not self.fundamental_feature_contract:
            raise ValueError("fundamental feature contract is required")
        if self.fundamental_as_of_dates is None:
            raise ValueError("fundamental source dates are required")
        as_of = np.asarray(self.fundamental_as_of_dates, dtype=object)
        if as_of.shape != expected[:2]:
            raise ValueError("fundamental source dates must match dates and symbols")
        if not np.all(np.isfinite(values[available])):
            raise ValueError("available fundamental features must be finite")
        market_dates = np.asarray([_date(item) for item in self.dates], dtype=object)
        for row, column in np.argwhere(available.any(axis=2)):
            source_date = as_of[row, column]
            if source_date is None:
                raise ValueError("available fundamental row is missing source date")
            if _date(source_date) > market_dates[row]:
                raise ValueError("fundamental panel contains future data")
        return self

    def require_historical_walk_forward_eligibility(self) -> None:
        """Fail closed before a solver can use a current-only snapshot."""

        if not self.fundamental_historical_walk_forward_eligible:
            raise ValueError(
                "fundamental panel lacks dated historical taxonomy or snapshots; "
                "it cannot enter walk-forward search"
            )


def attach_current_fundamental_snapshot(
    market_data: StrategyMarketData,
    snapshot: FundamentalPricingSnapshot,
    *,
    feature_contract: str | None = None,
) -> FundamentalStrategyMarketData:
    """Attach one current snapshot without broadcasting it into the past.

    Values become visible on ``snapshot.feature_date`` and later only.  The
    resulting panel is suitable for a live/research score on that date but is
    deliberately ineligible for historical candidate search.
    """

    snapshot.validate()
    dates = tuple(_date(item) for item in market_data.dates)
    symbols = tuple(str(item) for item in market_data.symbols)
    if len(set(symbols)) != len(symbols):
        raise ValueError("strategy market data contains duplicate symbols")
    if len(set(dates)) != len(dates):
        raise ValueError("strategy market data contains duplicate dates")
    if not dates:
        raise ValueError("strategy market data needs dates for fundamental panel")
    rows, columns, features = len(dates), len(symbols), len(snapshot.feature_names)
    values = np.zeros((rows, columns, features), dtype=np.float64)
    available = np.zeros_like(values, dtype=bool)
    as_of = np.empty((rows, columns), dtype=object)
    as_of[:, :] = None
    source_rows = {symbol: index for index, symbol in enumerate(snapshot.symbols)}
    for day_index, market_date in enumerate(dates):
        if market_date < snapshot.feature_date:
            continue
        for symbol_index, symbol in enumerate(symbols):
            source_index = source_rows.get(symbol)
            if source_index is None:
                continue
            source_mask = snapshot.availability_mask[source_index]
            values[day_index, symbol_index, source_mask] = snapshot.values[
                source_index, source_mask
            ]
            available[day_index, symbol_index] = source_mask
            if source_mask.any():
                as_of[day_index, symbol_index] = snapshot.feature_date
    return FundamentalStrategyMarketData(
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
        fundamental_feature_names=tuple(snapshot.feature_names),
        fundamental_feature_contract=(
            feature_contract
            or str(snapshot.metadata.get("contract") or "fundamental-snapshot/1")
        ),
        fundamental_as_of_dates=as_of,
        fundamental_historical_walk_forward_eligible=bool(
            snapshot.metadata.get("historical_walk_forward_eligible", False)
        ),
    ).validate_fundamental_panel()
