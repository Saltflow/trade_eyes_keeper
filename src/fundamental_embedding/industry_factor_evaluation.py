"""Low-dimensional industry-relative factor evaluation.

Appending nineteen raw industry ranks to nineteen raw fundamentals can create
an unstable, highly collinear Ridge problem in a short historical window.
This evaluator keeps the existing four fixed economic factor meanings and
tests only four industry-within-peer counterparts.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
from scipy.stats import spearmanr

from .exposure import RobustFeatureTransformer
from .industry_evaluation import IndustryRelativeDataset, IndustryRidgeConfig
from .split_api import FACTOR_FEATURE_DIRECTIONS


INDUSTRY_FACTOR_NAMES = tuple(
    f"industry_relative:{name}" for name in FACTOR_FEATURE_DIRECTIONS
)


def industry_relative_factors(
    relative: IndustryRelativeDataset,
) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate signed peer ranks into four auditable economic factors."""

    relative.validate()
    dataset = relative.dataset
    base_count = len(dataset.feature_names) // 2
    base_names = dataset.feature_names[:base_count]
    positions = {name: index for index, name in enumerate(base_names)}
    values = np.zeros((len(dataset.symbols), len(INDUSTRY_FACTOR_NAMES)))
    available = np.zeros_like(values, dtype=bool)
    for factor_index, (factor, directions) in enumerate(
        FACTOR_FEATURE_DIRECTIONS.items()
    ):
        indices = np.asarray([positions[name] for name in directions], dtype=int)
        signs = np.asarray(list(directions.values()), dtype=np.float64)
        factor_values = dataset.values[:, base_count + indices]
        factor_mask = dataset.availability_mask[:, base_count + indices]
        counts = factor_mask.sum(axis=1)
        required = max(1, (len(indices) + 1) // 2)
        valid = counts >= required
        values[valid, factor_index] = (
            np.where(factor_mask[valid], factor_values[valid] * signs, 0.0).sum(axis=1)
            / counts[valid]
        )
        available[:, factor_index] = valid
    return values, available


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


def _spread(score: np.ndarray, target: np.ndarray, fraction: float) -> float | None:
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


def evaluate_industry_factor_increment(
    relative: IndustryRelativeDataset,
    config: IndustryRidgeConfig | None = None,
) -> dict[str, Any]:
    """Compare raw fundamentals with raw fundamentals plus four peer factors."""

    relative.validate()
    settings = config or IndustryRidgeConfig()
    dataset = relative.dataset
    base_count = len(dataset.feature_names) // 2
    factor_values, factor_mask = industry_relative_factors(relative)
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
        train_dates = sorted(set(dates[train]))
        if (
            int(test.sum()) < 3
            or len(train_dates) < settings.minimum_train_dates
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
        augmented_score = _ridge_predict(
            np.concatenate([dataset.values[train, :base_count], factor_values[train]], axis=1),
            np.concatenate([dataset.availability_mask[train, :base_count], factor_mask[train]], axis=1),
            target_train,
            np.concatenate([dataset.values[test, :base_count], factor_values[test]], axis=1),
            np.concatenate([dataset.availability_mask[test, :base_count], factor_mask[test]], axis=1),
            dataset.feature_names[:base_count] + INDUSTRY_FACTOR_NAMES,
            settings.ridge_alpha,
        )
        rows.append(
            {
                "feature_date": test_date.isoformat(),
                "train_row_count": int(train.sum()),
                "train_date_count": len(train_dates),
                "test_row_count": int(test.sum()),
                "base_rank_ic": _rank_ic(base_score, target_test),
                "base_top_bottom_spread": _spread(base_score, target_test, settings.top_bottom_fraction),
                "industry_factor_rank_ic": _rank_ic(augmented_score, target_test),
                "industry_factor_top_bottom_spread": _spread(augmented_score, target_test, settings.top_bottom_fraction),
            }
        )
    paired = [
        item["industry_factor_rank_ic"] - item["base_rank_ic"]
        for item in rows
        if item["industry_factor_rank_ic"] is not None and item["base_rank_ic"] is not None
    ]
    return {
        "contract": "industry-relative-factor-increment-1",
        "factor_names": list(INDUSTRY_FACTOR_NAMES),
        "base": _summary(rows, "base"),
        "industry_factor": _summary(rows, "industry_factor"),
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
            "low_dimensional_industry_augmentation": True,
            "research_only": True,
        },
    }
