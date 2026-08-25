#!/usr/bin/env python3
"""Diagnose intrinsic-value expert availability without changing valuation logic."""

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
    EXPERT_NAMES,
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _load_benchmark(path: Path) -> PriceHistoryBundle:
    """Load either raw OHLC columns or the point-in-time qfq schema."""

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
        diagnostics=["coverage diagnostic benchmark uses local point-in-time prices"],
    )


def _diagnostic_solved(item: Any) -> bool:
    return _finite(item.get("market_implied_explicit_growth")) is not None


def _run_date(
    builder: PointInTimeValuationBuilder,
    engine: IntrinsicValueEngine,
    feature_date: Any,
    symbols: list[str],
) -> tuple[list[dict[str, Any]], Counter[str], Counter[str]]:
    rows: list[dict[str, Any]] = []
    availability: Counter[str] = Counter()
    selection: Counter[str] = Counter()
    for symbol in symbols:
        snapshot = builder.snapshot(symbol, feature_date, engine.config)
        if snapshot is None:
            continue
        estimate = engine.estimate(snapshot)
        expert_by_id = {item.expert_id: item for item in estimate.experts}
        available = tuple(
            item.expert_id
            for item in estimate.experts
            if item.available
            and item.compatibility >= engine.config.minimum_expert_weight
            and all(
                value is not None and np.isfinite(value) and value > 0
                for value in (item.low, item.base, item.high)
            )
        )
        selected = tuple(
            item.expert_id
            for item in estimate.experts
            if estimate.gate.get(item.expert_id, 0.0) > 0.0
        )
        selected_diagnostics = tuple(
            expert_id
            for expert_id, diagnostic in estimate.reverse_dcf.items()
            if _diagnostic_solved(diagnostic)
        )
        any_available_diagnostic = tuple(
            expert_id
            for expert_id in available
            if expert_id in expert_by_id
            and _diagnostic_solved(
                engine._reverse_dcf_diagnostic(expert_by_id[expert_id], snapshot)
            )
        )
        for expert_id in EXPERT_NAMES:
            if expert_id in available:
                availability[f"available_{expert_id}"] += 1
            if expert_id in selected:
                selection[f"selected_{expert_id}"] += 1
            if expert_id in selected_diagnostics:
                selection[f"selected_solved_{expert_id}"] += 1
            if expert_id in any_available_diagnostic:
                selection[f"available_solved_{expert_id}"] += 1
        dominant = max(estimate.gate, key=estimate.gate.get) if estimate.gate else None
        rows.append(
            {
                "symbol": str(symbol),
                "feature_date": str(feature_date),
                "estimate_available": bool(available),
                "available_experts": list(available),
                "selected_experts": list(selected),
                "available_solved_experts": list(any_available_diagnostic),
                "selected_solved_experts": list(selected_diagnostics),
                "dominant_expert": dominant,
                "dominant_solved": bool(
                    dominant
                    and _diagnostic_solved(estimate.reverse_dcf.get(dominant, {}))
                ),
                "selection_loses_available_solved": bool(
                    any_available_diagnostic and not selected_diagnostics
                ),
                "diagnostics": list(estimate.diagnostics),
            }
        )
    return rows, availability, selection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--progress-every", type=int, default=100)
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
    symbols = builder.available_symbols()
    dates = [builder.latest_date()]
    all_rows: list[dict[str, Any]] = []
    availability: Counter[str] = Counter()
    selection: Counter[str] = Counter()
    for index, feature_date in enumerate(dates, start=1):
        rows, available_counts, selection_counts = _run_date(
            builder, engine, feature_date, symbols
        )
        all_rows.extend(rows)
        availability.update(available_counts)
        selection.update(selection_counts)
        if index % max(1, args.progress_every) == 0:
            print(json.dumps({"date_index": index, "dates": len(dates), "rows": len(all_rows)}), flush=True)

    frame = pd.DataFrame(all_rows)
    lost = (
        int(frame["selection_loses_available_solved"].sum())
        if not frame.empty
        else 0
    )
    summary = {
        "contract": "valuation-expert-coverage-diagnostic-1",
        "data_root": str(Path(args.data_root).resolve()),
        "benchmark_prices": str(Path(args.benchmark_prices).resolve()),
        "market": args.market,
        "date_count": len(dates),
        "candidate_symbols": len(symbols),
        "snapshot_rows": len(frame),
        "availability_counts": dict(sorted(availability.items())),
        "selection_counts": dict(sorted(selection.items())),
        "no_available_expert": (
            int((~frame["estimate_available"]).sum()) if not frame.empty else 0
        ),
        "selected_experts_without_solved_diagnostic": (
            int(
                (frame["estimate_available"] & (~frame["dominant_solved"])).sum()
            )
            if not frame.empty
            else 0
        ),
        "available_solved_lost_by_selection": lost,
        "available_solved_lost_by_selection_fraction": (
            float(lost / max(1, int(frame["estimate_available"].sum())))
            if not frame.empty
            else 0.0
        ),
        "point_in_time": True,
        "uses_existing_engine_only": True,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output.with_suffix(".csv"), index=False, encoding="utf-8")
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_name(output.stem + "_examples.json").write_text(
        json.dumps(
            frame[frame["selection_loses_available_solved"]].head(100).to_dict("records"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
