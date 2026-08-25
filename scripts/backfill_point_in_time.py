#!/usr/bin/env python3
"""Backfill point-in-time market/fundamental history for the full config."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env")

from src.data.point_in_time_backfill import PointInTimeBackfillService


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill raw/qfq prices, corporate actions and disclosure-dated "
            "financial statements without changing active strategies"
        )
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "config.yaml"),
        help="configuration file (default: config/config.yaml)",
    )
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    report = PointInTimeBackfillService(config).run()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["market_history_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
