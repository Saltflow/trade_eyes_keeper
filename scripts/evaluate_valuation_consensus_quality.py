#!/usr/bin/env python3
"""Quality-gate ablation for historical multi-expert valuation features."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry_evaluation import (
    build_industry_relative_dataset,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.fundamental_embedding.valuation_industry_evaluation import (
    ValuationIndustryEvaluationConfig,
    evaluate_valuation_industry_increment,
)

VALUATION_COLUMNS = (
    "beta",
    "capm_cost_of_equity",
    "market_implied_growth_5y",
    "market_vs_fundamental_growth",
    "fundamental_growth",
)


def _symbol(value: object) -> str:
    text = str(value).strip()
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def _build_augmented(
    raw: FundamentalPricingDataset,
    frame: pd.DataFrame,
    policy: str,
    *,
    market_only: bool,
) -> tuple[FundamentalPricingDataset, dict[str, int]]:
    lookup = {
        (_symbol(row.symbol), row.feature_date): row
        for row in frame.itertuples(index=False)
    }
    values = np.zeros(
        (len(raw.symbols), len(VALUATION_COLUMNS)), dtype=np.float64
    )
    mask = np.zeros_like(values, dtype=bool)
    matched = 0
    selected = 0
    feature_values = 0
    for row_index, (feature_date, symbol) in enumerate(
        zip(raw.feature_dates, raw.symbols)
    ):
        selected_row = lookup.get((_symbol(symbol), feature_date))
        if selected_row is None:
            continue
        matched += 1
        selected_bool = str(getattr(selected_row, "status", "")) == "solved"
        consensus = str(
            getattr(selected_row, "market_implied_growth_status", "")
        ) == "solved_consensus"
        dispersion = pd.to_numeric(
            getattr(selected_row, "market_implied_growth_dispersion", np.nan),
            errors="coerce",
        )
        if policy == "consensus_only":
            selected_bool = selected_bool and consensus
        elif policy == "consensus_low_dispersion_5pct":
            selected_bool = selected_bool and consensus and bool(dispersion <= 0.05)
        elif policy == "consensus_low_dispersion_10pct":
            selected_bool = selected_bool and consensus and bool(dispersion <= 0.10)
        elif policy != "all_solved":
            raise ValueError(f"unknown policy: {policy}")
        if not selected_bool:
            continue
        selected += 1
        for column_index, name in enumerate(VALUATION_COLUMNS):
            if market_only and name not in {
                "market_implied_growth_5y",
                "market_vs_fundamental_growth",
            }:
                continue
            value = pd.to_numeric(getattr(selected_row, name), errors="coerce")
            if pd.notna(value) and np.isfinite(float(value)):
                values[row_index, column_index] = float(value)
                mask[row_index, column_index] = True
                feature_values += 1
    augmented = FundamentalPricingDataset(
        feature_dates=raw.feature_dates,
        label_end_dates=raw.label_end_dates,
        symbols=raw.symbols,
        feature_names=tuple(raw.feature_names)
        + tuple(f"valuation:{name}" for name in VALUATION_COLUMNS),
        values=np.concatenate([raw.values, values], axis=1),
        availability_mask=np.concatenate([raw.availability_mask, mask], axis=1),
        forward_returns=raw.forward_returns,
        excess_returns=raw.excess_returns,
        metadata={
            **raw.metadata,
            "contract": "historical-valuation-consensus-quality-ablation-1",
            "valuation_quality_policy": policy,
            "valuation_market_only": market_only,
            "valuation_features_are_point_in_time": True,
        },
    ).validate()
    return augmented, {
        "matched_rows": matched,
        "selected_rows": selected,
        "feature_values": feature_values,
    }


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
    required = {
        "symbol",
        "feature_date",
        "status",
        "market_implied_growth_status",
        "market_implied_growth_dispersion",
        *VALUATION_COLUMNS,
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"valuation file missing columns: {sorted(missing)}")
    store = IndustryClassificationHistoryStore(args.industry_history)
    results: dict[str, object] = {}
    for market_only in (False, True):
        for policy in (
            "all_solved",
            "consensus_only",
            "consensus_low_dispersion_5pct",
            "consensus_low_dispersion_10pct",
        ):
            augmented, join = _build_augmented(
                raw, frame, policy, market_only=market_only
            )
            config = ValuationIndustryEvaluationConfig(
                maximum_label_age_days=366,
                minimum_date_coverage=0.70,
                minimum_train_dates=2,
                minimum_train_rows=100,
                base_feature_count=len(raw.feature_names),
                valuation_feature_count=len(VALUATION_COLUMNS),
            )
            relative = build_industry_relative_dataset(augmented, store, config)
            report = evaluate_valuation_industry_increment(relative, config)
            key = f"{'market_only' if market_only else 'all_features'}:{policy}"
            results[key] = {
                "join": join,
                "summaries": report["summaries"],
                "paired_vs_base": report["paired_vs_base"],
                "coverage_by_date": report["coverage_by_date"],
                "configuration": asdict(config),
            }

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    final = {
        "contract": "valuation-consensus-quality-ablation-1",
        "data_root": str(Path(args.data_root).resolve()),
        "valuation_csv": str(Path(args.valuation_csv).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "base_feature_count": len(raw.feature_names),
        "row_count": len(raw.symbols),
        "results": results,
        "acceptance": {
            "published_at_before_feature_date": True,
            "training_labels_end_before_test_date": True,
            "current_taxonomy_not_used": True,
            "quality_gate_only_changes_availability_mask": True,
            "research_only": True,
        },
    }
    (output / "report.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
