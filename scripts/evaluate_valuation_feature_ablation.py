#!/usr/bin/env python3
"""Ablate each dated valuation feature against the same industry context."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.dataset import QuarterlyPricingDatasetBuilder
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

COLUMNS = (
    "beta",
    "capm_cost_of_equity",
    "market_implied_growth_5y",
    "market_vs_fundamental_growth",
    "fundamental_growth",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--valuation-csv", required=True)
    parser.add_argument("--industry-history", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    raw = QuarterlyPricingDatasetBuilder(args.data_root, market="a_share").build()
    frame = pd.read_csv(args.valuation_csv)
    frame["feature_date"] = pd.to_datetime(frame["feature_date"]).dt.date
    lookup = {
        (str(row.symbol), row.feature_date): row
        for row in frame.itertuples(index=False)
    }
    values = np.zeros((len(raw.symbols), len(COLUMNS)), dtype=np.float64)
    mask = np.zeros_like(values, dtype=bool)
    for index, (feature_date, symbol) in enumerate(zip(raw.feature_dates, raw.symbols)):
        row = lookup.get((str(symbol), feature_date))
        if row is None:
            continue
        for column_index, name in enumerate(COLUMNS):
            value = pd.to_numeric(getattr(row, name), errors="coerce")
            if str(getattr(row, "status", "")) == "solved" and pd.notna(value) and np.isfinite(float(value)):
                values[index, column_index] = float(value)
                mask[index, column_index] = True
    all_values = np.concatenate([raw.values, values], axis=1)
    all_mask = np.concatenate([raw.availability_mask, mask], axis=1)
    all_names = tuple(raw.feature_names) + tuple(f"valuation:{name}" for name in COLUMNS)
    full = FundamentalPricingDataset(
        feature_dates=raw.feature_dates,
        label_end_dates=raw.label_end_dates,
        symbols=raw.symbols,
        feature_names=all_names,
        values=all_values,
        availability_mask=all_mask,
        forward_returns=raw.forward_returns,
        excess_returns=raw.excess_returns,
        metadata={"contract": "historical-valuation-feature-ablation-1"},
    ).validate()
    relative = build_industry_relative_dataset(
        full, IndustryClassificationHistoryStore(args.industry_history)
    )
    output: dict[str, object] = {}
    for index, name in enumerate(COLUMNS):
        keep = list(range(19)) + [19 + index] + list(range(24, 43)) + [43 + index]
        sliced = FundamentalPricingDataset(
            feature_dates=relative.dataset.feature_dates,
            label_end_dates=relative.dataset.label_end_dates,
            symbols=relative.dataset.symbols,
            feature_names=tuple(relative.dataset.feature_names[i] for i in keep),
            values=relative.dataset.values[:, keep],
            availability_mask=relative.dataset.availability_mask[:, keep],
            forward_returns=relative.dataset.forward_returns,
            excess_returns=relative.dataset.excess_returns,
            metadata={**relative.dataset.metadata, "ablation": name},
        ).validate()
        report = evaluate_valuation_industry_increment(
            type(relative)(
                dataset=sliced,
                peer_context=relative.peer_context,
                coverage_by_date=relative.coverage_by_date,
            ),
            ValuationIndustryEvaluationConfig(valuation_feature_count=1),
        )
        output[name] = report["paired_vs_base"]
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
