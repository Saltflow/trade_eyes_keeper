#!/usr/bin/env python3
"""Run the full-config nested optimizer search-depth experiment."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config  # noqa: E402
from src.analysis.search_depth_analysis import (  # noqa: E402
    build_depth_checkpoints,
    run_full_config_depth_analysis,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate deterministic nested phase-one candidate prefixes without "
            "consulting isolated or holdout windows."
        )
    )
    parser.add_argument("--strategy", default="regime_pullback")
    parser.add_argument("--start", type=int, default=1000)
    parser.add_argument("--maximum", type=int, default=10000)
    parser.add_argument("--points", type=int, default=6)
    parser.add_argument("--threshold", type=float, default=5.0)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/analysis/search_depth"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    depths = build_depth_checkpoints(
        args.start,
        args.maximum,
        args.points,
    )
    result = run_full_config_depth_analysis(
        config=load_config(),
        strategy_name=args.strategy,
        depths=depths,
        threshold_pct=args.threshold,
        output_root=args.output_root,
    )
    print(f"balance_depth={result['balance_depth']}")
    for name, path in result["artifacts"].items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
