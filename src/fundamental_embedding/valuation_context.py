"""Causal, low-dimensional valuation context for quantitative consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from scipy.stats import rankdata

from .api import FundamentalPricingDataset
from .industry_evaluation import IndustryRelativeDataset

HISTORICAL_VALUATION_CONTEXT_CONTRACT = "historical-valuation-industry-context-1"
VALUATION_CONTEXT_FEATURE_NAMES = (
    "valuation:beta_cheap_rank",
    "valuation:capm_cost_cheap_rank",
    "valuation:implied_growth_cheap_rank",
    "valuation:growth_gap_cheap_rank",
    "valuation:fundamental_growth_rank",
    "valuation:observation_fraction",
    "valuation:growth_solved",
    "industry_relative:implied_growth_cheap_rank",
    "industry_relative:growth_gap_cheap_rank",
    "industry_relative:observation_fraction",
)
_SOURCE_COLUMNS = (
    "valuation:beta",
    "valuation:capm_cost_of_equity",
    "valuation:market_implied_growth_5y",
    "valuation:market_vs_fundamental_growth",
    "valuation:fundamental_growth",
)


@dataclass(frozen=True)
class ValuationContextConfig:
    """Fixed semantics for the context pack; no test-period tuning."""

    minimum_cross_section: int = 3

    def validate(self) -> ValuationContextConfig:
        if self.minimum_cross_section < 2:
            raise ValueError("minimum_cross_section must be at least two")
        return self


def _centered_rank(values: np.ndarray) -> np.ndarray:
    count = len(values)
    if count < 2:
        return np.zeros(count, dtype=np.float64)
    ranks = rankdata(values, method="average")
    return (ranks - (count + 1.0) / 2.0) / ((count - 1.0) / 2.0)


def _date_rank(
    dates: tuple[date, ...], values: np.ndarray, mask: np.ndarray,
    minimum_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    result = np.zeros(len(values), dtype=np.float64)
    output_mask = np.zeros(len(values), dtype=bool)
    date_array = np.asarray(dates, dtype=object)
    for current_date in sorted(set(dates)):
        selected = date_array == current_date
        observed = selected & mask & np.isfinite(values)
        positions = np.flatnonzero(observed)
        if len(positions) < minimum_count:
            continue
        result[positions] = _centered_rank(values[positions])
        output_mask[positions] = True
    return result, output_mask


def build_historical_valuation_context(
    relative: IndustryRelativeDataset,
    config: ValuationContextConfig | None = None,
) -> FundamentalPricingDataset:
    """Build a rank-and-quality feature pack from dated valuation inputs."""

    settings = (config or ValuationContextConfig()).validate()
    relative.validate()
    source = relative.dataset
    index = {name: position for position, name in enumerate(source.feature_names)}
    industry_sources = tuple(f"industry_relative:{name}" for name in _SOURCE_COLUMNS)
    missing = [name for name in (*_SOURCE_COLUMNS, *industry_sources) if name not in index]
    if missing:
        raise ValueError("valuation context source fields are missing: " + ", ".join(missing))
    base_end = min(index[name] for name in _SOURCE_COLUMNS)
    values = np.zeros((len(source.symbols), len(VALUATION_CONTEXT_FEATURE_NAMES)), dtype=np.float64)
    mask = np.zeros_like(values, dtype=bool)
    directions = (-1.0, -1.0, -1.0, -1.0, 1.0)
    for output_index, (name, direction) in enumerate(zip(_SOURCE_COLUMNS, directions)):
        ranked, ranked_mask = _date_rank(
            source.feature_dates,
            source.values[:, index[name]],
            source.availability_mask[:, index[name]],
            settings.minimum_cross_section,
        )
        values[:, output_index] = direction * ranked
        mask[:, output_index] = ranked_mask

    observed = source.availability_mask[:, [index[name] for name in _SOURCE_COLUMNS]]
    values[:, 5] = observed.mean(axis=1)
    mask[:, 5] = True
    values[:, 6] = (observed[:, 2] & observed[:, 3]).astype(np.float64)
    mask[:, 6] = True
    for output_index, name in zip((7, 8), industry_sources[2:4]):
        values[:, output_index] = -source.values[:, index[name]]
        mask[:, output_index] = source.availability_mask[:, index[name]]
    industry_observed = source.availability_mask[:, [index[name] for name in industry_sources]]
    values[:, 9] = industry_observed.mean(axis=1)
    mask[:, 9] = True

    metadata: dict[str, Any] = {
        **source.metadata,
        "contract": HISTORICAL_VALUATION_CONTEXT_CONTRACT,
        "source_feature_names": list(source.feature_names),
        "source_valuation_columns": list(_SOURCE_COLUMNS),
        "economic_direction": dict(zip(_SOURCE_COLUMNS, directions)),
        "missing_values_are_not_median_imputed": True,
        "quality_fields_always_observed": [
            "valuation:observation_fraction",
            "valuation:growth_solved",
            "industry_relative:observation_fraction",
        ],
        "published_at_before_feature_date": True,
        "current_taxonomy_not_used": True,
    }
    return FundamentalPricingDataset(
        feature_dates=source.feature_dates,
        label_end_dates=source.label_end_dates,
        symbols=source.symbols,
        feature_names=tuple(source.feature_names[:base_end]) + VALUATION_CONTEXT_FEATURE_NAMES,
        values=np.concatenate([source.values[:, :base_end], values], axis=1),
        availability_mask=np.concatenate([source.availability_mask[:, :base_end], mask], axis=1),
        forward_returns=source.forward_returns,
        excess_returns=source.excess_returns,
        metadata=metadata,
    ).validate()
