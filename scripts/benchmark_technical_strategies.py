#!/usr/bin/env python3
"""Benchmark every registered technical strategy at a fixed search depth."""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.search.resources import ResourcePlanner  # noqa: E402
from main import load_config  # noqa: E402
from src.strategy import list_strategy_ids  # noqa: E402
from src.search import list_solvers  # noqa: E402
from src.experiments.strategy_benchmark import (  # noqa: E402
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
        default=list(list_strategy_ids()),
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
        default=None,
        help=(
            "parallel strategy/market jobs; mutually exclusive with "
            "multi-process evaluation"
        ),
    )
    parser.add_argument(
        "--evaluation-workers",
        type=int,
        default=None,
        help="persistent candidate-scoring processes per job",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/analysis/strategy_benchmark"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    unknown = sorted(set(args.strategies) - set(list_strategy_ids()))
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
    planner = ResourcePlanner()
    if args.market_workers is None and args.evaluation_workers is None:
        if len(jobs) >= planner.physical_cores:
            market_workers = planner.physical_cores
            evaluation_workers = 1
        else:
            market_workers = 1
            evaluation_workers = planner.physical_cores
    else:
        market_workers = int(args.market_workers or 1)
        evaluation_workers = int(args.evaluation_workers or 1)
    if market_workers <= 0 or evaluation_workers <= 0:
        raise SystemExit("worker counts must be positive")
    if market_workers > 1 and evaluation_workers > 1:
        raise SystemExit(
            "choose one parallel axis: market workers or candidate evaluation workers"
        )
    resource_plan = planner.plan(
        "strategy_market", market_workers, batch_size=256
    )
    workers = min(resource_plan.outer_workers, len(jobs))

    print("preparing immutable market snapshots before parallel evaluation")
    prepared = prepare_benchmark_data(load_config())
    prefetch_summary = summarize_prepared_data(prepared)
    for group, summary in prefetch_summary.items():
        print(
            f"prepared {group}: {len(summary['available_codes'])} ready, "
            f"{len(summary['missing_or_short_history_codes'])} unavailable"
        )
    print(f"solver={args.solver}, budget={args.depth} candidates per market")
    print(
        f"parallelism=market:{workers}, "
        f"candidate_processes_per_job:{evaluation_workers}"
    )
    started = monotonic()
    results = []
    failures = []

    def record_success(strategy, group, result):
        results.append(result)
        print(
            f"completed {strategy}/{group}: "
            f"wf={result['wf_score']:.3f}, "
            f"holdout_excess="
            f"{result['holdout_summary']['mean_excess_pct']:.3f}%"
        )

    if workers == 1:
        for strategy, group in jobs:
            try:
                result = run_market_benchmark_from_snapshot(
                    strategy,
                    group,
                    prepared[group],
                    args.depth,
                    evaluation_workers,
                    args.solver,
                    None,
                )
                record_success(strategy, group, result)
            except Exception as exc:
                failures.append((strategy, group, str(exc)))
                print(f"failed {strategy}/{group}: {exc}", file=sys.stderr)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_market_benchmark_from_snapshot,
                    strategy,
                    group,
                    prepared[group],
                    args.depth,
                    evaluation_workers,
                    args.solver,
                    None,
                ): (strategy, group)
                for strategy, group in jobs
            }
            for future in as_completed(futures):
                strategy, group = futures[future]
                try:
                    record_success(strategy, group, future.result())
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
        evaluation_workers=evaluation_workers,
        wall_seconds=monotonic() - started,
        prefetch_summary=prefetch_summary,
    )
    for name, path in artifacts.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
