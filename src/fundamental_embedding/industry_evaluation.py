"""Causal evaluation of industry-relative fundamental features.

This module evaluates whether dated official industry labels add information
over the same raw point-in-time fundamentals.  It deliberately does not use
the current Baostock snapshot and it never trains on labels whose forward
return has not fully realized.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from scipy.stats import spearmanr

from .api import FundamentalPricingDataset, FundamentalPricingSnapshot
from .exposure import RobustFeatureTransformer
from .industry import IndustryRelativeSnapshotBuilder
from .industry_history import IndustryClassificationHistoryStore


@dataclass(frozen=True)
class IndustryRidgeConfig:
    maximum_label_age_days: int = 366
    minimum_industry_peers: int = 5
    minimum_date_coverage: float = 0.70
    minimum_train_dates: int = 2
    minimum_train_rows: int = 100
    ridge_alpha: float = 8.0
    top_bottom_fraction: float = 0.20


@dataclass(frozen=True)
class IndustryRelativeDataset:
    dataset: FundamentalPricingDataset
    peer_context: np.ndarray
    coverage_by_date: tuple[dict[str, Any], ...]

    def validate(self) -> "IndustryRelativeDataset":
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
                "row_count": int(len(selected)),
                "published_label_count": int(len(labels)),
                "fresh_label_count": int(len(fresh)),
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
        feature_names=tuple(dataset.feature_names) + tuple(
            f"industry_relative:{name}" for name in dataset.feature_names
        ),
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


def _ridge_predict(
    train_values: np.ndarray,
    train_mask: np.ndarray,
    train_target: np.ndarray,
    test_values: np.ndarray,
    test_mask: np.ndarray,
    feature_names: tuple[str, ...],
    ridge_alpha: float,
) -> np.ndarray:
    transformer = RobustFeatureTransformer(feature_names).fit(
        train_values, train_mask
    )
    train_x = transformer.transform(train_values, train_mask)
    test_x = transformer.transform(test_values, test_mask)
    penalty = np.eye(train_x.shape[1], dtype=np.float64) * float(ridge_alpha)
    coefficients = np.linalg.pinv(train_x.T @ train_x + penalty) @ train_x.T @ train_target
    return test_x @ coefficients


def _rank_ic(score: np.ndarray, target: np.ndarray) -> float | None:
    if len(score) < 3 or np.std(score) <= 1e-12 or np.std(target) <= 1e-12:
        return None
    value = spearmanr(score, target).statistic
    return float(value) if value is not None and np.isfinite(value) else None


def _top_bottom_spread(score: np.ndarray, target: np.ndarray, fraction: float) -> float | None:
    count = max(1, int(np.floor(len(score) * fraction)))
    if len(score) < count * 2:
        return None
    order = np.argsort(score, kind="mergesort")
    return float(np.mean(target[order[-count:]]) - np.mean(target[order[:count]]))


def _summary(rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    ic = [float(row[f"{prefix}_rank_ic"]) for row in rows if row[f"{prefix}_rank_ic"] is not None]
    spread = [float(row[f"{prefix}_top_bottom_spread"]) for row in rows if row[f"{prefix}_top_bottom_spread"] is not None]
    return {
        "quarter_count": len(rows),
        "mean_rank_ic": float(np.mean(ic)) if ic else None,
        "positive_rank_ic_quarters": int(sum(item > 0 for item in ic)),
        "rank_ic_quarters": len(ic),
        "mean_top_bottom_spread": float(np.mean(spread)) if spread else None,
    }


def evaluate_industry_relative_increment(
    relative: IndustryRelativeDataset,
    config: IndustryRidgeConfig | None = None,
) -> dict[str, Any]:
    """Compare raw fundamentals and raw-plus-industry features on equal rows."""

    relative.validate()
    settings = config or IndustryRidgeConfig()
    dataset = relative.dataset
    base_count = len(dataset.feature_names) // 2
    dates = np.asarray(dataset.feature_dates, dtype=object)
    label_ends = np.asarray(dataset.label_end_dates, dtype=object)
    eligible_dates = {
        date.fromisoformat(item["feature_date"])
        for item in relative.coverage_by_date
        if item["eligible_for_industry_evaluation"]
    }
    rows: list[dict[str, Any]] = []
    for test_date in sorted(eligible_dates):
        test = (dates == test_date) & relative.peer_context
        train = (label_ends < test_date) & relative.peer_context & np.isin(
            dates, list(eligible_dates)
        )
        if int(test.sum()) < 3:
            continue
        train_dates = sorted(set(dates[train]))
        if (
            len(train_dates) < settings.minimum_train_dates
            or int(train.sum()) < settings.minimum_train_rows
        ):
            continue
        target_train = dataset.excess_returns[train].astype(np.float64)
        target_test = dataset.excess_returns[test].astype(np.float64)
        base_score = _ridge_predict(
            dataset.values[train, :base_count],
            dataset.availability_mask[train, :base_count],
            target_train,
            dataset.values[test, :base_count],
            dataset.availability_mask[test, :base_count],
            dataset.feature_names[:base_count],
            settings.ridge_alpha,
        )
        combined_score = _ridge_predict(
            dataset.values[train],
            dataset.availability_mask[train],
            target_train,
            dataset.values[test],
            dataset.availability_mask[test],
            dataset.feature_names,
            settings.ridge_alpha,
        )
        rows.append(
            {
                "feature_date": test_date.isoformat(),
                "train_row_count": int(train.sum()),
                "train_date_count": len(train_dates),
                "test_row_count": int(test.sum()),
                "base_rank_ic": _rank_ic(base_score, target_test),
                "base_top_bottom_spread": _top_bottom_spread(
                    base_score, target_test, settings.top_bottom_fraction
                ),
                "industry_augmented_rank_ic": _rank_ic(combined_score, target_test),
                "industry_augmented_top_bottom_spread": _top_bottom_spread(
                    combined_score, target_test, settings.top_bottom_fraction
                ),
            }
        )
    paired = [
        row["industry_augmented_rank_ic"] - row["base_rank_ic"]
        for row in rows
        if row["industry_augmented_rank_ic"] is not None and row["base_rank_ic"] is not None
    ]
    return {
        "contract": "industry-relative-ridge-increment-1",
        "configuration": {
            "maximum_label_age_days": settings.maximum_label_age_days,
            "minimum_industry_peers": settings.minimum_industry_peers,
            "minimum_date_coverage": settings.minimum_date_coverage,
            "minimum_train_dates": settings.minimum_train_dates,
            "minimum_train_rows": settings.minimum_train_rows,
            "ridge_alpha": settings.ridge_alpha,
        },
        "coverage_by_date": list(relative.coverage_by_date),
        "base": _summary(rows, "base"),
        "industry_augmented": _summary(rows, "industry_augmented"),
        "paired_rank_ic": {
            "quarter_count": len(paired),
            "mean_delta": float(np.mean(paired)) if paired else None,
            "industry_win_count": int(sum(item > 0 for item in paired)),
            "base_win_count": int(sum(item < 0 for item in paired)),
        },
        "quarterly_results": rows,
        "acceptance": {
            "current_taxonomy_not_used": True,
            "published_at_before_feature_date": True,
            "training_labels_end_before_test_date": True,
            "research_only": True,
        },
    }
