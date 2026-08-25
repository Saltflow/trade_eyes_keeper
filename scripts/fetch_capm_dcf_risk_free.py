"""Fetch auditable point-in-time government yields for CAPM-DCF snapshots.

Calibration labels only need dated rates up to the last completed outcome
window.  A live/frozen value policy additionally needs a real rate at every
new reporting anchor.  This utility derives those anchors from the actual
point-in-time benchmark calendar and writes only fetched official values; it
never fills a missing historical rate with a current quote or a constant.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.market_history import PointInTimeMarketStore
from src.fundamental_embedding.capital_market_data import (
    OfficialCapitalMarketDataProvider,
)

CONTRACT = "official-government-risk-free-snapshot-history-2"
HKMA_EFBN_DAILY_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "monthly-statistical-bulletin/efbn/efbn-yield-daily"
)
US_TREASURY_DAILY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)


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


def _anchors_from_frame(raw: pd.DataFrame) -> list[date]:
    """Return actual last-trading-day anchors, never calendar approximations."""

    if "date" not in raw:
        raise ValueError("benchmark price data is missing date")
    frame = pd.DataFrame(
        {"date": pd.to_datetime(raw["date"], errors="coerce")}
    ).dropna()
    frame["quarter"] = frame["date"].dt.to_period("Q")
    return [item.date() for item in frame.groupby("quarter")["date"].max()]


def _anchors(benchmark_prices: Path) -> list[date]:
    if not benchmark_prices.exists():
        raise ValueError(f"benchmark price CSV is missing: {benchmark_prices}")
    raw = pd.read_csv(benchmark_prices)
    return _anchors_from_frame(raw)


def _store_anchors(data_root: Path, benchmark_symbol: str) -> list[date]:
    bundle = PointInTimeMarketStore(data_root).read(benchmark_symbol)
    if bundle is None:
        raise ValueError(
            "point-in-time benchmark symbol is missing from market store: "
            f"{benchmark_symbol}"
        )
    return _anchors_from_frame(bundle.prices)


def _fetch_chinabond_one(
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


def _hkma_records(
    http: requests.Session,
    *,
    timeout: int,
    not_before: date,
) -> list[dict[str, object]]:
    """Download the official HKMA daily EFBN table with bounded pagination."""

    records: list[dict[str, object]] = []
    offset = 0
    while True:
        response = http.get(
            HKMA_EFBN_DAILY_URL,
            params={"offset": offset},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        page = result.get("records", []) if isinstance(result, dict) else []
        if not isinstance(page, list):
            raise ValueError("HKMA EFBN response has no record list")
        records.extend(item for item in page if isinstance(item, dict))
        if not page:
            break
        page_dates = []
        for item in page:
            if not isinstance(item, dict):
                continue
            try:
                page_dates.append(_parse_date(str(item.get("end_of_day", ""))))
            except ValueError:
                continue
        # HKMA's ``datasize`` is the page size (normally 100), rather than a
        # total result count.  Continue through pages until the oldest page
        # crosses the earliest requested anchor; stopping at datasize would
        # silently make older valuation dates appear to have no risk-free rate.
        if page_dates and min(page_dates) <= not_before:
            break
        offset += len(page)
        if offset > 20_000:
            raise RuntimeError("HKMA EFBN pagination exceeded safe record limit")
    return records


def _fetch_hkma_efbn_two_year(
    records: list[dict[str, object]],
    requested: date,
) -> tuple[float, dict[str, object]]:
    """Select the last published HKD two-year EFN yield on or before anchor.

    HKMA notes that the 10-year EFN series ceased new issuance after 2015;
    using the published two-year EFN therefore avoids inventing a stale
    long-rate for current H-share valuation.  The chosen tenor is explicit in
    the audit record and is part of the market-local policy contract.
    """

    candidates: list[tuple[date, float]] = []
    for row in records:
        try:
            record_date = _parse_date(str(row.get("end_of_day", "")))
        except ValueError:
            continue
        value = row.get("efn_2y")
        try:
            rate = float(value) / 100.0
        except (TypeError, ValueError):
            continue
        if record_date <= requested and 0.0 < rate < 0.20:
            candidates.append((record_date, rate))
    if not candidates:
        raise RuntimeError(
            "HKMA two-year EFN yield unavailable on or before "
            f"{requested.isoformat()}"
        )
    source_date, rate = max(candidates)
    if (requested - source_date).days > 8:
        raise RuntimeError(
            "HKMA two-year EFN yield is stale for "
            f"{requested.isoformat()}: {source_date.isoformat()}"
        )
    return rate, {
        "requested_date": requested.isoformat(),
        "source_date": source_date.isoformat(),
        "risk_free_rate": rate,
        "source": "HKMA Exchange Fund Notes 2Y yield",
        "maturity": "2Y",
        "source_url": HKMA_EFBN_DAILY_URL,
    }


def _us_treasury_year(
    http: requests.Session,
    *,
    year: int,
    timeout: int,
) -> pd.DataFrame:
    """Read the official US Treasury daily curve for one calendar year."""

    response = http.get(
        US_TREASURY_DAILY_URL.format(year=year),
        params={
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value": str(year),
            "page": "",
            "_format": "csv",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    raw = pd.read_csv(io.StringIO(response.text))
    column_by_normalized = {
        str(column).strip().lower().replace(" ", ""): column
        for column in raw.columns
    }
    date_column = column_by_normalized.get("date")
    ten_year_column = (
        column_by_normalized.get("10yr")
        or column_by_normalized.get("10-year")
        or column_by_normalized.get("10year")
    )
    if date_column is None or ten_year_column is None:
        raise ValueError("US Treasury daily curve is missing Date or 10 yr")
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[date_column], errors="coerce"),
            "rate": pd.to_numeric(raw[ten_year_column], errors="coerce") / 100.0,
        }
    ).dropna()
    return frame[(frame["rate"] > 0.0) & (frame["rate"] < 0.20)]


def _fetch_us_treasury_ten_year(
    cache: dict[int, pd.DataFrame],
    http: requests.Session,
    requested: date,
    *,
    timeout: int,
) -> tuple[float, dict[str, object]]:
    """Select last available official 10-year Treasury yield at the anchor."""

    frames: list[pd.DataFrame] = []
    for year in (requested.year, requested.year - 1):
        if year not in cache:
            cache[year] = _us_treasury_year(http, year=year, timeout=timeout)
        frames.append(cache[year])
    merged = pd.concat(frames, ignore_index=True)
    observed = merged[merged["date"].dt.date <= requested]
    if observed.empty:
        raise RuntimeError(
            "US Treasury 10-year yield unavailable on or before "
            f"{requested.isoformat()}"
        )
    item = observed.sort_values("date").iloc[-1]
    source_date = pd.Timestamp(item["date"]).date()
    if (requested - source_date).days > 8:
        raise RuntimeError(
            "US Treasury 10-year yield is stale for "
            f"{requested.isoformat()}: {source_date.isoformat()}"
        )
    rate = float(item["rate"])
    return rate, {
        "requested_date": requested.isoformat(),
        "source_date": source_date.isoformat(),
        "risk_free_rate": rate,
        "source": "US Treasury daily 10-year yield curve",
        "maturity": "10Y",
        "source_url": US_TREASURY_DAILY_URL.format(year=requested.year),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    benchmark_group = parser.add_mutually_exclusive_group(required=True)
    benchmark_group.add_argument("--benchmark-prices")
    benchmark_group.add_argument("--benchmark-symbol")
    parser.add_argument(
        "--data-root",
        help="point-in-time root; required with --benchmark-symbol",
    )
    parser.add_argument(
        "--source",
        choices=("chinabond_10y", "hkma_efn_2y", "us_treasury_10y"),
        default="chinabond_10y",
    )
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
    if args.benchmark_symbol:
        if not args.data_root:
            parser.error("--data-root is required with --benchmark-symbol")
        anchors = _store_anchors(Path(args.data_root), args.benchmark_symbol)
    else:
        anchors = _anchors(Path(args.benchmark_prices))
    requested = [item for item in anchors if start <= item <= end]
    if not requested:
        raise ValueError("no quarterly benchmark anchors fall in the requested range")
    timeout = max(1, int(args.timeout))
    provider = OfficialCapitalMarketDataProvider(timeout=timeout)
    http = requests.Session()
    hkma_records = (
        _hkma_records(
            http,
            timeout=timeout,
            not_before=min(requested) - timedelta(days=8),
        )
        if args.source == "hkma_efn_2y"
        else []
    )
    treasury_cache: dict[int, pd.DataFrame] = {}
    for item in requested:
        if item in rates:
            continue
        print(json.dumps({"stage": "fetch", "date": item.isoformat()}), flush=True)
        if args.source == "chinabond_10y":
            rate, row = _fetch_chinabond_one(provider, item)
        elif args.source == "hkma_efn_2y":
            rate, row = _fetch_hkma_efbn_two_year(hkma_records, item)
        else:
            rate, row = _fetch_us_treasury_ten_year(
                treasury_cache, http, item, timeout=timeout
            )
        rates[item] = rate
        audit[item.isoformat()] = row
    payload = {
        "contract": CONTRACT,
        "risk_free_source": args.source,
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
