#!/usr/bin/env python3
"""Evaluate dated reverse-DCF features and official industry history together."""

from __future__ import annotations

import argparse
import html
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

VALUATION_COLUMNS = (
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
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--maximum-label-age-days", type=int, default=366)
    parser.add_argument("--minimum-date-coverage", type=float, default=0.70)
    parser.add_argument("--minimum-train-dates", type=int, default=2)
    parser.add_argument("--minimum-train-rows", type=int, default=100)
    args = parser.parse_args()

    raw = QuarterlyPricingDatasetBuilder(args.data_root, market=args.market).build()
    raw.validate()
    frame = pd.read_csv(args.valuation_csv)
    required = {"symbol", "feature_date", "status", *VALUATION_COLUMNS}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"valuation file missing columns: {sorted(missing)}")
    frame["feature_date"] = pd.to_datetime(frame["feature_date"], errors="coerce").dt.date
    if frame[["symbol", "feature_date"]].isna().any().any():
        raise ValueError("valuation file contains invalid symbol/date")
    key_counts = frame.groupby(["symbol", "feature_date"]).size()
    if (key_counts > 1).any():
        raise ValueError("valuation file contains duplicate symbol/date keys")
    lookup = {
        (str(row.symbol), row.feature_date): row
        for row in frame.itertuples(index=False)
    }
    values = np.zeros((len(raw.symbols), len(VALUATION_COLUMNS)), dtype=np.float64)
    mask = np.zeros_like(values, dtype=bool)
    stats = {"source_rows": len(frame), "matched_rows": 0, "solved_rows": 0,
             "feature_values": 0}
    for index, (feature_date, symbol) in enumerate(zip(raw.feature_dates, raw.symbols)):
        row = lookup.get((str(symbol), feature_date))
        if row is None:
            continue
        stats["matched_rows"] += 1
        if str(getattr(row, "status", "")) != "solved":
            continue
        stats["solved_rows"] += 1
        for column_index, name in enumerate(VALUATION_COLUMNS):
            value = pd.to_numeric(getattr(row, name), errors="coerce")
            if pd.notna(value) and np.isfinite(float(value)):
                values[index, column_index] = float(value)
                mask[index, column_index] = True
                stats["feature_values"] += 1

    augmented = FundamentalPricingDataset(
        feature_dates=raw.feature_dates,
        label_end_dates=raw.label_end_dates,
        symbols=raw.symbols,
        feature_names=tuple(raw.feature_names) + tuple(
            f"valuation:{name}" for name in VALUATION_COLUMNS
        ),
        values=np.concatenate([raw.values, values], axis=1),
        availability_mask=np.concatenate([raw.availability_mask, mask], axis=1),
        forward_returns=raw.forward_returns,
        excess_returns=raw.excess_returns,
        metadata={**raw.metadata, "contract": "historical-valuation-fundamental-dataset-1",
                  "valuation_features_are_point_in_time": True},
    ).validate()
    config = ValuationIndustryEvaluationConfig(
        maximum_label_age_days=args.maximum_label_age_days,
        minimum_date_coverage=args.minimum_date_coverage,
        minimum_train_dates=args.minimum_train_dates,
        minimum_train_rows=args.minimum_train_rows,
        base_feature_count=len(raw.feature_names),
        valuation_feature_count=len(VALUATION_COLUMNS),
    )
    relative = build_industry_relative_dataset(
        augmented, IndustryClassificationHistoryStore(args.industry_history), config
    )
    report = evaluate_valuation_industry_increment(relative, config)
    report["data"] = {
        "data_root": str(Path(args.data_root).resolve()),
        "valuation_csv": str(Path(args.valuation_csv).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "base_row_count": len(raw.symbols),
        "base_feature_count": len(raw.feature_names),
        "valuation_feature_count": len(VALUATION_COLUMNS),
        "valuation_join": stats,
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    quarters = pd.DataFrame(report["quarterly_results"])
    quarters.to_csv(output / "quarterly_results.csv", index=False, encoding="utf-8")
    document = f"""<!doctype html><meta charset='utf-8'>
<title>估值与行业嵌套增量评估</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:24px;color:#172b4d}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #d0d5dd;padding:5px}}th{{background:#f2f4f7}}pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}</style>
<h1>基本面 × 反向DCF/CAPM × 行业相对：严格时点嵌套评估</h1>
<p>四组模型在同一发布日期约束、同一行业覆盖和同一未来收益标签上比较；结果仅用于研究，不自动进入搜参。</p>
<pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre>
{quarters.to_html(index=False, escape=True)}"""
    (output / "report.html").write_text(document, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
