#!/usr/bin/env python3
"""Build a current all-instrument reverse-DCF consensus classification."""

from __future__ import annotations

import argparse
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

from scripts.diagnose_market_cost_expert_coverage import (
    _load_benchmark,
)
from src.fundamental_embedding.intrinsic_value import (
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
)
from src.fundamental_embedding.reverse_dcf_consensus import (
    build_reverse_dcf_consensus,
)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _bucket(value: float | None) -> str:
    if value is None:
        return "unresolved"
    if value < 0:
        return "priced_decline"
    if value < 0.05:
        return "priced_growth_0_5pct"
    if value < 0.10:
        return "priced_growth_5_10pct"
    if value < 0.20:
        return "priced_growth_10_20pct"
    if value < 0.30:
        return "priced_growth_20_30pct"
    return "priced_growth_30pct_plus"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--market", default="a_share")
    args = parser.parse_args()

    config = IntrinsicValueConfig(
        market_cost_of_equity_floor=False,
        terminal_growth=0.02,
    )
    builder = PointInTimeValuationBuilder(
        args.data_root,
        market=args.market,
        benchmark_bundle=_load_benchmark(Path(args.benchmark_prices)),
    )
    engine = IntrinsicValueEngine(config)
    anchor = builder.latest_date()
    rows: list[dict[str, Any]] = []
    for symbol in builder.available_symbols():
        snapshot = builder.snapshot(symbol, anchor, config)
        if snapshot is None:
            continue
        estimate = engine.estimate(snapshot)
        consensus = build_reverse_dcf_consensus(estimate, snapshot, config)
        dominant = max(estimate.gate, key=estimate.gate.get) if estimate.gate else None
        dominant_candidate = next(
            item for item in consensus.candidates if item.expert_id == dominant
        ) if dominant else None
        implied = consensus.implied_growth
        fundamental = _finite(snapshot.growth)
        capital = snapshot.capital_cost
        rows.append(
            {
                "symbol": str(symbol),
                "evaluation_date": anchor.isoformat(),
                "current_price": float(snapshot.current_price),
                "reverse_dcf_status": consensus.status,
                "market_implied_growth_5y": implied,
                "market_implied_growth_low_5y": consensus.lower_growth,
                "market_implied_growth_high_5y": consensus.upper_growth,
                "market_implied_growth_dispersion": consensus.dispersion,
                "market_implied_growth_expert_count": consensus.candidate_count,
                "market_implied_growth_bucket": _bucket(implied),
                "fundamental_growth": fundamental,
                "market_vs_fundamental_growth": (
                    implied - fundamental
                    if implied is not None and fundamental is not None
                    else None
                ),
                "dominant_expert": dominant,
                "dominant_expert_growth": (
                    dominant_candidate.implied_growth
                    if dominant_candidate is not None
                    else None
                ),
                "beta": (
                    _finite(capital.adjusted_beta)
                    if capital is not None
                    else None
                ),
                "capm_cost_of_equity": (
                    _finite(
                        estimate.required_return_policy.get(
                            "market_cost_of_equity", {}
                        ).get("base")
                    )
                ),
                "expert_candidates": ";".join(
                    f"{item.expert_id}:{item.implied_growth:.6f}"
                    for item in consensus.candidates
                    if item.implied_growth is not None
                ),
            }
        )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(output / "reverse_dcf_consensus_all.csv", index=False, encoding="utf-8")
    solved = frame[frame["market_implied_growth_5y"].notna()]
    report = {
        "contract": "market-cost-reverse-dcf-consensus-cross-section-1",
        "data_root": str(Path(args.data_root).resolve()),
        "benchmark_prices": str(Path(args.benchmark_prices).resolve()),
        "market": args.market,
        "evaluation_date": anchor.isoformat(),
        "input_snapshot_count": len(frame),
        "consensus_solved_count": len(solved),
        "consensus_count": int((frame["reverse_dcf_status"] == "solved_consensus").sum()),
        "single_expert_count": int((frame["reverse_dcf_status"] == "solved_single_expert").sum()),
        "unresolved_count": int((frame["reverse_dcf_status"] == "unresolved").sum()),
        "growth_bucket_counts": frame["market_implied_growth_bucket"].value_counts().to_dict(),
        "high_dispersion_over_5pct": int(
            (pd.to_numeric(frame["market_implied_growth_dispersion"], errors="coerce") > 0.05).sum()
        ),
        "mean_expert_count_solved": (
            float(solved["market_implied_growth_expert_count"].mean())
            if not solved.empty
            else 0.0
        ),
        "point_in_time": True,
        "capm_is_equity_cost_for_fcfe_proxy": True,
        "reverse_dcf_diagnostic_only": True,
        "expert_selection_policy": "available_positive_equity_cash_flow_candidates_weighted_median",
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    display = frame.copy()
    html_table = display.sort_values(
        ["market_implied_growth_bucket", "market_implied_growth_5y"],
        na_position="last",
    ).to_html(index=False, escape=True)
    (output / "report.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>多专家反向DCF共识</title>"
        "<style>body{font-family:Arial,'Microsoft YaHei';max-width:1600px;margin:24px auto;"
        "padding:0 16px;color:#172b4d}table{border-collapse:collapse;width:100%;font-size:11px}"
        "th,td{border:1px solid #d0d5dd;padding:4px}th{background:#f2f4f7}pre{white-space:pre-wrap;"
        "background:#f7f8fa;padding:12px}</style><h1>667标的多专家反向 DCF 共识</h1>"
        f"<p>CAPM 只作为股权成本；不构造 WACC，不把隐含增长回灌公允价值。</p>"
        f"<pre>{html.escape(json.dumps(report,ensure_ascii=False,indent=2))}</pre>"
        f"{html_table}",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
