"""Nested, point-in-time evaluation of valuation and industry features."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Sequence

import numpy as np

from .industry_evaluation import (
    IndustryRelativeDataset,
    IndustryRidgeConfig,
    _rank_ic,
    _ridge_predict,
    _summary,
    _top_bottom_spread,
)


@dataclass(frozen=True)
class ValuationIndustryEvaluationConfig(IndustryRidgeConfig):
    """Configuration with explicit raw and valuation feature block sizes."""

    base_feature_count: int = 19
    valuation_feature_count: int = 5

    def validate(self) -> ValuationIndustryEvaluationConfig:
        if self.base_feature_count <= 0:
            raise ValueError("base_feature_count must be positive")
        if self.valuation_feature_count <= 0:
            raise ValueError("valuation_feature_count must be positive")
        return self


def _score(
    dataset: Any,
    train: np.ndarray,
    test: np.ndarray,
    start: int,
    end: int,
    target_train: np.ndarray,
    feature_names: Sequence[str],
    ridge_alpha: float,
) -> np.ndarray:
    return _ridge_predict(
        dataset.values[train, start:end],
        dataset.availability_mask[train, start:end],
        target_train,
        dataset.values[test, start:end],
        dataset.availability_mask[test, start:end],
        tuple(feature_names[start:end]),
        ridge_alpha,
    )


def evaluate_valuation_industry_increment(
    relative: IndustryRelativeDataset,
    config: ValuationIndustryEvaluationConfig | None = None,
) -> dict[str, Any]:
    """Compare raw, valuation, industry, and combined nested models."""

    settings = (config or ValuationIndustryEvaluationConfig()).validate()
    relative.validate()
    dataset = relative.dataset
    base_end = settings.base_feature_count
    valuation_end = base_end + settings.valuation_feature_count
    expected_full = valuation_end * 2
    if len(dataset.feature_names) != expected_full:
        raise ValueError(
            "unexpected feature block count: "
            f"expected {expected_full}, got {len(dataset.feature_names)}"
        )

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
        scores = {
            "base": _score(dataset, train, test, 0, base_end, target_train,
                           dataset.feature_names, settings.ridge_alpha),
            "valuation": _score(dataset, train, test, 0, valuation_end, target_train,
                                dataset.feature_names, settings.ridge_alpha),
            "industry": _score(dataset, train, test, 0, base_end * 2, target_train,
                               dataset.feature_names, settings.ridge_alpha),
            "valuation_industry": _score(dataset, train, test, 0, expected_full,
                                          target_train, dataset.feature_names,
                                          settings.ridge_alpha),
        }
        row: dict[str, Any] = {
            "feature_date": test_date.isoformat(),
            "train_row_count": int(train.sum()),
            "train_date_count": len(train_dates),
            "test_row_count": int(test.sum()),
        }
        for name, score in scores.items():
            row[f"{name}_rank_ic"] = _rank_ic(score, target_test)
            row[f"{name}_top_bottom_spread"] = _top_bottom_spread(
                score, target_test, settings.top_bottom_fraction
            )
        rows.append(row)

    names = ("base", "valuation", "industry", "valuation_industry")
    summaries = {name: _summary(rows, name) for name in names}
    paired: dict[str, dict[str, Any]] = {}
    for name in names[1:]:
        deltas = [
            row[f"{name}_rank_ic"] - row["base_rank_ic"]
            for row in rows
            if row[f"{name}_rank_ic"] is not None
            and row["base_rank_ic"] is not None
        ]
        spreads = [
            row[f"{name}_top_bottom_spread"] - row["base_top_bottom_spread"]
            for row in rows
            if row[f"{name}_top_bottom_spread"] is not None
            and row["base_top_bottom_spread"] is not None
        ]
        paired[name] = {
            "rank_ic_quarter_count": len(deltas),
            "mean_rank_ic_delta": float(np.mean(deltas)) if deltas else None,
            "win_count": int(sum(value > 0 for value in deltas)),
            "loss_count": int(sum(value < 0 for value in deltas)),
            "mean_top_bottom_spread_delta": float(np.mean(spreads)) if spreads else None,
        }
    return {
        "contract": "valuation-industry-nested-ridge-increment-1",
        "configuration": asdict(settings),
        "summaries": summaries,
        "paired_vs_base": paired,
        "coverage_by_date": list(relative.coverage_by_date),
        "quarterly_results": rows,
        "acceptance": {
            "published_at_before_feature_date": True,
            "training_labels_end_before_test_date": True,
            "valuation_features_are_dated": True,
            "current_taxonomy_not_used": True,
            "research_only": True,
        },
    }
