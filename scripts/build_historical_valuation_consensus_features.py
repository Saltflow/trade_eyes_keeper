#!/usr/bin/env python3
"""Build dated multi-expert reverse-DCF features for OOS research."""

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

from scripts.diagnose_market_cost_expert_coverage import _load_benchmark
from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.intrinsic_value import (
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
)
from src.fundamental_embedding.reverse_dcf_consensus import (
    build_reverse_dcf_consensus,
)


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--growth-low", type=float, default=-0.50)
    parser.add_argument("--growth-high", type=float, default=1.00)
    parser.add_argument("--progress-every", type=int, default=250)
    args = parser.parse_args()
    if args.growth_low >= args.growth_high:
        raise ValueError("growth-low must be below growth-high")

    dataset = QuarterlyPricingDatasetBuilder(
        args.data_root, market=args.market
    ).build()
    config = IntrinsicValueConfig(
        market_cost_of_equity_floor=False,
        terminal_growth=0.02,
    )
    valuation_builder = PointInTimeValuationBuilder(
        args.data_root,
        market=args.market,
        benchmark_bundle=_load_benchmark(Path(args.benchmark_prices)),
    )
    engine = IntrinsicValueEngine(config)
    rows: list[dict[str, object]] = []
    total = len(dataset.symbols)
    for index, (feature_date, symbol) in enumerate(
        zip(dataset.feature_dates, dataset.symbols), start=1
    ):
        snapshot = valuation_builder.snapshot(symbol, feature_date, config)
        if snapshot is None:
            continue
        estimate = engine.estimate(snapshot)
        consensus = build_reverse_dcf_consensus(
            estimate,
            snapshot,
            config,
            growth_low=args.growth_low,
            growth_high=args.growth_high,
        )
        dominant = max(estimate.gate, key=estimate.gate.get) if estimate.gate else None
        dominant_candidate = next(
            (item for item in consensus.candidates if item.expert_id == dominant),
            None,
        )
        implied = consensus.implied_growth
        fundamental = _finite(snapshot.growth)
        market_cost = _finite(
            estimate.required_return_policy
            .get("market_cost_of_equity", {})
            .get("base")
        )
        beta = None
        beta_source = "missing"
        if snapshot.capital_cost is not None:
            for candidate, source in (
                (snapshot.capital_cost.adjusted_beta, "adjusted_beta"),
                (snapshot.capital_cost.raw_beta, "raw_beta"),
            ):
                if candidate is not None and np.isfinite(candidate):
                    beta = float(candidate)
                    beta_source = source
                    break
        rows.append(
            {
                "symbol": str(symbol),
                "feature_date": feature_date.isoformat(),
                "market_date": snapshot.market_date.isoformat(),
                "current_price": float(snapshot.current_price),
                "dominant_expert": dominant,
                "cash_per_share": (
                    dominant_candidate.cash_per_share
                    if dominant_candidate is not None
                    else None
                ),
                "market_implied_growth_5y": implied,
                "market_implied_growth_low_5y": consensus.lower_growth,
                "market_implied_growth_high_5y": consensus.upper_growth,
                "market_implied_growth_dispersion": consensus.dispersion,
                "market_implied_growth_expert_count": consensus.candidate_count,
                "market_implied_growth_status": consensus.status,
                "market_implied_growth_candidates": json.dumps(
                    [item.to_dict() for item in consensus.candidates],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "fundamental_growth": fundamental,
                "market_vs_fundamental_growth": (
                    implied - fundamental
                    if implied is not None and fundamental is not None
                    else None
                ),
                "beta": beta,
                "beta_source": beta_source,
                "capm_cost_of_equity": market_cost,
                "terminal_growth": (
                    dominant_candidate.terminal_growth
                    if dominant_candidate is not None
                    else None
                ),
                "financial_age_days": snapshot.financial_age_days,
                "status": "solved" if implied is not None else "unresolved",
                "market_cost_growth_status": (
                    "solved" if implied is not None else "unresolved"
                ),
            }
        )
        if index % max(1, args.progress_every) == 0:
            print(
                json.dumps(
                    {"processed": index, "total": total, "rows": len(rows)}
                ),
                flush=True,
            )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(output, index=False, encoding="utf-8")
    summary = {
        "contract": "historical-reverse-dcf-capm-consensus-features-1",
        "data_root": str(Path(args.data_root).resolve()),
        "benchmark_prices": str(Path(args.benchmark_prices).resolve()),
        "market": args.market,
        "valuation_config": asdict(config),
        "reverse_growth_range": [args.growth_low, args.growth_high],
        "dataset_rows": total,
        "output_rows": len(frame),
        "solved_rows": int(frame["status"].eq("solved").sum()),
        "consensus_rows": int(
            frame["market_implied_growth_status"].eq("solved_consensus").sum()
        ),
        "single_expert_rows": int(
            frame["market_implied_growth_status"].eq("solved_single_expert").sum()
        ),
        "unresolved_rows": int(frame["status"].eq("unresolved").sum()),
        "actual_beta_rows": int(frame["beta_source"].ne("missing").sum()),
        "feature_date_count": int(frame["feature_date"].nunique()),
        "high_dispersion_rows": int(
            pd.to_numeric(
                frame["market_implied_growth_dispersion"], errors="coerce"
            ).gt(0.05).sum()
        ),
        "no_lookahead": True,
        "capm_is_equity_cost_for_fcfe_proxy": True,
        "reverse_dcf_diagnostic_only": True,
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
