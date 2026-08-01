#!/usr/bin/env python3
"""Benchmark every registered technical strategy at a fixed search depth."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.resource_planner import ResourcePlanner  # noqa: E402
from main import load_config  # noqa: E402
from src.analysis.strategies import STRATEGIES  # noqa: E402
from src.analysis.solvers import list_solvers  # noqa: E402
from src.analysis.strategy_benchmark import (  # noqa: E402
    BENCHMARK_GROUPS,
    prepare_benchmark_data,
    run_market_benchmark_from_snapshot,
    summarize_prepared_data,
    write_benchmark_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare registered technical strategies under one ranking-only "
            "candidate budget and the authoritative 11+2+1 window contract."
        )
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=list(STRATEGIES),
    )
    parser.add_argument("--depth", type=int, default=1000)
    parser.add_argument(
        "--solver",
        choices=list_solvers(),
        default="random",
    )
    parser.add_argument(
        "--market-workers",
        type=int,
        default=min(12, max(1, (os.cpu_count() or 4) - 2)),
    )
    parser.add_argument("--evaluation-workers", type=int, default=1)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/analysis/strategy_benchmark"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.evaluation_workers != 1:
        raise SystemExit("cross-strategy benchmark requires --evaluation-workers 1")
    unknown = sorted(set(args.strategies) - set(STRATEGIES))
    if unknown:
        raise SystemExit(f"unknown registered strategies: {', '.join(unknown)}")
    if args.depth <= 0:
        raise SystemExit("--depth must be positive")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    output_dir = args.output_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    jobs = [
        (strategy, group) for strategy in args.strategies for group in BENCHMARK_GROUPS
    ]
    print("preparing immutable market snapshots before parallel evaluation")
    prepared = prepare_benchmark_data(load_config())
    prefetch_summary = summarize_prepared_data(prepared)
    for group, summary in prefetch_summary.items():
        print(
            f"prepared {group}: {len(summary['available_codes'])} ready, "
            f"{len(summary['missing_or_short_history_codes'])} unavailable"
        )
    print(f"solver={args.solver}, budget={args.depth} candidates per market")
    resource_plan = ResourcePlanner().plan(
        "strategy_market", args.market_workers, batch_size=256
    )
    workers = min(resource_plan.outer_workers, len(jobs))
    started = monotonic()
    results = []
    failures = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                run_market_benchmark_from_snapshot,
                strategy,
                group,
                prepared[group],
                args.depth,
                args.evaluation_workers,
                args.solver,
                None,
            ): (strategy, group)
            for strategy, group in jobs
        }
        for future in as_completed(futures):
            strategy, group = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(
                    f"completed {strategy}/{group}: "
                    f"wf={result['wf_score']:.3f}, "
                    f"holdout_excess="
                    f"{result['holdout_summary']['mean_excess_pct']:.3f}%"
                )
            except Exception as exc:
                failures.append((strategy, group, str(exc)))
                print(f"failed {strategy}/{group}: {exc}", file=sys.stderr)

    if failures:
        for strategy, group, reason in failures:
            logging.error("%s/%s failed: %s", strategy, group, reason)
        return 1
    artifacts = write_benchmark_artifacts(
        output_dir=output_dir,
        market_results=results,
        search_depth=args.depth,
        market_workers=workers,
        evaluation_workers=args.evaluation_workers,
        wall_seconds=monotonic() - started,
        prefetch_summary=prefetch_summary,
    )
    for name, path in artifacts.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
