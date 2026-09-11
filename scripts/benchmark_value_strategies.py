#!/usr/bin/env python3
"""Benchmark the opt-in CAPM/value strategies on one market.

This is intentionally separate from ``benchmark_technical_strategies.py``:
value strategies need an explicit, historical context enricher and must never
fall back to an empty or current-fundamental panel.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config
from src.experiments.strategy_benchmark import (
    prepare_benchmark_data,
    run_market_benchmark_from_snapshot,
    summarize_prepared_data,
    write_benchmark_artifacts,
)
from src.fundamental_embedding.dataset import QuarterlyPricingDatasetBuilder
from src.fundamental_embedding.industry_history import (
    IndustryClassificationHistoryStore,
)
from src.fundamental_embedding.valuation_consensus_context import (
    ValuationConsensusQualityConfig,
    build_historical_consensus_context,
)
from src.strategy import get_strategy
from src.strategy.context_enrichment import make_consensus_context_enricher

DEFAULT_DATA_ROOT = (
    ROOT
    / "data"
    / "reference_universe"
    / "point_in_time_20260818"
    / "partial_first_pass_20260822"
    / "dataset"
)
DEFAULT_VALUATION_CSV = (
    ROOT / "data" / "analysis" / "historical_valuation_consensus_features_667_20260822.csv"
)
DEFAULT_INDUSTRY_HISTORY = (
    ROOT
    / "data"
    / "reference_universe"
    / "industry_classification_history_official_2020q4_2025h2.json"
)
DEFAULT_CAPM_V2_POLICY = (
    ROOT
    / "data"
    / "analysis"
    / "capm_dcf_value_policy_20260825_execution_v2"
    / "report.json"
)
DEFAULT_CAPM_V1_POLICY = (
    ROOT / "data" / "analysis" / "capm_dcf_value_policy_20260825" / "report.json"
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_valuation_enricher(
    data_root: Path,
    valuation_csv: Path,
    industry_history: Path,
):
    raw = QuarterlyPricingDatasetBuilder(data_root, market="a_share").build()
    frame = pd.read_csv(valuation_csv)
    frame["feature_date"] = pd.to_datetime(
        frame["feature_date"], errors="coerce"
    ).dt.date
    if frame["feature_date"].isna().any():
        raise ValueError("valuation CSV contains invalid feature dates")
    context = build_historical_consensus_context(
        raw,
        frame.to_dict("records"),
        IndustryClassificationHistoryStore(industry_history),
        quality=ValuationConsensusQualityConfig(
            minimum_expert_count=2,
            maximum_dispersion=0.10,
            require_consensus=True,
        ),
    )
    strategy = get_strategy("valuation_aware_ensemble")
    if strategy is None:
        raise RuntimeError("valuation_aware_ensemble is not registered")
    enricher = make_consensus_context_enricher(
        context,
        required_feature_names=tuple(strategy.fundamental_feature_dependencies),
    )
    required = tuple(strategy.fundamental_feature_dependencies)
    names = {str(name) for name in context.feature_names}
    indices = [context.feature_names.index(name) for name in required]
    fully_observed = np.all(context.availability_mask[:, indices], axis=1)
    context_dates = np.asarray(context.feature_dates, dtype=object)[fully_observed]
    return enricher, {
        "contract": context.metadata.get("contract"),
        "contract_hash": enricher.contract_hash,
        "rows": len(context.symbols),
        "symbols": len(set(context.symbols)),
        "feature_dates": [
            str(min(context.feature_dates)), str(max(context.feature_dates))
        ],
        "fully_observed_rows": int(fully_observed.sum()),
        "fully_observed_dates": (
            [str(min(context_dates)), str(max(context_dates))]
            if len(context_dates)
            else []
        ),
        "required_features_present": set(required) <= names,
        "matched_valuation_rows": context.metadata.get("matched_valuation_rows"),
        "gated_valuation_rows": context.metadata.get("gated_valuation_rows"),
        "missing_values_are_not_median_imputed": context.metadata.get(
            "missing_values_are_not_median_imputed"
        ),
    }


def _capm_readiness(base_config: dict, codes: tuple[str, ...]) -> dict[str, object]:
    """Explain why a frozen CAPM policy can or cannot enter this benchmark."""

    results = []
    for policy_path in (DEFAULT_CAPM_V2_POLICY, DEFAULT_CAPM_V1_POLICY):
        if not policy_path.is_file():
            continue
        report = _json(policy_path)
        dataset = report.get("dataset") or {}
        benchmark = report.get("benchmark") or {}
        industry = report.get("industry_history") or {}
        config = copy.deepcopy(base_config)
        optimizer = dict(config.get("optimizer") or {})
        optimizer["capm_dcf_value"] = {
            "equity_risk_premium": 0.06,
            "beta_margin_gamma": 0.32,
            "minimum_entry_fraction": 0.75,
            "maximum_entry_fraction": 0.95,
            "maximum_snapshot_age_days": 550,
            "markets": {
                "a_share": {
                    "data_root": dataset.get("root"),
                    "market_currency": "CNY",
                    "benchmark_prices": benchmark.get("path")
                    or str(ROOT / "cache" / "data" / "510300.csv"),
                    "industry_history": industry.get("path")
                    or str(DEFAULT_INDUSTRY_HISTORY),
                    "risk_free_rates_json": str(
                        policy_path.with_name("risk_free_rates.json")
                    ),
                    "frozen_policy_report": str(policy_path),
                }
            },
        }
        config["optimizer"] = optimizer
        try:
            strategy = get_strategy("capm_dcf_value")
            context = strategy.make_context_enricher(
                config, market="a_share", symbols=codes
            )
            results.append(
                {
                    "policy": str(policy_path),
                    "status": "ready",
                    "contract": report.get("contract"),
                    "context_snapshots": len(context.snapshots),
                }
            )
        except Exception as exc:  # noqa: BLE001
            validation = report.get("validation") or {}
            metrics = validation.get("metrics") or {}
            results.append(
                {
                    "policy": str(policy_path),
                    "status": "rejected",
                    "contract": report.get("contract"),
                    "reason": str(exc),
                    "candidate_eligible": (report.get("acceptance") or {}).get(
                        "candidate_eligible_for_manual_strategy_experiment"
                    ),
                    "validation_summary": {
                        str(key): {
                            name: value
                            for name, value in item.items()
                            if name
                            in {
                                "eligible_count",
                                "hit_count",
                                "hit_rate",
                                "post_entry_success_rate",
                                "post_entry_success_wilson_lower_95",
                            }
                        }
                        for key, item in metrics.items()
                    },
                }
            )
    return {
        "status": "ready" if any(item["status"] == "ready" for item in results) else "blocked",
        "strategies": results,
        "note": (
            "CAPM was not run when no current-contract, holdout-passed frozen policy "
            "was available."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A-share benchmark for capm_dcf_value and valuation_aware_ensemble"
    )
    parser.add_argument("--depth", type=int, default=60000)
    parser.add_argument("--evaluation-workers", type=int, default=1)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--valuation-csv", type=Path, default=DEFAULT_VALUATION_CSV)
    parser.add_argument(
        "--industry-history", type=Path, default=DEFAULT_INDUSTRY_HISTORY
    )
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "data" / "analysis" / "value_strategy_benchmark"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.depth <= 0 or args.evaluation_workers <= 0:
        raise SystemExit("depth and evaluation-workers must be positive")
    config = load_config()
    prepared = prepare_benchmark_data(config, groups=("a_share",))
    snapshot = prepared["a_share"]
    codes = tuple(sorted(snapshot["stocks_data"]))
    readiness = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market": "a_share",
        "configured_codes": snapshot["configured_codes"],
        "evaluated_codes": list(codes),
        "data": summarize_prepared_data(prepared)["a_share"],
        "capm_dcf_value": _capm_readiness(config, codes),
    }
    enricher, valuation_readiness = _build_valuation_enricher(
        args.data_root.resolve(),
        args.valuation_csv.resolve(),
        args.industry_history.resolve(),
    )
    readiness["valuation_aware_ensemble"] = valuation_readiness
    output_dir = args.output_root / datetime.now().astimezone().strftime(
        "%Y%m%d_%H%M%S"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data_readiness.json").write_text(
        json.dumps(readiness, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(readiness, ensure_ascii=False, indent=2))

    started = monotonic()
    result = run_market_benchmark_from_snapshot(
        "valuation_aware_ensemble",
        "a_share",
        snapshot,
        args.depth,
        args.evaluation_workers,
        "random",
        context_enricher=enricher,
    )
    artifacts = write_benchmark_artifacts(
        output_dir=output_dir,
        market_results=[result],
        search_depth=args.depth,
        market_workers=1,
        evaluation_workers=args.evaluation_workers,
        wall_seconds=monotonic() - started,
        prefetch_summary={"a_share": readiness["data"]},
        artifact_stem="value_strategy_benchmark",
    )
    for name, path in artifacts.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
