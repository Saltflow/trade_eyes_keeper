#!/usr/bin/env python3
"""Compare CAPM reverse-DCF solvability across all valuation experts."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.market_history import PriceHistoryBundle
from src.fundamental_embedding.intrinsic_value import (
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
    _market_implied_growth,
)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _load_benchmark(path: Path) -> PriceHistoryBundle:
    raw = pd.read_csv(path)

    def column(name: str) -> pd.Series:
        for candidate in (name, f"raw_{name}", f"qfq_{name}"):
            if candidate in raw:
                return pd.to_numeric(raw[candidate], errors="coerce")
        raise ValueError(f"benchmark is missing {name} in {path}")

    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce"),
            "raw_open": column("open"),
            "raw_high": column("high"),
            "raw_low": column("low"),
            "raw_close": column("close"),
            "volume": pd.to_numeric(
                raw.get("volume", 0.0), errors="coerce"
            ).fillna(0.0),
        }
    ).dropna(subset=["date", "raw_close"])
    for name in ("open", "high", "low", "close"):
        frame[f"qfq_{name}"] = frame[f"raw_{name}"]
    frame["qfq_factor"] = 1.0
    frame["tradable"] = frame["volume"] > 0
    return PriceHistoryBundle(
        code=path.stem,
        prices=frame.sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True),
        source=f"local_real_price_cache:{path.resolve()}",
        diagnostics=["market-cost expert coverage uses local point-in-time prices"],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--output", required=True)
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
    expert_solved: Counter[str] = Counter()
    expert_positive_cash: Counter[str] = Counter()
    for symbol in builder.available_symbols():
        snapshot = builder.snapshot(symbol, anchor, config)
        if snapshot is None:
            continue
        estimate = engine.estimate(snapshot)
        market_policy = estimate.required_return_policy.get(
            "market_cost_of_equity", {}
        )
        discount_rate = _finite(market_policy.get("base"))
        solved: dict[str, float] = {}
        positive_cash: list[str] = []
        available_solved: dict[str, float] = {}
        for item in estimate.experts:
            cash = _finite(item.assumptions.get("cash_per_share"))
            terminal = _finite(item.assumptions.get("terminal_growth"))
            if cash is not None and cash > 0:
                positive_cash.append(item.expert_id)
                expert_positive_cash[item.expert_id] += 1
                implied = _market_implied_growth(
                    cash,
                    float(snapshot.current_price),
                    discount_rate,
                    terminal,
                    config.projection_years,
                )
                if implied is not None:
                    solved[item.expert_id] = float(implied)
                    expert_solved[item.expert_id] += 1
                    if item.available:
                        available_solved[item.expert_id] = float(implied)
        dominant = max(estimate.gate, key=estimate.gate.get) if estimate.gate else None
        rows.append(
            {
                "symbol": str(symbol),
                "evaluation_date": anchor.isoformat(),
                "current_price": float(snapshot.current_price),
                "market_cost_of_equity": discount_rate,
                "dominant_expert": dominant,
                "dominant_solved": bool(dominant in solved),
                "any_expert_solved": bool(solved),
                "any_available_expert_solved": bool(available_solved),
                "positive_cash_experts": positive_cash,
                "solved_experts": sorted(solved),
                "available_solved_experts": sorted(available_solved),
                "best_implied_growth": (
                    max(solved.values()) if solved else None
                ),
                "diagnostics": list(estimate.diagnostics),
            }
        )
    frame = pd.DataFrame(rows)
    report = {
        "contract": "market-cost-reverse-dcf-expert-coverage-1",
        "data_root": str(Path(args.data_root).resolve()),
        "benchmark_prices": str(Path(args.benchmark_prices).resolve()),
        "market": args.market,
        "evaluation_date": anchor.isoformat(),
        "input_snapshot_count": len(frame),
        "dominant_solved_count": int(frame["dominant_solved"].sum()),
        "any_expert_solved_count": int(frame["any_expert_solved"].sum()),
        "any_available_expert_solved_count": int(
            frame["any_available_expert_solved"].sum()
        ),
        "dominant_unresolved_but_available_alternative_solved": int(
            ((~frame["dominant_solved"]) & frame["any_available_expert_solved"]).sum()
        ),
        "no_positive_cash_expert_count": int(
            frame["positive_cash_experts"].map(len).eq(0).sum()
        ),
        "expert_positive_cash_counts": dict(sorted(expert_positive_cash.items())),
        "expert_solved_counts": dict(sorted(expert_solved.items())),
        "point_in_time": True,
        "capm_is_equity_cost_for_fcfe_proxy": True,
        "uses_existing_engine_only": True,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output.with_suffix(".csv"), index=False, encoding="utf-8")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
