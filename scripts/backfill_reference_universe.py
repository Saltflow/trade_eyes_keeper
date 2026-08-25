#!/usr/bin/env python3
"""Backfill the current index-reference company universe with resume support."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env")

from src.data.reference_universe_backfill import (
    ReferenceUniverseBackfillService,
    load_reference_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resumably backfill current CSI 300/500/Dividend constituents using "
            "a bounded process pool"
        )
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "config.yaml"),
        help="configuration file (default: config/config.yaml)",
    )
    parser.add_argument("--manifest", required=True, help="reference manifest JSON")
    parser.add_argument("--output-dir", required=True, help="isolated dataset root")
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        choices=range(1, 5),
        metavar="{1,2,3,4}",
        help="worker processes (default: 2; intentionally capped at 4)",
    )
    parser.add_argument(
        "--evaluation-date",
        type=date.fromisoformat,
        default=date.today(),
        help="point-in-time window end, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--codes",
        nargs="*",
        help="optional manifest-code subset for an isolated smoke run",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore matching checkpoints and fetch every selected code again",
    )
    parser.add_argument(
        "--no-retry-failures",
        action="store_true",
        help="reuse failed checkpoints instead of retrying them",
    )
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    manifest = load_reference_manifest(args.manifest)
    report = ReferenceUniverseBackfillService(
        config,
        manifest,
        output_dir=args.output_dir,
        workers=args.workers,
    ).run(
        evaluation_date=args.evaluation_date,
        codes=args.codes,
        force=args.force,
        retry_failures=not args.no_retry_failures,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["state"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
