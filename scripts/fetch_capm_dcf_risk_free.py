"""Fetch auditable point-in-time ChinaBond 10Y rates for CAPM-DCF snapshots.

Calibration labels only need dated rates up to the last completed outcome
window.  A live/frozen value policy additionally needs a real rate at every
new reporting anchor.  This utility derives those anchors from the actual
point-in-time benchmark calendar and writes only fetched official values; it
never fills a missing historical rate with a current quote or a constant.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fundamental_embedding.capital_market_data import (
    OfficialCapitalMarketDataProvider,
)

CONTRACT = "official-chinabond-10y-snapshot-history-1"


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value))


def _load_existing(path: Path | None) -> tuple[dict[date, float], dict[str, Any]]:
    if path is None or not path.exists():
        return {}, {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = raw.get("risk_free_rates", raw)
    if not isinstance(values, dict):
        raise TypeError("risk-free input must be a date-to-rate mapping")
    rates = {_parse_date(key): float(value) for key, value in values.items()}
    audit = raw.get("audit", {})
    return rates, dict(audit) if isinstance(audit, dict) else {}


def _anchors(benchmark_prices: Path) -> list[date]:
    if not benchmark_prices.exists():
        raise ValueError(f"benchmark price CSV is missing: {benchmark_prices}")
    import pandas as pd

    raw = pd.read_csv(benchmark_prices)
    if "date" not in raw:
        raise ValueError("benchmark price CSV is missing date")
    frame = pd.DataFrame(
        {"date": pd.to_datetime(raw["date"], errors="coerce")}
    ).dropna()
    frame["quarter"] = frame["date"].dt.to_period("Q")
    return [item.date() for item in frame.groupby("quarter")["date"].max()]


def _fetch_one(
    provider: OfficialCapitalMarketDataProvider,
    requested: date,
) -> tuple[float, dict[str, object]]:
    failures: list[str] = []
    for offset in range(8):
        attempted = requested - timedelta(days=offset)
        try:
            rate = float(provider.fetch_chinabond_ten_year_yield(attempted))
        except (
            requests.RequestException,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            failures.append(f"{attempted.isoformat()}:{type(exc).__name__}")
            continue
        if not 0.0 < rate < 0.20:
            failures.append(f"{attempted.isoformat()}:out_of_range")
            continue
        return rate, {
            "requested_date": requested.isoformat(),
            "source_date": attempted.isoformat(),
            "risk_free_rate": rate,
            "source": "ChinaBond 10Y government curve",
        }
    raise RuntimeError(
        "ChinaBond 10Y yield unavailable for "
        f"{requested.isoformat()}; attempts={','.join(failures)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-prices", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", help="inclusive YYYY-MM-DD")
    parser.add_argument("--end", help="inclusive YYYY-MM-DD")
    parser.add_argument("--resume-from")
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args()

    output = Path(args.output)
    rates, audit = _load_existing(
        Path(args.resume_from) if args.resume_from else None
    )
    start = _parse_date(args.start) if args.start else date.min
    end = _parse_date(args.end) if args.end else date.max
    requested = [
        item
        for item in _anchors(Path(args.benchmark_prices))
        if start <= item <= end
    ]
    if not requested:
        raise ValueError("no quarterly benchmark anchors fall in the requested range")
    provider = OfficialCapitalMarketDataProvider(timeout=max(1, int(args.timeout)))
    for item in requested:
        if item in rates:
            continue
        print(json.dumps({"stage": "fetch", "date": item.isoformat()}), flush=True)
        rate, row = _fetch_one(provider, item)
        rates[item] = rate
        audit[item.isoformat()] = row
    payload = {
        "contract": CONTRACT,
        "risk_free_rates": {
            item.isoformat(): rate for item, rate in sorted(rates.items())
        },
        "audit": {key: audit[key] for key in sorted(audit)},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "output": str(output),
                "requested_count": len(requested),
                "rate_count": len(rates),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
