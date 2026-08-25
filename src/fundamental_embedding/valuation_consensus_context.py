"""Quality-gated historical valuation context for quantitative consumers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np

from .api import FundamentalPricingDataset
from .industry_evaluation import IndustryRidgeConfig, build_industry_relative_dataset
from .industry_history import IndustryClassificationHistoryStore
from .valuation_context import (
    ValuationContextConfig,
    build_historical_valuation_context,
)

VALUATION_CONSENSUS_CONTEXT_CONTRACT = (
    "historical-valuation-consensus-industry-context-1"
)
VALUATION_CONSENSUS_SOURCE_COLUMNS = (
    "beta",
    "capm_cost_of_equity",
    "market_implied_growth_5y",
    "market_vs_fundamental_growth",
    "fundamental_growth",
)


@dataclass(frozen=True)
class ValuationConsensusQualityConfig:
    """Predeclared quality gate for market-implied growth diagnostics."""

    minimum_expert_count: int = 2
    maximum_dispersion: float = 0.10
    require_consensus: bool = True

    def validate(self) -> ValuationConsensusQualityConfig:
        if self.minimum_expert_count < 1:
            raise ValueError("minimum_expert_count must be positive")
        if self.maximum_dispersion < 0:
            raise ValueError("maximum_dispersion must be non-negative")
        return self


def _symbol(value: object) -> str:
    text = str(value).strip()
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _quality_ok(
    row: Mapping[str, Any], config: ValuationConsensusQualityConfig
) -> bool:
    if str(row.get("status", "")) != "solved":
        return False
    expert_count = _finite(row.get("market_implied_growth_expert_count"))
    dispersion = _finite(row.get("market_implied_growth_dispersion"))
    if expert_count is None or expert_count < config.minimum_expert_count:
        return False
    if dispersion is None or dispersion > config.maximum_dispersion:
        return False
    return not (config.require_consensus and str(row.get("market_implied_growth_status", "")) != "solved_consensus")


def attach_consensus_valuation_features(
    raw: FundamentalPricingDataset,
    rows: Sequence[Mapping[str, Any]],
    config: ValuationConsensusQualityConfig | None = None,
) -> FundamentalPricingDataset:
    """Append dated valuation fields and gate only their availability masks."""

    settings = (config or ValuationConsensusQualityConfig()).validate()
    raw.validate()
    records: dict[tuple[str, date], Mapping[str, Any]] = {}
    for row in rows:
        symbol = _symbol(row.get("symbol"))
        feature_date = row.get("feature_date")
        if not isinstance(feature_date, date):
            try:
                feature_date = date.fromisoformat(str(feature_date))
            except (TypeError, ValueError) as exc:
                raise ValueError("valuation row has invalid feature_date") from exc
        key = (symbol, feature_date)
        if key in records:
            raise ValueError(f"duplicate valuation row: {key}")
        records[key] = row

    values = np.zeros(
        (len(raw.symbols), len(VALUATION_CONSENSUS_SOURCE_COLUMNS)),
        dtype=np.float64,
    )
    mask = np.zeros_like(values, dtype=bool)
    matched = 0
    gated_rows = 0
    for index, (feature_date, symbol) in enumerate(
        zip(raw.feature_dates, raw.symbols)
    ):
        row = records.get((_symbol(symbol), feature_date))
        if row is None:
            continue
        matched += 1
        if not _quality_ok(row, settings):
            continue
        gated_rows += 1
        for column_index, name in enumerate(VALUATION_CONSENSUS_SOURCE_COLUMNS):
            value = _finite(row.get(name))
            if value is None:
                continue
            values[index, column_index] = value
            mask[index, column_index] = True
    return FundamentalPricingDataset(
        feature_dates=raw.feature_dates,
        label_end_dates=raw.label_end_dates,
        symbols=raw.symbols,
        feature_names=tuple(raw.feature_names)
        + tuple(f"valuation:{name}" for name in VALUATION_CONSENSUS_SOURCE_COLUMNS),
        values=np.concatenate([raw.values, values], axis=1),
        availability_mask=np.concatenate([raw.availability_mask, mask], axis=1),
        forward_returns=raw.forward_returns,
        excess_returns=raw.excess_returns,
        metadata={
            **raw.metadata,
            "contract": "historical-valuation-consensus-source-1",
            "valuation_quality_config": {
                "minimum_expert_count": settings.minimum_expert_count,
                "maximum_dispersion": settings.maximum_dispersion,
                "require_consensus": settings.require_consensus,
            },
            "matched_valuation_rows": matched,
            "gated_valuation_rows": gated_rows,
            "published_at_before_feature_date": True,
            "current_taxonomy_not_used": True,
        },
    ).validate()


def build_historical_consensus_context(
    raw: FundamentalPricingDataset,
    rows: Sequence[Mapping[str, Any]],
    industry_history: IndustryClassificationHistoryStore,
    *,
    quality: ValuationConsensusQualityConfig | None = None,
    industry: IndustryRidgeConfig | None = None,
    context: ValuationContextConfig | None = None,
) -> FundamentalPricingDataset:
    """Build a dated, industry-relative, quality-gated context dataset."""

    source = attach_consensus_valuation_features(raw, rows, quality)
    relative = build_industry_relative_dataset(
        source, industry_history, industry or IndustryRidgeConfig()
    )
    result = build_historical_valuation_context(
        relative, context or ValuationContextConfig()
    )
    return FundamentalPricingDataset(
        feature_dates=result.feature_dates,
        label_end_dates=result.label_end_dates,
        symbols=result.symbols,
        feature_names=result.feature_names,
        values=result.values,
        availability_mask=result.availability_mask,
        forward_returns=result.forward_returns,
        excess_returns=result.excess_returns,
        metadata={
            **result.metadata,
            "contract": VALUATION_CONSENSUS_CONTEXT_CONTRACT,
            "upstream_contract": result.metadata.get("contract"),
            "valuation_quality_config": source.metadata[
                "valuation_quality_config"
            ],
            "gated_valuation_rows": source.metadata["gated_valuation_rows"],
            "matched_valuation_rows": source.metadata["matched_valuation_rows"],
        },
    ).validate()
