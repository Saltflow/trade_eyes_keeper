#!/usr/bin/env python3
"""Evaluate fixed cross-sectional blends of company and industry scores."""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry_blend_evaluation import (
    IndustryBlendConfig,
    evaluate_industry_rank_blends,
)
from src.fundamental_embedding.industry_evaluation import (
    build_industry_relative_dataset,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--industry-history", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--maximum-label-age-days", type=int, default=366)
    parser.add_argument("--minimum-date-coverage", type=float, default=0.70)
    parser.add_argument("--minimum-train-dates", type=int, default=2)
    parser.add_argument("--minimum-train-rows", type=int, default=100)
    parser.add_argument(
        "--weights",
        default="0,0.1,0.2,0.3,0.5,0.75,1",
        help="fixed industry-rank weights, selected before evaluation",
    )
    args = parser.parse_args()
    weights = tuple(float(item) for item in args.weights.split(",") if item.strip())
    config = IndustryBlendConfig(
        maximum_label_age_days=args.maximum_label_age_days,
        minimum_date_coverage=args.minimum_date_coverage,
        minimum_train_dates=args.minimum_train_dates,
        minimum_train_rows=args.minimum_train_rows,
        weights=weights,
    )
    raw = QuarterlyPricingDatasetBuilder(args.data_root, market=args.market).build()
    relative = build_industry_relative_dataset(
        raw, IndustryClassificationHistoryStore(args.industry_history), config
    )
    report = evaluate_industry_rank_blends(relative, config)
    report["configuration"] = asdict(config)
    report["data"] = {
        "data_root": str(Path(args.data_root).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "base_row_count": len(raw.symbols),
        "base_feature_count": len(raw.feature_names),
        "industry_factor_count": 4,
    }
    report["coverage_by_date"] = list(relative.coverage_by_date)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    quarters = pd.DataFrame(report["quarterly_results"])
    quarters.to_csv(output / "quarterly_results.csv", index=False, encoding="utf-8")
    document = f"""<!doctype html><meta charset='utf-8'>
<title>行业 rank blend 评估</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:24px;color:#172b4d}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #d0d5dd;padding:5px}}th{{background:#f2f4f7}}pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}</style>
<h1>公司基本面 × 行业相对 rank blend</h1>
<p>权重在评估前固定；行业分类只使用发布日期不晚于特征日的官方历史。</p>
<pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre>
{quarters.to_html(index=False, escape=True)}"""
    (output / "report.html").write_text(document, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
