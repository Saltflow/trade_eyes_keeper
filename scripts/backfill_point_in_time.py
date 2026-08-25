#!/usr/bin/env python3
"""Backfill point-in-time market/fundamental history for the full config."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env")

from src.data.point_in_time_backfill import (  # noqa: E402
    PointInTimeBackfillService,
)


def _append_unique(settings: dict[str, Any], key: str, values: list[str]) -> None:
    """Add explicitly requested market-only inputs without mutating base config."""

    existing = [str(value).strip() for value in settings.get(key, []) or []]
    additions = [str(value).strip() for value in values if str(value).strip()]
    settings[key] = list(dict.fromkeys([*existing, *additions]))


def _scoped_config(
    config: dict[str, Any],
    *,
    output_dir: str | None,
    history_years: int | None,
    fx_symbols: list[str],
    market_only_symbols: list[str],
    hkex_report_kinds: list[str],
) -> dict[str, Any]:
    """Return a research-safe copy with explicit point-in-time overrides."""

    scoped = copy.deepcopy(config)
    settings = scoped.setdefault("point_in_time_data", {})
    if not isinstance(settings, dict):
        raise TypeError("point_in_time_data configuration must be a mapping")
    if output_dir:
        settings["output_dir"] = output_dir
    if history_years is not None:
        if history_years < 1:
            raise ValueError("--history-years must be at least one")
        settings["history_years"] = int(history_years)
    _append_unique(settings, "fx_symbols", fx_symbols)
    _append_unique(settings, "market_only_symbols", market_only_symbols)
    if hkex_report_kinds:
        settings["hkex_report_kinds"] = list(
            dict.fromkeys(str(value).strip() for value in hkex_report_kinds)
        )
    return scoped


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
    parser.add_argument(
        "--codes",
        nargs="*",
        help="optional instrument subset; avoids a full-config backfill",
    )
    parser.add_argument(
        "--output-dir",
        help="isolated point-in-time output root; never changes config.yaml",
    )
    parser.add_argument(
        "--history-years",
        type=int,
        help="history years for this invocation (default: config value)",
    )
    parser.add_argument(
        "--evaluation-date",
        type=date.fromisoformat,
        help="end of the point-in-time window, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--fx-symbol",
        action="append",
        default=[],
        help="market-only FX series; repeated values are allowed",
    )
    parser.add_argument(
        "--market-only-symbol",
        action="append",
        default=[],
        help="benchmark/index series without company-statement requests",
    )
    parser.add_argument(
        "--hkex-report-kind",
        action="append",
        choices=("year", "half_year"),
        default=[],
        help=(
            "restrict HKEX statement documents for this run; use year for "
            "a bounded annual-history bootstrap"
        ),
    )
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise TypeError("configuration root must be a mapping")
    scoped = _scoped_config(
        config,
        output_dir=args.output_dir,
        history_years=args.history_years,
        fx_symbols=args.fx_symbol,
        market_only_symbols=args.market_only_symbol,
        hkex_report_kinds=args.hkex_report_kind,
    )
    report = PointInTimeBackfillService(scoped).run(
        codes=args.codes or None,
        evaluation_date=args.evaluation_date,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["market_history_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
