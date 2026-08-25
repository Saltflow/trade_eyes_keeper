#!/usr/bin/env python3
"""Walk-forward evaluation of the training-only valuation context gate."""

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

from src.fundamental_embedding.api import FundamentalPricingDataset
from src.fundamental_embedding.causal_context_gate import (
    CausalContextGate,
    CausalContextGateConfig,
)
from src.fundamental_embedding.dataset import QuarterlyPricingDatasetBuilder
from src.fundamental_embedding.industry_evaluation import (
    IndustryRidgeConfig,
    _rank_ic,
    _top_bottom_spread,
    build_industry_relative_dataset,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.fundamental_embedding.valuation_context import (
    build_historical_valuation_context,
)

COLUMNS = (
    "beta",
    "capm_cost_of_equity",
    "market_implied_growth_5y",
    "market_vs_fundamental_growth",
    "fundamental_growth",
)


def _summary(rows: list[dict[str, object]], name: str) -> dict[str, object]:
    by_date: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_date.setdefault(str(row["feature_date"]), []).append(row)
    ics, spreads = [], []
    quarterly = {}
    for key, items in sorted(by_date.items()):
        score = np.asarray([item[name] for item in items], dtype=float)
        target = np.asarray([item["target"] for item in items], dtype=float)
        value = _rank_ic(score, target)
        quarterly[key] = value
        if value is not None:
            ics.append(value)
        spread = _top_bottom_spread(score, target, 0.2)
        if spread is not None:
            spreads.append(spread)
    return {
        "quarter_count": len(ics),
        "mean_rank_ic": float(np.mean(ics)) if ics else None,
        "positive_quarters": int(sum(value > 0 for value in ics)),
        "mean_top_bottom_spread": float(np.mean(spreads)) if spreads else None,
        "quarterly_rank_ic": quarterly,
    }


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
    relative = build_industry_relative_dataset(
        augmented,
        IndustryClassificationHistoryStore(args.industry_history),
        IndustryRidgeConfig(minimum_train_dates=2, minimum_train_rows=100),
    )
    context = build_historical_valuation_context(relative)
    dates = np.asarray(context.feature_dates, dtype=object)
    label_ends = np.asarray(context.label_end_dates, dtype=object)
    base_count = len(raw.feature_names)
    eligible = {
        date.fromisoformat(item["feature_date"])
        for item in relative.coverage_by_date
        if item["eligible_for_industry_evaluation"]
    }
    rows: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    for test_date in sorted(eligible):
        test = (dates == test_date) & relative.peer_context
        train = (label_ends < test_date) & relative.peer_context & np.isin(dates, list(eligible))
        train_dates = sorted(set(dates[train]))
        if int(test.sum()) < 3 or len(train_dates) < 2 or int(train.sum()) < 100:
            continue
        gate = CausalContextGate(CausalContextGateConfig()).fit(
            context.values[train, :base_count],
            context.availability_mask[train, :base_count],
            context.values[train, base_count:],
            context.availability_mask[train, base_count:],
            context.excess_returns[train],
            dates[train],
        )
        prediction = gate.predict(
            context.values[test, :base_count],
            context.availability_mask[test, :base_count],
            context.values[test, base_count:],
            context.availability_mask[test, base_count:],
        )
        target = context.excess_returns[test]
        rows.extend({
            "feature_date": test_date.isoformat(),
            "target": float(target[index]),
            "fundamental": float(prediction["fundamental"][index]),
            "valuation_context": float(prediction["valuation_context"][index]),
            "gated": float(prediction["gated"][index]),
        } for index in range(len(target)))
        diagnostics.append({
            "feature_date": test_date.isoformat(),
            **gate.diagnostics(),
        })
    report = {
        "contract": "causal-fundamental-context-gate-1",
        "configuration": CausalContextGateConfig().__dict__,
        "summaries": {
            name: _summary(rows, name)
            for name in ("fundamental", "valuation_context", "gated")
        },
        "gate_diagnostics": diagnostics,
        "data": {
            "row_count": len(raw.symbols),
            "symbol_count": len(set(raw.symbols)),
            "evaluated_quarters": len(diagnostics),
            "base_feature_count": base_count,
            "context_feature_count": len(context.feature_names) - base_count,
        },
        "acceptance": {
            "gate_weights_fit_only_on_training_dates": True,
            "test_and_holdout_hidden": True,
            "published_at_before_feature_date": True,
            "research_only": True,
        },
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(output / "predictions.csv", index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
