"""Point-in-time industry-relative dataset construction; no model scoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .api import FundamentalPricingDataset, FundamentalPricingSnapshot
from .industry import IndustryRelativeSnapshotBuilder
from .industry_history import IndustryClassificationHistoryStore


@dataclass(frozen=True)
class IndustryRidgeConfig:
    maximum_label_age_days: int = 366
    minimum_industry_peers: int = 5
    minimum_date_coverage: float = 0.70


@dataclass(frozen=True)
class IndustryRelativeDataset:
    dataset: FundamentalPricingDataset
    peer_context: np.ndarray
    coverage_by_date: tuple[dict[str, Any], ...]

    def validate(self) -> IndustryRelativeDataset:
        self.dataset.validate()
        if self.peer_context.shape != (len(self.dataset.symbols),):
            raise ValueError("industry peer context has an invalid shape")
        return self


def build_industry_relative_dataset(
    dataset: FundamentalPricingDataset,
    history: IndustryClassificationHistoryStore,
    config: IndustryRidgeConfig | None = None,
) -> IndustryRelativeDataset:
    """Append industry-relative copies of raw features with dated coverage."""

    dataset.validate()
    settings = config or IndustryRidgeConfig()
    dates = np.asarray(dataset.feature_dates, dtype=object)
    relative_values = np.zeros_like(dataset.values, dtype=np.float64)
    relative_mask = np.zeros_like(dataset.availability_mask, dtype=bool)
    peer_context = np.zeros(len(dataset.symbols), dtype=bool)
    coverage: list[dict[str, Any]] = []
    ranker = IndustryRelativeSnapshotBuilder(
        minimum_industry_peers=settings.minimum_industry_peers
    )
    for feature_date in sorted(set(dataset.feature_dates)):
        selected = np.flatnonzero(dates == feature_date)
        symbols = tuple(dataset.symbols[index] for index in selected)
        labels = history.labels_as_of(feature_date, symbols)
        fresh = {
            symbol: label
            for symbol, label in labels.items()
            if (feature_date - label.effective_from).days
            <= settings.maximum_label_age_days
        }
        base = FundamentalPricingSnapshot(
            feature_date=feature_date,
            symbols=symbols,
            feature_names=dataset.feature_names,
            values=dataset.values[selected].copy(),
            availability_mask=dataset.availability_mask[selected].copy(),
        ).validate()
        industry = ranker.build(base, fresh)
        contextual = np.asarray(
            [scope is not None for scope in industry.peer_scopes], dtype=bool
        )
        relative_values[selected] = industry.values
        relative_mask[selected] = industry.availability_mask
        peer_context[selected] = contextual
        coverage.append(
            {
                "feature_date": feature_date.isoformat(),
                "row_count": len(selected),
                "published_label_count": len(labels),
                "fresh_label_count": len(fresh),
                "peer_context_count": int(contextual.sum()),
                "peer_context_coverage": float(contextual.mean())
                if len(contextual)
                else 0.0,
                "maximum_label_age_days": settings.maximum_label_age_days,
                "eligible_for_industry_evaluation": bool(
                    len(contextual)
                    and contextual.mean() >= settings.minimum_date_coverage
                ),
            }
        )
    augmented = FundamentalPricingDataset(
        feature_dates=dataset.feature_dates,
        label_end_dates=dataset.label_end_dates,
        symbols=dataset.symbols,
        feature_names=tuple(dataset.feature_names)
        + tuple(f"industry_relative:{name}" for name in dataset.feature_names),
        values=np.concatenate([dataset.values, relative_values], axis=1),
        availability_mask=np.concatenate(
            [dataset.availability_mask, relative_mask], axis=1
        ),
        forward_returns=dataset.forward_returns,
        excess_returns=dataset.excess_returns,
        metadata={
            **dataset.metadata,
            "contract": "industry-relative-fundamental-dataset-1",
            "industry_history_contract": "industry-classification-history-1",
            "industry_label_rule": "published_at <= feature_date",
            "current_taxonomy_not_used": True,
            "maximum_label_age_days": settings.maximum_label_age_days,
        },
    ).validate()
    return IndustryRelativeDataset(
        dataset=augmented,
        peer_context=peer_context,
        coverage_by_date=tuple(coverage),
    ).validate()
