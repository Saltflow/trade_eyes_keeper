#!/usr/bin/env python3
"""Evaluate dated industry-relative fundamentals against raw fundamentals."""

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

from src.fundamental_embedding.dataset import (  # noqa: E402
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry_evaluation import (  # noqa: E402
    IndustryRidgeConfig,
    build_industry_relative_dataset,
    evaluate_industry_relative_increment,
)
from src.fundamental_embedding.industry_history import (  # noqa: E402
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
    args = parser.parse_args()

    config = IndustryRidgeConfig(
        maximum_label_age_days=args.maximum_label_age_days,
        minimum_date_coverage=args.minimum_date_coverage,
        minimum_train_dates=args.minimum_train_dates,
        minimum_train_rows=args.minimum_train_rows,
    )
    dataset = QuarterlyPricingDatasetBuilder(
        args.data_root, market=args.market
    ).build()
    relative = build_industry_relative_dataset(
        dataset,
        IndustryClassificationHistoryStore(args.industry_history),
        config,
    )
    report = evaluate_industry_relative_increment(relative, config)
    report["data"] = {
        "data_root": str(Path(args.data_root).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "base_row_count": len(dataset.symbols),
        "base_feature_count": len(dataset.feature_names),
        "augmented_feature_count": len(relative.dataset.feature_names),
    }
    report["configuration"] = asdict(config)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    coverage = pd.DataFrame(report["coverage_by_date"])
    quarters = pd.DataFrame(report["quarterly_results"])
    coverage.to_csv(output / "coverage_by_date.csv", index=False, encoding="utf-8")
    quarters.to_csv(output / "quarterly_results.csv", index=False, encoding="utf-8")
    document = f"""<!doctype html><meta charset='utf-8'>
<title>行业相对基本面增量评估</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:24px;color:#172b4d}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:22px}}th,td{{border:1px solid #d0d5dd;padding:5px}}th{{background:#f2f4f7}}pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}</style>
<h1>行业相对基本面：严格时点增量评估</h1>
<p>原始基本面与“原始+同业相对值”在相同标的、相同训练窗比较。行业标签取证监会公告日生效；超过账龄上限时不使用。</p>
<pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre>
<h2>分类覆盖</h2>{coverage.to_html(index=False, escape=True)}
<h2>季度结果</h2>{quarters.to_html(index=False, escape=True)}"""
    (output / "report.html").write_text(document, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
