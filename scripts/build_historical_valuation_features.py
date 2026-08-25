#!/usr/bin/env python3
"""Build dated reverse-DCF/CAPM features from point-in-time statements."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.market_history import PriceHistoryBundle
from src.fundamental_embedding.dataset import (
    QuarterlyPricingDatasetBuilder,
)
from src.fundamental_embedding.intrinsic_value import (
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
    _market_implied_growth,
)


def _benchmark_bundle(path: Path) -> PriceHistoryBundle:
    raw = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close"}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(f"benchmark is missing columns: {sorted(missing)}")
    frame = pd.DataFrame({
        "date": pd.to_datetime(raw["date"], errors="coerce"),
        "raw_open": pd.to_numeric(raw["open"], errors="coerce"),
        "raw_high": pd.to_numeric(raw["high"], errors="coerce"),
        "raw_low": pd.to_numeric(raw["low"], errors="coerce"),
        "raw_close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(raw.get("volume", 0.0), errors="coerce").fillna(0.0),
    }).dropna(subset=["date", "raw_close"])
    for name in ("open", "high", "low", "close"):
        frame[f"qfq_{name}"] = frame[f"raw_{name}"]
    frame["qfq_factor"] = 1.0
    frame["tradable"] = frame["volume"] > 0
    return PriceHistoryBundle(
        code=path.stem,
        prices=frame.sort_values("date").drop_duplicates("date").reset_index(drop=True),
        source=f"local_real_price_cache:{path.resolve()}",
        diagnostics=["historical beta benchmark uses local unadjusted close"],
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
    parser.add_argument("--progress-every", type=int, default=250)
    args = parser.parse_args()

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
        benchmark_bundle=_benchmark_bundle(Path(args.benchmark_prices)),
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
        dominant = max(estimate.gate, key=estimate.gate.get)
        diagnostic = estimate.reverse_dcf.get(dominant, {})
        cash = _finite(diagnostic.get("cash_per_share"))
        market_cost = (
            estimate.required_return_policy
            .get("market_cost_of_equity", {})
            .get("base")
        )
        market_cost = _finite(market_cost)
        terminal = _finite(diagnostic.get("terminal_growth"))
        implied = _market_implied_growth(
            cash,
            float(snapshot.current_price),
            market_cost,
            terminal,
            config.projection_years,
        )
        fundamental = _finite(diagnostic.get("fundamental_explicit_growth"))
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
        gap = implied - fundamental if implied is not None and fundamental is not None else None
        rows.append({
            "symbol": str(symbol),
            "feature_date": feature_date.isoformat(),
            "market_date": snapshot.market_date.isoformat(),
            "current_price": float(snapshot.current_price),
            "dominant_expert": dominant,
            "cash_per_share": cash,
            "market_implied_growth_5y": implied,
            "fundamental_growth": fundamental,
            "market_vs_fundamental_growth": gap,
            "beta": beta,
            "beta_source": beta_source,
            "capm_cost_of_equity": market_cost,
            "terminal_growth": terminal,
            "financial_age_days": snapshot.financial_age_days,
            "status": "solved" if implied is not None else "unresolved",
        })
        if index % max(1, args.progress_every) == 0:
            print(json.dumps({
                "processed": index,
                "total": total,
                "rows": len(rows),
            }), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["symbol"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "contract": "historical-reverse-dcf-capm-features-1",
        "data_root": str(Path(args.data_root).resolve()),
        "benchmark_prices": str(Path(args.benchmark_prices).resolve()),
        "market": args.market,
        "valuation_config": asdict(config),
        "dataset_rows": total,
        "output_rows": len(rows),
        "solved_rows": sum(item["status"] == "solved" for item in rows),
        "actual_beta_rows": sum(item["beta_source"] != "missing" for item in rows),
        "feature_date_count": len({item["feature_date"] for item in rows}),
        "no_lookahead": True,
        "capm_is_equity_cost_for_fcfe_proxy": True,
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
