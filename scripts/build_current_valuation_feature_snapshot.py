#!/usr/bin/env python3
"""Build a current, research-only valuation plus industry feature snapshot.

This command is intentionally not a strategy optimizer input.  The source
taxonomy is a current snapshot, so it produces a numerically useful live
research artifact while refusing to claim historical walk-forward eligibility.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fundamental_embedding.dataset import (  # noqa: E402
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry import (  # noqa: E402
    IndustryClassificationStore,
    IndustryRelativeSnapshotBuilder,
)
from src.fundamental_embedding.valuation_features import (  # noqa: E402
    CurrentValuationFeatureBuilder,
)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _frame(snapshot, industry) -> pd.DataFrame:
    values = snapshot.values.copy()
    values[~snapshot.availability_mask] = np.nan
    frame = pd.DataFrame(values, columns=snapshot.feature_names)
    frame.insert(0, "symbol", list(snapshot.symbols))
    frame.insert(1, "industry_code", list(industry.industry_codes))
    frame.insert(2, "peer_scope", list(industry.peer_scopes))
    frame.insert(3, "peer_count", industry.peer_counts)
    return frame


def _write_report(output: Path, snapshot, industry, frame: pd.DataFrame) -> dict[str, Any]:
    coverage = {
        name: int(snapshot.availability_mask[:, index].sum())
        for index, name in enumerate(snapshot.feature_names)
    }
    report = {
        "contract": snapshot.metadata["contract"],
        "feature_date": snapshot.feature_date.isoformat(),
        "symbol_count": len(snapshot.symbols),
        "industry_label_count": int(sum(item is not None for item in industry.industry_codes)),
        "industry_peer_count": int(sum(item == "industry" for item in industry.peer_scopes)),
        "sector_fallback_count": int(sum(item == "sector_fallback" for item in industry.peer_scopes)),
        "feature_coverage": coverage,
        "metadata": snapshot.metadata,
        "acceptance": {
            "current_snapshot_only": True,
            "historical_backtest_input": False,
            "current_taxonomy_not_backfilled": True,
            "valuation_date_equals_fundamental_snapshot_date": True,
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output / "valuation_industry_features.csv", index=False, encoding="utf-8")
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    display = frame.copy()
    for name in snapshot.feature_names:
        display[name] = display[name].map(
            lambda value: "" if pd.isna(value) else f"{float(value):.2%}"
        )
    document = f"""<!doctype html><meta charset='utf-8'>
<title>当前行业估值特征</title>
<style>body{{font-family:Arial,'Microsoft YaHei',sans-serif;margin:24px;color:#172b4d}}
table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #d0d5dd;padding:5px}}th{{background:#f2f4f7}}pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}</style>
<h1>当前行业相对估值特征</h1>
<p>企业能力、市场隐含预期和行业相对预期分开保存。本产物只能用于当前研究，不能作为历史回测特征。</p>
<pre>{html.escape(json.dumps(report, ensure_ascii=False, indent=2))}</pre>
{display.to_html(index=False, escape=True)}"""
    (output / "report.html").write_text(document, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--industry-classifications", required=True)
    parser.add_argument("--reverse-dcf", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--market", default="a_share")
    args = parser.parse_args()

    fundamental = QuarterlyPricingDatasetBuilder(
        args.data_root, market=args.market
    ).build_latest()
    labels = IndustryClassificationStore(args.industry_classifications).labels_as_of(
        fundamental.feature_date, fundamental.symbols
    )
    industry = IndustryRelativeSnapshotBuilder().build(fundamental, labels)
    valuation = CurrentValuationFeatureBuilder().build(
        fundamental, industry, _read_rows(Path(args.reverse_dcf))
    )
    report = _write_report(
        Path(args.output_dir), valuation, industry, _frame(valuation, industry)
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
