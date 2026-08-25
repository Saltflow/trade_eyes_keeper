#!/usr/bin/env python3
"""Evaluate the rank/quality valuation context against raw fundamentals."""

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

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.dataset import QuarterlyPricingDatasetBuilder
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
from src.fundamental_embedding.valuation_context import (
    ValuationContextConfig,
    build_historical_valuation_context,
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
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    raw = QuarterlyPricingDatasetBuilder(args.data_root, market="a_share").build()
    frame = pd.read_csv(args.valuation_csv)
    frame["feature_date"] = pd.to_datetime(frame["feature_date"]).dt.date
    lookup = {(str(row.symbol), row.feature_date): row for row in frame.itertuples(index=False)}
    valuation = np.zeros((len(raw.symbols), len(COLUMNS)), dtype=float)
    valuation_mask = np.zeros_like(valuation, dtype=bool)
    for row_index, (feature_date, symbol) in enumerate(zip(raw.feature_dates, raw.symbols)):
        row = lookup.get((str(symbol), feature_date))
        if row is None or str(getattr(row, "status", "")) != "solved":
            continue
        for column_index, name in enumerate(COLUMNS):
            value = pd.to_numeric(getattr(row, name), errors="coerce")
            if pd.notna(value) and np.isfinite(float(value)):
                valuation[row_index, column_index] = float(value)
                valuation_mask[row_index, column_index] = True
    augmented = FundamentalPricingDataset(
        feature_dates=raw.feature_dates,
        label_end_dates=raw.label_end_dates,
        symbols=raw.symbols,
        feature_names=tuple(raw.feature_names) + tuple(f"valuation:{name}" for name in COLUMNS),
        values=np.concatenate([raw.values, valuation], axis=1),
        availability_mask=np.concatenate([raw.availability_mask, valuation_mask], axis=1),
        forward_returns=raw.forward_returns,
        excess_returns=raw.excess_returns,
    ).validate()
    config = IndustryRidgeConfig(minimum_train_dates=2, minimum_train_rows=100)
    relative = build_industry_relative_dataset(
        augmented, IndustryClassificationHistoryStore(args.industry_history), config
    )
    context = build_historical_valuation_context(relative, ValuationContextConfig())
    dates = np.asarray(context.feature_dates, dtype=object)
    label_ends = np.asarray(context.label_end_dates, dtype=object)
    base_count = len(raw.feature_names)
    eligible = {
        date.fromisoformat(item["feature_date"])
        for item in relative.coverage_by_date
        if item["eligible_for_industry_evaluation"]
    }
    rows: list[dict[str, object]] = []
    for test_date in sorted(eligible):
        test = (dates == test_date) & relative.peer_context
        train = (label_ends < test_date) & relative.peer_context & np.isin(dates, list(eligible))
        train_dates = sorted(set(dates[train]))
        if int(test.sum()) < 3 or len(train_dates) < config.minimum_train_dates or int(train.sum()) < config.minimum_train_rows:
            continue
        target_train = context.excess_returns[train].astype(float)
        target_test = context.excess_returns[test].astype(float)
        base_score = _ridge_predict(
            context.values[train, :base_count],
            context.availability_mask[train, :base_count],
            target_train,
            context.values[test, :base_count],
            context.availability_mask[test, :base_count],
            context.feature_names[:base_count],
            config.ridge_alpha,
        )
        context_score = _ridge_predict(
            context.values[train], context.availability_mask[train], target_train,
            context.values[test], context.availability_mask[test],
            context.feature_names, config.ridge_alpha,
        )
        rows.append({
            "feature_date": test_date.isoformat(),
            "train_row_count": int(train.sum()),
            "test_row_count": int(test.sum()),
            "base_rank_ic": _rank_ic(base_score, target_test),
            "base_top_bottom_spread": _top_bottom_spread(base_score, target_test, config.top_bottom_fraction),
            "context_rank_ic": _rank_ic(context_score, target_test),
            "context_top_bottom_spread": _top_bottom_spread(context_score, target_test, config.top_bottom_fraction),
        })
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    context_rows = [
        {"base_rank_ic": row["base_rank_ic"], "base_top_bottom_spread": row["base_top_bottom_spread"], "context_rank_ic": row["context_rank_ic"], "context_top_bottom_spread": row["context_top_bottom_spread"]}
        for row in rows
    ]
    report = {
        "contract": "valuation-context-increment-1",
        "configuration": asdict(ValuationContextConfig()),
        "base": _summary(context_rows, "base"),
        "context": _summary(context_rows, "context"),
        "paired": {
            "rank_ic_delta": float(np.mean([row["context_rank_ic"] - row["base_rank_ic"] for row in rows if row["context_rank_ic"] is not None and row["base_rank_ic"] is not None])),
            "spread_delta": float(np.mean([row["context_top_bottom_spread"] - row["base_top_bottom_spread"] for row in rows if row["context_top_bottom_spread"] is not None and row["base_top_bottom_spread"] is not None])),
        },
        "data": {"row_count": len(raw.symbols), "symbol_count": len(set(raw.symbols)), "base_feature_count": base_count, "context_feature_count": len(context.feature_names) - base_count},
        "coverage_by_date": list(relative.coverage_by_date),
        "quarterly_results": rows,
        "acceptance": context.metadata,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(output / "quarterly_results.csv", index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
