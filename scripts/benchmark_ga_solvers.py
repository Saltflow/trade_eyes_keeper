#!/usr/bin/env python3
"""Compare global and local-mutation GA with generation-level traces."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config  # noqa: E402
from src.experiments.strategy_benchmark import (  # noqa: E402
    BENCHMARK_GROUPS,
    prepare_benchmark_data,
    run_market_benchmark_from_snapshot,
    summarize_prepared_data,
)
from src.search.resources import ResourcePlanner  # noqa: E402


SOLVERS = ("genetic", "local_genetic")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare genetic and local_genetic on identical ranking windows, "
            "printing the cumulative winner after initialization and every "
            "generation."
        )
    )
    parser.add_argument("--strategy", default="technical_ensemble")
    parser.add_argument(
        "--markets",
        nargs="+",
        choices=BENCHMARK_GROUPS,
        default=list(BENCHMARK_GROUPS),
    )
    parser.add_argument(
        "--solvers",
        nargs="+",
        choices=SOLVERS,
        default=list(SOLVERS),
    )
    parser.add_argument("--budget", type=int, default=12000)
    parser.add_argument("--generations", type=int, default=5)
    parser.add_argument("--generation-size", type=int, default=1000)
    parser.add_argument("--initial-samples", type=int)
    parser.add_argument("--population-size", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260726)
    parser.add_argument(
        "--evaluation-workers",
        type=int,
        default=ResourcePlanner().physical_cores,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/analysis/ga_solver_benchmark"),
    )
    return parser


def _checkpoints(
    initial_samples: int,
    generations: int,
    generation_size: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "stage": "initialization",
            "requested_candidates": initial_samples,
        }
    ]
    for generation in range(1, generations + 1):
        rows.append(
            {
                "stage": f"generation_{generation}",
                "requested_candidates": (
                    initial_samples + generation * generation_size
                ),
            }
        )
    return rows


def _solver_config(
    solver_id: str,
    *,
    initial_samples: int,
    generations: int,
    generation_size: int,
    population_size: int,
    seed: int,
) -> dict[str, object]:
    config: dict[str, object] = {
        "phase1_random_samples": initial_samples,
        "phase1_top_keep": population_size,
        "num_generations": generations,
        "population_size": population_size,
        "offspring_size": generation_size,
        "crossover_rate": 0.7,
        "gene_mutation_rate": 0.15,
        "random_seed": seed,
    }
    if solver_id == "genetic":
        config["mutation_rate"] = 0.3
    else:
        config.update(
            {
                "max_local_step": 3,
                "step_schedule": "linear_to_one",
                "random_immigrant_rate": 0.10,
                "duplicate_retry_limit": 64,
            }
        )
    return config


def _local_step(stage: str, generations: int) -> int | None:
    if not stage.startswith("generation_"):
        return None
    generation = int(stage.rsplit("_", 1)[1])
    remaining = generations - min(generation, generations) + 1
    return max(1, -(-3 * remaining // generations))


def _progress_row(
    solver_id: str,
    market: str,
    stage: dict[str, object],
    generations: int,
) -> dict[str, object]:
    metrics = dict(stage.get("best_ranking_metrics", {}) or {})
    return {
        "solver_id": solver_id,
        "market": market,
        "stage": stage["stage"],
        "requested_candidates": stage["requested_candidates"],
        "actual_candidates": stage["actual_candidates"],
        "reached": stage["reached"],
        "unique_parameters": stage["unique_parameters"],
        "cache_hits": stage["cache_hits"],
        "feasible_candidates": stage["feasible_candidates"],
        "selection_basis": stage["selection_basis"],
        "local_step_limit": (
            _local_step(str(stage["stage"]), generations)
            if solver_id == "local_genetic"
            else None
        ),
        "best_selection_score": stage["best_selection_score"],
        "best_objective_score": stage["best_objective_score"],
        "selection_score_improvement": stage[
            "selection_score_improvement"
        ],
        "weighted_strategy_return": metrics.get("weighted_strategy_return"),
        "mean_majority_benchmark_excess": metrics.get(
            "mean_majority_benchmark_excess"
        ),
        "majority_benchmark_win_windows": metrics.get(
            "majority_benchmark_win_windows"
        ),
        "mean_strongest_benchmark_excess": metrics.get(
            "mean_strongest_benchmark_excess"
        ),
        "strongest_benchmark_win_windows": metrics.get(
            "strongest_benchmark_win_windows"
        ),
        "best_candidate_id": stage["best_candidate_id"],
    }


def _format(value: object, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (float, int)):
        return f"{float(value):.{digits}f}"
    return str(value)


def _print_progress(rows: list[dict[str, object]]) -> None:
    for row in rows:
        print(
            f"  {row['stage']}: "
            f"{row['actual_candidates']}/{row['requested_candidates']} "
            f"unique={row['unique_parameters']} "
            f"feasible={row['feasible_candidates']} "
            f"selection={_format(row['best_selection_score'])} "
            f"delta={_format(row['selection_score_improvement'])} "
            f"majority_excess="
            f"{_format(row['mean_majority_benchmark_excess'])} "
            f"majority_wins={row['majority_benchmark_win_windows']} "
            f"strongest_excess="
            f"{_format(row['mean_strongest_benchmark_excess'])}",
            flush=True,
        )


def _comparison_rows(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    indexed = {
        (str(item["solver_id"]), str(item["market"])): item
        for item in results
    }
    rows = []
    markets = sorted({str(item["market"]) for item in results})
    for market in markets:
        if ("genetic", market) not in indexed or (
            "local_genetic", market
        ) not in indexed:
            continue
        old = indexed[("genetic", market)]
        local = indexed[("local_genetic", market)]
        old_init = old["search_progress"][0]
        local_init = local["search_progress"][0]
        rows.append(
            {
                "market": market,
                "same_initial_best": (
                    old_init["best_parameters"]
                    == local_init["best_parameters"]
                    and old_init["best_selection_score"]
                    == local_init["best_selection_score"]
                ),
                "genetic_wf_score": old["wf_score"],
                "local_genetic_wf_score": local["wf_score"],
                "wf_score_delta": local["wf_score"] - old["wf_score"],
                "genetic_ranking_excess_pct": old["ranking_summary"][
                    "mean_excess_pct"
                ],
                "local_genetic_ranking_excess_pct": local["ranking_summary"][
                    "mean_excess_pct"
                ],
                "ranking_excess_delta_pct": (
                    local["ranking_summary"]["mean_excess_pct"]
                    - old["ranking_summary"]["mean_excess_pct"]
                ),
                "genetic_holdout_excess_pct": old["holdout_summary"][
                    "mean_excess_pct"
                ],
                "local_genetic_holdout_excess_pct": local["holdout_summary"][
                    "mean_excess_pct"
                ],
                "holdout_excess_delta_pct": (
                    local["holdout_summary"]["mean_excess_pct"]
                    - old["holdout_summary"]["mean_excess_pct"]
                ),
                "genetic_unique_parameters": old["solver_unique_parameters"],
                "local_genetic_unique_parameters": local[
                    "solver_unique_parameters"
                ],
                "genetic_elapsed_seconds": old["elapsed_seconds"],
                "local_genetic_elapsed_seconds": local["elapsed_seconds"],
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(
    path: Path,
    comparison: list[dict[str, object]],
    progress: list[dict[str, object]],
) -> None:
    lines = [
        "# Genetic vs local_genetic (12,000 candidates)",
        "",
        "Holdout is evaluated once for the final selected candidate only.",
        "",
        "## Final market comparison",
        "",
        "| Market | Same init | Old WF | Local WF | WF delta | "
        "Old holdout | Local holdout | Holdout delta |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in comparison:
        lines.append(
            f"| {row['market']} | {row['same_initial_best']} | "
            f"{_format(row['genetic_wf_score'])} | "
            f"{_format(row['local_genetic_wf_score'])} | "
            f"{_format(row['wf_score_delta'])} | "
            f"{_format(row['genetic_holdout_excess_pct'])} | "
            f"{_format(row['local_genetic_holdout_excess_pct'])} | "
            f"{_format(row['holdout_excess_delta_pct'])} |"
        )
    lines.extend(
        [
            "",
            "## Ranking-only generation trace",
            "",
            "| Solver | Market | Stage | Evaluated | Unique | Feasible | "
            "Selection | Delta | Majority excess | Majority wins |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in progress:
        lines.append(
            f"| {row['solver_id']} | {row['market']} | {row['stage']} | "
            f"{row['actual_candidates']} | {row['unique_parameters']} | "
            f"{row['feasible_candidates']} | "
            f"{_format(row['best_selection_score'])} | "
            f"{_format(row['selection_score_improvement'])} | "
            f"{_format(row['mean_majority_benchmark_excess'])} | "
            f"{row['majority_benchmark_win_windows']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    if args.budget <= 0 or args.generations <= 0:
        raise SystemExit("budget and generations must be positive")
    if args.generation_size <= 0 or args.population_size <= 1:
        raise SystemExit("generation and population sizes are invalid")
    initial_samples = (
        int(args.initial_samples)
        if args.initial_samples is not None
        else args.budget - args.generations * args.generation_size
    )
    expected_budget = (
        initial_samples + args.generations * args.generation_size
    )
    if initial_samples <= 0 or expected_budget != args.budget:
        raise SystemExit(
            "budget must equal initial_samples + generations * generation_size"
        )
    if args.evaluation_workers <= 0:
        raise SystemExit("evaluation workers must be positive")

    checkpoints = _checkpoints(
        initial_samples,
        args.generations,
        args.generation_size,
    )
    print("preparing immutable full-config market snapshots", flush=True)
    prepared = prepare_benchmark_data(load_config(), groups=args.markets)
    prefetch = summarize_prepared_data(prepared)
    started = monotonic()
    results: list[dict[str, object]] = []
    progress_rows: list[dict[str, object]] = []
    for market in args.markets:
        for solver_id in args.solvers:
            solver_config = _solver_config(
                solver_id,
                initial_samples=initial_samples,
                generations=args.generations,
                generation_size=args.generation_size,
                population_size=args.population_size,
                seed=args.seed,
            )
            print(
                f"running {solver_id}/{market}: budget={args.budget}, "
                f"initial={initial_samples}, "
                f"generations={args.generations}x{args.generation_size}, "
                f"workers={args.evaluation_workers}",
                flush=True,
            )
            result = run_market_benchmark_from_snapshot(
                args.strategy,
                market,
                prepared[market],
                args.budget,
                args.evaluation_workers,
                solver_id,
                solver_config,
                checkpoints,
            )
            results.append(result)
            rows = [
                _progress_row(
                    solver_id,
                    market,
                    stage,
                    args.generations,
                )
                for stage in result["search_progress"]
            ]
            progress_rows.extend(rows)
            _print_progress(rows)
            print(
                f"  final: wf={_format(result['wf_score'])}, "
                f"ranking_excess="
                f"{_format(result['ranking_summary']['mean_excess_pct'])}%, "
                f"holdout_excess="
                f"{_format(result['holdout_summary']['mean_excess_pct'])}%, "
                f"elapsed={_format(result['elapsed_seconds'], 1)}s",
                flush=True,
            )

    comparison = _comparison_rows(results)
    for row in comparison:
        print(
            f"comparison/{row['market']}: "
            f"wf_delta={_format(row['wf_score_delta'])}, "
            f"holdout_delta="
            f"{_format(row['holdout_excess_delta_pct'])}%, "
            f"same_initial={row['same_initial_best']}",
            flush=True,
        )

    output_dir = (
        args.output_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().isoformat(),
        "contract": {
            "strategy_id": args.strategy,
            "solvers": list(args.solvers),
            "markets": list(args.markets),
            "budget_per_solver_market": args.budget,
            "initial_samples": initial_samples,
            "generations": args.generations,
            "generation_size": args.generation_size,
            "population_size": args.population_size,
            "random_seed": args.seed,
            "selection_windows": 11,
            "isolated_windows": 2,
            "holdout_windows": 1,
            "generation_trace_uses_holdout": False,
            "activation_allowed": False,
        },
        "parallelism": {
            "market_workers": 1,
            "evaluation_workers": args.evaluation_workers,
        },
        "prefetch_summary": prefetch,
        "wall_seconds": monotonic() - started,
        "comparison": comparison,
        "generation_progress": progress_rows,
        "market_results": results,
    }
    json_path = output_dir / "ga_solver_comparison.json"
    progress_csv = output_dir / "generation_progress.csv"
    comparison_csv = output_dir / "market_comparison.csv"
    markdown_path = output_dir / "ga_solver_comparison.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    _write_csv(progress_csv, progress_rows)
    _write_csv(comparison_csv, comparison)
    _write_markdown(markdown_path, comparison, progress_rows)
    print(f"json={json_path.resolve()}")
    print(f"progress_csv={progress_csv.resolve()}")
    print(f"comparison_csv={comparison_csv.resolve()}")
    print(f"markdown={markdown_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
