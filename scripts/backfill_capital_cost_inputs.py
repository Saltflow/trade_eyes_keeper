#!/usr/bin/env python3
"""Backfill official point-in-time inputs required by CAPM and WACC."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env")

from src.data.market_history import (  # noqa: E402
    BaostockMarketHistoryProvider,
    PointInTimeMarketStore,
)
from src.data.point_in_time_backfill import (  # noqa: E402
    PointInTimeBackfillService,
)
from src.fundamental_embedding.capital_cost import (  # noqa: E402
    CapitalMarketAssumptionStore,
)
from src.fundamental_embedding.capital_market_data import (  # noqa: E402
    OfficialCapitalMarketDataProvider,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the CSI 300 benchmark, official dated capital-market "
            "assumptions, and optional company annual-report WACC fields"
        )
    )
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--data-root", default="data/point_in_time")
    parser.add_argument("--benchmark-years", type=int, default=6)
    parser.add_argument(
        "--refresh-symbols",
        default="",
        help="comma-separated equities whose statements should be refreshed",
    )
    args = parser.parse_args()

    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    data_root = Path(args.data_root)
    audit = config.get("instrument_audit", {}) or {}
    provider = OfficialCapitalMarketDataProvider(
        timeout=int(audit.get("timeout_seconds", 30)),
        user_agent=str(
            audit.get("user_agent")
            or os.getenv("EMAIL_SENDER")
            or "trade-eyes-keeper capital-cost research"
        ),
    )
    assumptions = provider.fetch_assumptions()
    assumption_path = CapitalMarketAssumptionStore(data_root).upsert(assumptions)

    end = date.today()
    start = end - timedelta(days=max(2, args.benchmark_years) * 366)
    benchmark = BaostockMarketHistoryProvider(config=config).fetch(
        "sh.000300", start, end
    )
    benchmark_paths = PointInTimeMarketStore(data_root).write(benchmark)

    symbols = [
        item.strip() for item in args.refresh_symbols.split(",") if item.strip()
    ]
    statement_report = None
    if symbols:
        scoped_config = copy.deepcopy(config)
        scoped_config.setdefault("point_in_time_data", {})["output_dir"] = str(
            data_root
        )
        statement_report = PointInTimeBackfillService(scoped_config).run(
            codes=symbols,
            evaluation_date=end,
        )

    print(
        json.dumps(
            {
                "contract": "capital-cost-input-backfill-1",
                "assumptions": assumptions.to_dict(),
                "assumption_file": str(assumption_path.resolve()),
                "benchmark": {
                    "code": benchmark.code,
                    "source": benchmark.source,
                    "rows": len(benchmark.prices),
                    "start": str(benchmark.prices["date"].min().date()),
                    "end": str(benchmark.prices["date"].max().date()),
                    "price_file": str(benchmark_paths["prices"].resolve()),
                },
                "refreshed_symbols": symbols,
                "statement_report": statement_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
