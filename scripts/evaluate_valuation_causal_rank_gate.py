#!/usr/bin/env python3
"""Evaluate a training-only Rank-IC gate for valuation versus base experts."""

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
from src.fundamental_embedding.causal_context_gate import (
    CausalContextGateConfig,
)
from src.fundamental_embedding.causal_rank_context_gate import (
    CausalRankContextGate,
)
from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.industry_evaluation import (
    _rank_ic,
    _top_bottom_spread,
    build_industry_relative_dataset,
)
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.fundamental_embedding.valuation_industry_evaluation import (
    ValuationIndustryEvaluationConfig,
)


def _slice(
    values: np.ndarray, masks: np.ndarray, indices: list[int]
) -> tuple[np.ndarray, np.ndarray]:
    return values[:, indices], masks[:, indices]


def _summary(rows: list[dict[str, object]], name: str) -> dict[str, object]:
    ics = [
        row[f"{name}_rank_ic"]
        for row in rows
        if row[f"{name}_rank_ic"] is not None
    ]
    spreads = [
        row[f"{name}_spread"]
        for row in rows
        if row[f"{name}_spread"] is not None
    ]
    return {
        "quarter_count": len(ics),
        "mean_rank_ic": float(np.mean(ics)) if ics else None,
        "positive_quarters": int(sum(value > 0 for value in ics)),
        "mean_spread": float(np.mean(spreads)) if spreads else None,
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
    frame["feature_date"] = pd.to_datetime(
        frame["feature_date"], errors="coerce"
    ).dt.date
    history = IndustryClassificationHistoryStore(args.industry_history)
    ridge_config = ValuationIndustryEvaluationConfig(
        maximum_label_age_days=366,
        minimum_date_coverage=0.70,
        minimum_train_dates=2,
        minimum_train_rows=100,
        base_feature_count=len(raw.feature_names),
        valuation_feature_count=len(VALUATION_COLUMNS),
    )
    gate_config = CausalContextGateConfig(
        ridge_alpha=8.0,
        gate_temperature=0.35,
        gate_floor=0.05,
        validation_fraction=0.25,
        minimum_validation_dates=2,
    )
    results: dict[str, object] = {}
    for policy in ("consensus_only", "consensus_low_dispersion_10pct"):
        augmented, join = _build_augmented(raw, frame, policy, market_only=False)
        relative = build_industry_relative_dataset(augmented, history, ridge_config)
        dataset = relative.dataset
        dates = np.asarray(dataset.feature_dates, dtype=object)
        label_ends = np.asarray(dataset.label_end_dates, dtype=object)
        base_count = len(raw.feature_names)
        valuation_end = base_count + len(VALUATION_COLUMNS)
        industry_base_start = valuation_end
        industry_valuation_start = valuation_end + base_count
        full_end = len(dataset.feature_names)
        base_indices = list(range(base_count)) + list(
            range(industry_base_start, industry_valuation_start)
        )
        context_indices = list(range(base_count, valuation_end)) + list(
            range(industry_valuation_start, full_end)
        )
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
                or len(train_dates) < ridge_config.minimum_train_dates
                or int(train.sum()) < ridge_config.minimum_train_rows
            ):
                continue
            values = dataset.values
            masks = dataset.availability_mask
            base_train, base_train_mask = _slice(
                values[train], masks[train], base_indices
            )
            context_train, context_train_mask = _slice(
                values[train], masks[train], context_indices
            )
            base_test, base_test_mask = _slice(
                values[test], masks[test], base_indices
            )
            context_test, context_test_mask = _slice(
                values[test], masks[test], context_indices
            )
            gate = CausalRankContextGate(gate_config).fit(
                base_train,
                base_train_mask,
                context_train,
                context_train_mask,
                dataset.excess_returns[train].astype(np.float64),
                dates[train],
            )
            prediction = gate.predict(
                base_test,
                base_test_mask,
                context_test,
                context_test_mask,
            )
            target = dataset.excess_returns[test].astype(np.float64)
            rows.append(
                {
                    "feature_date": test_date.isoformat(),
                    "train_row_count": int(train.sum()),
                    "test_row_count": int(test.sum()),
                    "base_rank_ic": _rank_ic(prediction["fundamental"], target),
                    "context_rank_ic": _rank_ic(
                        prediction["valuation_context"], target
                    ),
                    "gated_rank_ic": _rank_ic(prediction["gated"], target),
                    "base_spread": _top_bottom_spread(
                        prediction["fundamental"],
                        target,
                        ridge_config.top_bottom_fraction,
                    ),
                    "context_spread": _top_bottom_spread(
                        prediction["valuation_context"],
                        target,
                        ridge_config.top_bottom_fraction,
                    ),
                    "gated_spread": _top_bottom_spread(
                        prediction["gated"],
                        target,
                        ridge_config.top_bottom_fraction,
                    ),
                    "base_gate_weight": float(gate.gate_weights[0]),
                    "valuation_gate_weight": float(gate.gate_weights[1]),
                    "gate_validation_dates": list(gate.gate_training_dates),
                }
            )
        results[policy] = {
            "join": join,
            "summary": {
                "base": _summary(rows, "base"),
                "context": _summary(rows, "context"),
                "gated": _summary(rows, "gated"),
            },
            "mean_gate_weights": {
                "base": (
                    float(np.mean([row["base_gate_weight"] for row in rows]))
                    if rows
                    else None
                ),
                "valuation": (
                    float(np.mean([row["valuation_gate_weight"] for row in rows]))
                    if rows
                    else None
                ),
            },
            "quarterly_results": rows,
        }

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "contract": "valuation-consensus-causal-rank-gate-1",
        "data_root": str(Path(args.data_root).resolve()),
        "valuation_csv": str(Path(args.valuation_csv).resolve()),
        "industry_history": str(Path(args.industry_history).resolve()),
        "gate_configuration": {
            "ridge_alpha": gate_config.ridge_alpha,
            "gate_temperature": gate_config.gate_temperature,
            "gate_floor": gate_config.gate_floor,
            "validation_fraction": gate_config.validation_fraction,
            "minimum_validation_dates": gate_config.minimum_validation_dates,
            "objective": "training-only validation-period cross-sectional Rank IC",
        },
        "results": results,
        "acceptance": {
            "gate_fit_uses_only_rows_before_test_date": True,
            "gate_weights_frozen_before_test": True,
            "published_at_before_feature_date": True,
            "training_labels_end_before_test_date": True,
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
