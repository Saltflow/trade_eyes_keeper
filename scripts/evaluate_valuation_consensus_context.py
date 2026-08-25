#!/usr/bin/env python3
"""Evaluate the formal quality-gated valuation context contract OOS."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry_evaluation import (
    IndustryRidgeConfig,
    _rank_ic,
    _ridge_predict,
    _summary,
    _top_bottom_spread,
    build_industry_relative_dataset,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.fundamental_embedding.valuation_consensus_context import (
    ValuationConsensusQualityConfig,
    attach_consensus_valuation_features,
)
from src.fundamental_embedding.valuation_context import (
    ValuationContextConfig,
    build_historical_valuation_context,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--valuation-csv", required=True)
    parser.add_argument("--industry-history", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    raw = QuarterlyPricingDatasetBuilder(args.data_root, market="a_share").build()
    raw.validate()
    frame = pd.read_csv(args.valuation_csv)
    frame["feature_date"] = pd.to_datetime(
        frame["feature_date"], errors="coerce"
    ).dt.date
    rows = frame.to_dict("records")
    history = IndustryClassificationHistoryStore(args.industry_history)
    industry_config = IndustryRidgeConfig(
        maximum_label_age_days=366,
        minimum_date_coverage=0.70,
        minimum_train_dates=2,
        minimum_train_rows=100,
    )
    results: dict[str, object] = {}
    for quality in (
        ValuationConsensusQualityConfig(
            minimum_expert_count=2,
            maximum_dispersion=0.10,
            require_consensus=True,
        ),
        ValuationConsensusQualityConfig(
            minimum_expert_count=2,
            maximum_dispersion=0.05,
            require_consensus=True,
        ),
    ):
        source = attach_consensus_valuation_features(raw, rows, quality)
        relative = build_industry_relative_dataset(source, history, industry_config)
        context = build_historical_valuation_context(
            relative, ValuationContextConfig()
        )
        dates = np.asarray(context.feature_dates, dtype=object)
        label_ends = np.asarray(context.label_end_dates, dtype=object)
        base_count = len(raw.feature_names)
        eligible = {
            date.fromisoformat(item["feature_date"])
            for item in relative.coverage_by_date
            if item["eligible_for_industry_evaluation"]
        }
        rows_out: list[dict[str, object]] = []
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
                or len(train_dates) < industry_config.minimum_train_dates
                or int(train.sum()) < industry_config.minimum_train_rows
            ):
                continue
            target_train = context.excess_returns[train].astype(np.float64)
            target_test = context.excess_returns[test].astype(np.float64)
            base_score = _ridge_predict(
                context.values[train, :base_count],
                context.availability_mask[train, :base_count],
                target_train,
                context.values[test, :base_count],
                context.availability_mask[test, :base_count],
                context.feature_names[:base_count],
                industry_config.ridge_alpha,
            )
            context_score = _ridge_predict(
                context.values[train],
                context.availability_mask[train],
                target_train,
                context.values[test],
                context.availability_mask[test],
                context.feature_names,
                industry_config.ridge_alpha,
            )
            rows_out.append(
                {
                    "feature_date": test_date.isoformat(),
                    "train_row_count": int(train.sum()),
                    "test_row_count": int(test.sum()),
                    "base_rank_ic": _rank_ic(base_score, target_test),
                    "context_rank_ic": _rank_ic(context_score, target_test),
                    "base_top_bottom_spread": _top_bottom_spread(
                        base_score, target_test, industry_config.top_bottom_fraction
                    ),
                    "context_top_bottom_spread": _top_bottom_spread(
                        context_score,
                        target_test,
                        industry_config.top_bottom_fraction,
                    ),
                }
            )
        context_rows = [
            {
                "base_rank_ic": row["base_rank_ic"],
                "base_top_bottom_spread": row["base_top_bottom_spread"],
                "context_rank_ic": row["context_rank_ic"],
                "context_top_bottom_spread": row["context_top_bottom_spread"],
            }
            for row in rows_out
        ]
        deltas = [
            row["context_rank_ic"] - row["base_rank_ic"]
            for row in context_rows
            if row["context_rank_ic"] is not None
            and row["base_rank_ic"] is not None
        ]
        spreads = [
            row["context_top_bottom_spread"] - row["base_top_bottom_spread"]
            for row in context_rows
            if row["context_top_bottom_spread"] is not None
            and row["base_top_bottom_spread"] is not None
        ]
        key = f"dispersion_le_{quality.maximum_dispersion:g}"
        results[key] = {
            "quality": asdict(quality),
            "source_metadata": {
                "matched_rows": source.metadata["matched_valuation_rows"],
                "gated_rows": source.metadata["gated_valuation_rows"],
            },
            "base": _summary(context_rows, "base"),
            "context": _summary(context_rows, "context"),
            "paired": {
                "mean_rank_ic_delta": float(np.mean(deltas)) if deltas else None,
                "mean_spread_delta": float(np.mean(spreads)) if spreads else None,
                "win_count": int(sum(value > 0 for value in deltas)),
                "loss_count": int(sum(value < 0 for value in deltas)),
            },
            "quarterly_results": rows_out,
            "acceptance": context.metadata,
        }

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "contract": "valuation-consensus-formal-context-increment-1",
        "data_root": str(Path(args.data_root).resolve()),
        "valuation_csv": str(Path(args.valuation_csv).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "results": results,
        "research_only": True,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
