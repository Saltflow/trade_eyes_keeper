#!/usr/bin/env python3
"""Evaluate fixed rank blends of base, valuation and industry experts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_valuation_consensus_quality import (
    VALUATION_COLUMNS,
    _build_augmented,
)
from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry_evaluation import (
    _rank_ic,
    _ridge_predict,
    _top_bottom_spread,
    build_industry_relative_dataset,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.fundamental_embedding.valuation_industry_evaluation import (
    ValuationIndustryEvaluationConfig,
)


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    result = np.zeros(len(values), dtype=np.float64)
    if len(values) > 1:
        result[order] = np.linspace(-1.0, 1.0, len(values))
    return result


def _columns(
    values: np.ndarray,
    masks: np.ndarray,
    names: tuple[str, ...],
    indices: list[int],
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...]]:
    return values[:, indices], masks[:, indices], tuple(names[index] for index in indices)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--valuation-csv", required=True)
    parser.add_argument("--industry-history", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    raw = QuarterlyPricingDatasetBuilder(args.data_root, market="a_share").build()
    frame = pd.read_csv(args.valuation_csv)
    frame["feature_date"] = pd.to_datetime(
        frame["feature_date"], errors="coerce"
    ).dt.date
    history = IndustryClassificationHistoryStore(args.industry_history)
    config = ValuationIndustryEvaluationConfig(
        maximum_label_age_days=366,
        minimum_date_coverage=0.70,
        minimum_train_dates=2,
        minimum_train_rows=100,
        base_feature_count=len(raw.feature_names),
        valuation_feature_count=len(VALUATION_COLUMNS),
    )
    weights = (0.0, 0.10, 0.20, 0.30, 0.50, 0.75, 1.0)
    results: dict[str, object] = {}
    for policy in ("consensus_only", "consensus_low_dispersion_10pct"):
        augmented, join = _build_augmented(raw, frame, policy, market_only=False)
        relative = build_industry_relative_dataset(augmented, history, config)
        dataset = relative.dataset
        dates = np.asarray(dataset.feature_dates, dtype=object)
        label_ends = np.asarray(dataset.label_end_dates, dtype=object)
        base_count = len(raw.feature_names)
        valuation_end = base_count + len(VALUATION_COLUMNS)
        industry_base_start = valuation_end
        industry_valuation_start = valuation_end + base_count
        full_end = len(dataset.feature_names)
        eligible = {
            date.fromisoformat(item["feature_date"])
            for item in relative.coverage_by_date
            if item["eligible_for_industry_evaluation"]
        }
        rows: list[dict[str, object]] = []
        for test_date in sorted(eligible):
            test = (dates == test_date) & relative.peer_context
            train = (
                (label_ends < test_date)
                & relative.peer_context
                & np.isin(dates, list(eligible))
            )
            train_dates = sorted(set(dates[train]))
            if (
                int(test.sum()) < 3
                or len(train_dates) < config.minimum_train_dates
                or int(train.sum()) < config.minimum_train_rows
            ):
                continue
            target_train = dataset.excess_returns[train].astype(np.float64)
            target_test = dataset.excess_returns[test].astype(np.float64)
            names = dataset.feature_names
            values = dataset.values
            masks = dataset.availability_mask
            raw_indices = list(range(base_count))
            valuation_indices = list(range(base_count, valuation_end))
            base_industry_indices = raw_indices + list(
                range(industry_base_start, industry_valuation_start)
            )
            valuation_industry_indices = valuation_indices + list(
                range(industry_valuation_start, full_end)
            )
            scores: dict[str, np.ndarray] = {}
            for name, indices in (
                ("base", raw_indices),
                ("valuation", valuation_indices),
                ("base_industry", base_industry_indices),
                ("valuation_industry", valuation_industry_indices),
            ):
                train_values, train_masks, feature_names = _columns(
                    values[train], masks[train], names, indices
                )
                test_values, test_masks, _ = _columns(
                    values[test], masks[test], names, indices
                )
                scores[name] = _ridge_predict(
                    train_values,
                    train_masks,
                    target_train,
                    test_values,
                    test_masks,
                    feature_names,
                    config.ridge_alpha,
                )
            base_rank = _rank(scores["base_industry"])
            valuation_rank = _rank(scores["valuation"])
            valuation_industry_rank = _rank(scores["valuation_industry"])
            row: dict[str, object] = {
                "feature_date": test_date.isoformat(),
                "train_row_count": int(train.sum()),
                "test_row_count": int(test.sum()),
            }
            for weight in weights:
                key = f"{weight:g}"
                score = base_rank + float(weight) * valuation_rank
                row[f"base_plus_valuation_{key}_rank_ic"] = _rank_ic(
                    score, target_test
                )
                row[f"base_plus_valuation_{key}_spread"] = _top_bottom_spread(
                    score, target_test, config.top_bottom_fraction
                )
                score_industry = base_rank + float(weight) * valuation_industry_rank
                row[f"base_plus_valuation_industry_{key}_rank_ic"] = _rank_ic(
                    score_industry, target_test
                )
                row[f"base_plus_valuation_industry_{key}_spread"] = _top_bottom_spread(
                    score_industry, target_test, config.top_bottom_fraction
                )
            rows.append(row)
        summary: dict[str, object] = {"join": join, "quarterly_results": rows}
        for prefix in ("base_plus_valuation", "base_plus_valuation_industry"):
            by_weight: dict[str, dict[str, float | None]] = {}
            for weight in weights:
                key = f"{weight:g}"
                ics = [
                    row[f"{prefix}_{key}_rank_ic"]
                    for row in rows
                    if row[f"{prefix}_{key}_rank_ic"] is not None
                ]
                spreads = [
                    row[f"{prefix}_{key}_spread"]
                    for row in rows
                    if row[f"{prefix}_{key}_spread"] is not None
                ]
                by_weight[key] = {
                    "mean_rank_ic": float(np.mean(ics)) if ics else None,
                    "positive_quarters": int(sum(value > 0 for value in ics)),
                    "mean_spread": float(np.mean(spreads)) if spreads else None,
                }
            summary[prefix] = by_weight
        results[policy] = summary

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "contract": "valuation-consensus-fixed-rank-blend-1",
        "data_root": str(Path(args.data_root).resolve()),
        "valuation_csv": str(Path(args.valuation_csv).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "weights": list(weights),
        "results": results,
        "acceptance": {
            "weights_fixed_before_test": True,
            "training_labels_end_before_test_date": True,
            "published_at_before_feature_date": True,
            "current_taxonomy_not_used": True,
            "research_only": True,
        },
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
