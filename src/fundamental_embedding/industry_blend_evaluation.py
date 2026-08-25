"""Fixed-weight, cross-sectional blends for industry-relative research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from scipy.stats import spearmanr

from .industry_evaluation import (
    IndustryRelativeDataset,
    IndustryRidgeConfig,
    _ridge_predict,
)
from .industry_factor_evaluation import industry_relative_factors


@dataclass(frozen=True)
class IndustryBlendConfig(IndustryRidgeConfig):
    """Weights are fixed before evaluation; no test-period tuning is allowed."""

    weights: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0)


def _rank(values: np.ndarray) -> np.ndarray:
    if len(values) < 2 or np.std(values) <= 1e-12:
        return np.zeros(len(values), dtype=np.float64)
    order = np.argsort(values, kind="mergesort")
    result = np.zeros(len(values), dtype=np.float64)
    result[order] = np.linspace(-1.0, 1.0, len(values))
    return result


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
    ic = [
        float(row[prefix]["rank_ic"])
        for row in rows
        if row[prefix]["rank_ic"] is not None
    ]
    spread = [
        float(row[prefix]["top_bottom_spread"])
        for row in rows
        if row[prefix]["top_bottom_spread"] is not None
    ]
    return {
        "quarter_count": len(rows),
        "mean_rank_ic": float(np.mean(ic)) if ic else None,
        "positive_rank_ic_quarters": int(sum(item > 0 for item in ic)),
        "rank_ic_quarters": len(ic),
        "mean_top_bottom_spread": float(np.mean(spread)) if spread else None,
    }


def evaluate_industry_rank_blends(
    relative: IndustryRelativeDataset,
    config: IndustryBlendConfig | None = None,
) -> dict[str, Any]:
    """Evaluate fixed rank blends without using test outcomes to pick a weight."""

    relative.validate()
    settings = config or IndustryBlendConfig()
    if any(weight < 0 for weight in settings.weights):
        raise ValueError("industry blend weights must be nonnegative")
    dataset = relative.dataset
    base_count = len(dataset.feature_names) // 2
    factor_values, factor_mask = industry_relative_factors(relative)
    dates = np.asarray(dataset.feature_dates, dtype=object)
    label_ends = np.asarray(dataset.label_end_dates, dtype=object)
    eligible_dates = {
        date.fromisoformat(item["feature_date"])
        for item in relative.coverage_by_date
        if item.get("eligible_for_industry_evaluation")
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
        base_train = dataset.values[train, :base_count]
        base_test = dataset.values[test, :base_count]
        base_mask_train = dataset.availability_mask[train, :base_count]
        base_mask_test = dataset.availability_mask[test, :base_count]
        factor_train = factor_values[train]
        factor_test = factor_values[test]
        factor_mask_train = factor_mask[train]
        factor_mask_test = factor_mask[test]
        base_score = _ridge_predict(
            base_train,
            base_mask_train,
            target_train,
            base_test,
            base_mask_test,
            dataset.feature_names[:base_count],
            settings.ridge_alpha,
        )
        factor_score = _ridge_predict(
            factor_train,
            factor_mask_train,
            target_train,
            factor_test,
            factor_mask_test,
            tuple(f"industry_factor:{index}" for index in range(factor_values.shape[1])),
            settings.ridge_alpha,
        )
        base_rank = _rank(base_score)
        factor_rank = _rank(factor_score)
        by_weight: dict[str, dict[str, float | None]] = {}
        for weight in settings.weights:
            score = base_rank + float(weight) * factor_rank
            key = f"{float(weight):g}"
            by_weight[key] = {
                "rank_ic": _rank_ic(score, target_test),
                "top_bottom_spread": _spread(
                    score, target_test, settings.top_bottom_fraction
                ),
            }
        rows.append({
            "feature_date": test_date.isoformat(),
            "train_row_count": int(train.sum()),
            "train_date_count": len(train_dates),
            "test_row_count": int(test.sum()),
            "weights": by_weight,
        })
    summaries = {
        f"{float(weight):g}": _summary(rows, "weights")
        for weight in ()
    }
    for weight in settings.weights:
        key = f"{float(weight):g}"
        summaries[key] = _summary(
            [
                {"weights": row["weights"][key]}
                for row in rows
            ],
            "weights",
        )
    baseline_key = f"{float(settings.weights[0]):g}"
    deltas: dict[str, dict[str, Any]] = {}
    for weight in settings.weights:
        key = f"{float(weight):g}"
        paired = [
            row["weights"][key]["rank_ic"]
            - row["weights"][baseline_key]["rank_ic"]
            for row in rows
            if row["weights"][key]["rank_ic"] is not None
            and row["weights"][baseline_key]["rank_ic"] is not None
        ]
        deltas[key] = {
            "quarter_count": len(paired),
            "mean_rank_ic_delta": float(np.mean(paired)) if paired else None,
            "win_count": int(sum(item > 0 for item in paired)),
            "loss_count": int(sum(item < 0 for item in paired)),
        }
    return {
        "contract": "industry-relative-rank-blend-evaluation-1",
        "weights": [float(item) for item in settings.weights],
        "summaries": summaries,
        "paired_vs_baseline": deltas,
        "quarterly_results": rows,
        "acceptance": {
            "weights_fixed_before_test": True,
            "published_at_before_feature_date": True,
            "training_labels_end_before_test_date": True,
            "current_taxonomy_not_used": True,
            "research_only": True,
        },
    }
