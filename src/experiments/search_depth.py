"""Controlled marginal-effect analysis for phase-one optimizer depth.

The experiment deliberately evaluates one deterministic candidate stream in
nested prefixes.  A larger checkpoint therefore contains every candidate from
the smaller checkpoint and cannot look better merely because it used another
random seed.  Only ranking windows are used; embargo and holdout windows remain
untouched.
"""

from __future__ import annotations

import csv
import json
import logging
import math
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import Iterable

import numpy as np

from ..backtest.engine import FastEvaluator, WalkForwardManager
from .benchmark_search import run_ranking_benchmark_search
from ..markets import _detect_fine_group, get_skip_search
from ..search.workflow import _partition_window_indexes
from ..search.resources import ResourcePlanner
from ..strategy import get_strategy
from .strategy_benchmark import frame_fingerprint

logger = logging.getLogger(__name__)


def build_depth_checkpoints(
    start: int = 1000,
    maximum: int = 10000,
    points: int = 6,
) -> list[int]:
    """Return evenly spaced, inclusive integer search-depth checkpoints."""
    start = int(start)
    maximum = int(maximum)
    points = int(points)
    if start <= 0:
        raise ValueError("start must be positive")
    if maximum < start:
        raise ValueError("maximum must not be less than start")
    if points < 2:
        raise ValueError("points must be at least 2")

    depths = [
        int(round(start + index * (maximum - start) / (points - 1)))
        for index in range(points)
    ]
    result = list(dict.fromkeys(depths))
    if len(result) != points:
        raise ValueError("range is too narrow to produce distinct checkpoints")
    return result


def score_index(wf_score: float) -> float:
    """Map a percentage-point-like WF objective to a positive effect index."""
    return max(1e-9, 100.0 + float(wf_score))


def marginal_effect_improvement(previous: float, current: float) -> float:
    """Return relative improvement in the positive WF score index."""
    return (score_index(current) / score_index(previous) - 1.0) * 100.0


def choose_balance_depth(
    depths: list[int],
    marginal_improvements: list[float | None],
    ranking_eligible: list[bool],
    threshold_pct: float = 5.0,
) -> int:
    """Choose the first depth after which every later increment is below target.

    The selected value is the lower checkpoint before the first persistently
    unproductive increment.  A later transition to a ranking-eligible
    all-market candidate prevents an earlier stop, regardless of the raw score
    delta.  This function never interprets holdout results.
    """
    if not depths:
        raise ValueError("at least one depth is required")
    if not (len(depths) == len(marginal_improvements) == len(ranking_eligible)):
        raise ValueError("depth, improvement and deployable lengths must match")
    if len(depths) == 1:
        return depths[0]

    for current_index in range(1, len(depths)):
        future_improvements = [
            float(value)
            for value in marginal_improvements[current_index:]
            if value is not None
        ]
        if not future_improvements:
            continue
        later_becomes_eligible = not ranking_eligible[current_index - 1] and any(
            ranking_eligible[current_index:]
        )
        if not later_becomes_eligible and all(
            value < threshold_pct for value in future_improvements
        ):
            return depths[current_index - 1]
    return depths[-1]


def _candidate_outcome(result) -> dict[str, object]:
    if result is None or not result.ranking_stats:
        return {
            "evaluated": False,
            "wf_score": None,
            "return_gate_passed": False,
            "hard_constraints_passed": False,
            "eligible": False,
            "ranking_diagnostics": {},
        }
    diagnostics = dict(result.ranking_metrics)
    gate_passed = bool(result.gate_feasible)
    violations = [item["rule_id"] for item in result.gate_results if not item["passed"]]
    return {
        "evaluated": True,
        "wf_score": float(result.objective_score),
        "return_gate_passed": gate_passed,
        "hard_constraints_passed": gate_passed,
        "eligible": gate_passed,
        "ranking_diagnostics": diagnostics,
        "hard_constraint_violations": list(violations),
    }


def _best_by_score(outcomes: Iterable[dict[str, object]]):
    scored = [outcome for outcome in outcomes if outcome.get("wf_score") is not None]
    return max(scored, key=lambda item: float(item["wf_score"])) if scored else None


def summarize_prefix(
    outcomes: list[dict[str, object]],
    depth: int,
    unique_evaluations: int,
    elapsed_seconds: float,
) -> dict[str, object]:
    """Summarize one nested prefix without consulting holdout results."""
    prefix = outcomes[:depth]
    evaluated = [item for item in prefix if item["evaluated"]]
    return_gate = [item for item in evaluated if item["return_gate_passed"]]
    hard_pass = [item for item in evaluated if item["hard_constraints_passed"]]
    eligible = [item for item in evaluated if item["eligible"]]
    best_raw = _best_by_score(evaluated)
    best_gate = _best_by_score(return_gate)
    best_eligible = _best_by_score(eligible)
    diagnostics = dict(best_raw.get("ranking_diagnostics", {})) if best_raw else {}
    return {
        "depth": int(depth),
        "generated_candidates": len(prefix),
        "unique_evaluations": int(unique_evaluations),
        "valid_evaluations": len(evaluated),
        "return_gate_pass_count": len(return_gate),
        "hard_constraint_pass_count": len(hard_pass),
        "eligible_candidate_count": len(eligible),
        "best_raw_wf_score": (float(best_raw["wf_score"]) if best_raw else None),
        "best_return_gate_wf_score": (
            float(best_gate["wf_score"]) if best_gate else None
        ),
        "best_eligible_wf_score": (
            float(best_eligible["wf_score"]) if best_eligible else None
        ),
        "best_raw_weighted_strategy_return": diagnostics.get(
            "weighted_strategy_return"
        ),
        "best_raw_positive_return_windows": diagnostics.get("positive_return_windows"),
        "best_raw_mean_strongest_benchmark_excess": diagnostics.get(
            "mean_strongest_benchmark_excess"
        ),
        "best_raw_strongest_benchmark_win_windows": diagnostics.get(
            "strongest_benchmark_win_windows"
        ),
        "elapsed_seconds": float(elapsed_seconds),
    }


def aggregate_market_records(
    depths: list[int],
    market_records: dict[str, list[dict[str, object]]],
    threshold_pct: float = 5.0,
) -> tuple[list[dict[str, object]], int]:
    """Build cross-market curves and their stable 5% marginal balance point."""
    aggregate: list[dict[str, object]] = []
    groups = list(market_records)
    for index, depth in enumerate(depths):
        rows = [market_records[group][index] for group in groups]
        scores = [
            float(row["best_raw_wf_score"])
            for row in rows
            if row["best_raw_wf_score"] is not None
        ]
        if len(scores) != len(groups):
            raise RuntimeError(f"missing raw score at search depth {depth}")
        eligible_markets = sum(int(row["eligible_candidate_count"]) > 0 for row in rows)
        row = {
            "depth": depth,
            "compute_multiple_vs_1000": depth / 1000.0,
            "mean_best_raw_wf_score": float(np.mean(scores)),
            "worst_market_best_raw_wf_score": float(np.min(scores)),
            "eligible_market_count": eligible_markets,
            "all_markets_ranking_eligible": eligible_markets == len(groups),
            "total_eligible_candidates": sum(
                int(item["eligible_candidate_count"]) for item in rows
            ),
            "cumulative_seconds": sum(float(item["elapsed_seconds"]) for item in rows),
            "incremental_score_gain": None,
            "marginal_effect_improvement_pct": None,
        }
        if aggregate:
            previous = aggregate[-1]
            current_score = float(row["mean_best_raw_wf_score"])
            previous_score = float(previous["mean_best_raw_wf_score"])
            row["incremental_score_gain"] = current_score - previous_score
            row["marginal_effect_improvement_pct"] = marginal_effect_improvement(
                previous_score, current_score
            )
        aggregate.append(row)

    balance = choose_balance_depth(
        depths,
        [row["marginal_effect_improvement_pct"] for row in aggregate],
        [bool(row["all_markets_ranking_eligible"]) for row in aggregate],
        threshold_pct=threshold_pct,
    )
    return aggregate, balance


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _plot_result(
    path: Path,
    depths: list[int],
    market_records: dict[str, list[dict[str, object]]],
    aggregate: list[dict[str, object]],
    balance_depth: int,
    threshold_pct: float,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {
        "a_share": "#d95f02",
        "hk": "#1b9e77",
        "us": "#7570b3",
    }
    labels = {"a_share": "A-share", "hk": "Hong Kong", "us": "US"}
    figure, axes = plt.subplots(
        3,
        1,
        figsize=(12, 11),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1.5, 1.5]},
    )

    for group, rows in market_records.items():
        axes[0].plot(
            depths,
            [row["best_raw_wf_score"] for row in rows],
            marker="o",
            linewidth=1.8,
            label=labels.get(group, group),
            color=colors.get(group),
        )
    axes[0].plot(
        depths,
        [row["mean_best_raw_wf_score"] for row in aggregate],
        marker="D",
        linewidth=2.6,
        linestyle="--",
        color="#222222",
        label="Cross-market mean",
    )
    axes[0].set_ylabel("Best raw ranking WF score")
    axes[0].set_title("Marginal Effect of Ranking-only Search Depth")
    axes[0].grid(alpha=0.25)
    axes[0].legend(ncol=4, fontsize=9)

    marginal = [
        (
            0.0
            if row["marginal_effect_improvement_pct"] is None
            else float(row["marginal_effect_improvement_pct"])
        )
        for row in aggregate
    ]
    axes[1].bar(
        depths[1:],
        marginal[1:],
        width=900,
        color="#4c78a8",
        alpha=0.85,
        label="Marginal effect improvement",
    )
    axes[1].axhline(
        threshold_pct,
        color="#e45756",
        linestyle="--",
        linewidth=2,
        label=f"{threshold_pct:g}% threshold",
    )
    axes[1].set_ylabel("Improvement vs prior point (%)")
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend(fontsize=9)

    eligible = [int(row["total_eligible_candidates"]) for row in aggregate]
    axes[2].bar(
        depths,
        eligible,
        width=900,
        color="#59a14f",
        alpha=0.8,
        label="Eligible candidates (3 markets)",
    )
    axes[2].set_ylabel("Eligible candidates")
    maximum_eligible = max(eligible, default=0)
    axes[2].set_ylim(0, max(1.0, maximum_eligible * 1.15))
    if maximum_eligible == 0:
        axes[2].text(
            0.5,
            0.42,
            "No ranking-eligible candidate at any depth",
            transform=axes[2].transAxes,
            horizontalalignment="center",
            color="#3f6f3b",
            fontsize=10,
        )
    axes[2].set_xlabel("Nested random candidate evaluations per market")
    axes[2].grid(axis="y", alpha=0.25)
    time_axis = axes[2].twinx()
    time_axis.plot(
        depths,
        [float(row["cumulative_seconds"]) / 60.0 for row in aggregate],
        color="#f28e2b",
        marker="o",
        label="Measured cumulative runtime",
    )
    time_axis.set_ylabel("Runtime (minutes)")
    handles_left, labels_left = axes[2].get_legend_handles_labels()
    handles_right, labels_right = time_axis.get_legend_handles_labels()
    axes[2].legend(
        handles_left + handles_right,
        labels_left + labels_right,
        loc="upper left",
        fontsize=9,
    )

    for axis in axes:
        axis.axvline(
            balance_depth,
            color="#b279a2",
            linestyle=":",
            linewidth=2,
        )
    axes[0].annotate(
        f"5% balance: {balance_depth}",
        xy=(balance_depth, axes[0].get_ylim()[1]),
        xytext=(8, -18),
        textcoords="offset points",
        color="#7a4e74",
        fontsize=10,
    )
    axes[2].set_xticks(depths)
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def run_full_config_depth_analysis(
    *,
    config: dict,
    strategy_name: str = "regime_pullback",
    depths: list[int] | None = None,
    threshold_pct: float = 5.0,
    output_root: Path | str = Path("data/analysis/search_depth"),
) -> dict[str, object]:
    """Run a full-config, three-market nested phase-one depth experiment."""
    from main import (
        OPTIMIZER_GROUPS,
        _has_optimizer_history,
        _load_optimizer_benchmarks,
        _optimizer_lookback_days,
        _stock_code,
    )
    from src.search.config import get_constraints
    from src.data.data_source import DataSource

    strategy = get_strategy(strategy_name)
    if strategy is None:
        raise ValueError(f"unknown registered strategy: {strategy_name}")
    depths = depths or build_depth_checkpoints()
    if sorted(depths) != depths or len(set(depths)) != len(depths):
        raise ValueError("depth checkpoints must be strictly increasing")

    constraints = get_constraints()
    seed = constraints.genetic_search.random_seed
    if seed is None:
        raise ValueError("a fixed genetic_search.random_seed is required")
    lookback_days = _optimizer_lookback_days(constraints)
    skipped = get_skip_search(config)
    configured_groups: dict[str, list[str]] = {group: [] for group in OPTIMIZER_GROUPS}
    for stock in config.get("stocks", []) or []:
        code = _stock_code(stock)
        if not code or code in skipped:
            continue
        group = _detect_fine_group(code)
        if group in configured_groups:
            configured_groups[group].append(code)
    configured_groups = {
        group: codes for group, codes in configured_groups.items() if codes
    }
    if set(configured_groups) != set(OPTIMIZER_GROUPS):
        raise RuntimeError(
            "full-config benchmark requires configured A-share, HK and US groups"
        )

    output_dir = Path(output_root) / (
        f"{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    data_source = DataSource(config)
    market_records: dict[str, list[dict[str, object]]] = {}
    market_metadata: dict[str, dict[str, object]] = {}

    for group in OPTIMIZER_GROUPS:
        constraints.set_group(group)
        stocks_data = {}
        missing_codes = []
        for code in configured_groups[group]:
            try:
                data = data_source.fetch_stock_data(code, days=lookback_days)
            except Exception as exc:
                logger.warning("Unable to load %s for %s: %s", code, group, exc)
                missing_codes.append(code)
                continue
            if (
                data is None
                or data.empty
                or not _has_optimizer_history(data, constraints)
            ):
                missing_codes.append(code)
                continue
            stocks_data[code] = data
        if not stocks_data:
            raise RuntimeError(f"no full-horizon market data for {group}")

        benchmarks = _load_optimizer_benchmarks(
            data_source, constraints, group, lookback_days
        )
        wf_manager = WalkForwardManager(
            stocks_data,
            constraints,
            list(stocks_data),
            benchmark_data=benchmarks,
        )
        wf_manager.market_group = group
        windows = wf_manager.iter_windows()
        ranking_indexes, purged_indexes, validation_indexes = _partition_window_indexes(
            windows, constraints
        )
        ranking_windows = [windows[index] for index in ranking_indexes]
        if len(ranking_windows) != 11:
            raise RuntimeError(
                f"{group} produced {len(ranking_windows)} ranking windows, expected 11"
            )

        evaluator = FastEvaluator(constraints.execution, group)
        batch_size = reduce(math.gcd, depths)
        searched, service, gate_pipeline, problem, solver_config, _solver = (
            run_ranking_benchmark_search(
                strategy=strategy,
                constraints=constraints,
                manager=wf_manager,
                evaluator=evaluator,
                ranking_windows=ranking_windows,
                group=group,
                search_depth=depths[-1],
                random_seed=seed,
                evaluation_workers=max(
                    1,
                    int(constraints.search.workers or ResourcePlanner().physical_cores),
                ),
                input_fingerprints={
                    "stocks": {
                        code: frame_fingerprint(frame)
                        for code, frame in stocks_data.items()
                    },
                    "benchmarks": {
                        code: frame_fingerprint(frame)
                        for code, frame in benchmarks.items()
                    },
                },
                solver_id="random",
                batch_size=batch_size,
            )
        )
        results_by_id = {item.candidate_id: item for item in searched}
        if set(service.evaluation_order) != set(results_by_id):
            raise RuntimeError("depth benchmark lost evaluated candidates")
        outcomes = [
            _candidate_outcome(results_by_id[candidate_id])
            for candidate_id in service.evaluation_order
        ]
        events_by_depth = {
            int(event["requested_candidates"]): event
            for event in service.progress_events
        }
        records: list[dict[str, object]] = []
        for depth in depths:
            event = events_by_depth.get(depth)
            if event is None:
                raise RuntimeError(f"no progress event at search depth {depth}")
            records.append(
                summarize_prefix(
                    outcomes,
                    depth,
                    int(event["unique_evaluations"]),
                    float(event["wall_seconds"]),
                )
            )

        market_records[group] = records
        market_metadata[group] = {
            "configured_codes": configured_groups[group],
            "evaluated_codes": list(wf_manager.stock_codes),
            "missing_or_short_history_codes": missing_codes,
            "ranking_window_count": len(ranking_indexes),
            "purged_window_count": len(purged_indexes),
            "holdout_window_count": len(validation_indexes),
            "first_ranking_test_start": windows[ranking_indexes[0]].test_start_date,
            "last_ranking_test_end": windows[ranking_indexes[-1]].test_end_date,
            "solver_id": "random",
            "solver_config": solver_config,
            "gate_profile": gate_pipeline.profile_id,
            "search_contract_hash": problem.contract_hash,
        }

    aggregate, balance_depth = aggregate_market_records(
        depths,
        market_records,
        threshold_pct=threshold_pct,
    )
    flat_market_rows = []
    for group, rows in market_records.items():
        for row in rows:
            flat_market_rows.append({"market": group, **row})

    aggregate_csv = output_dir / "search_depth_summary.csv"
    market_csv = output_dir / "search_depth_by_market.csv"
    chart_path = output_dir / "search_depth_marginal_effect.png"
    json_path = output_dir / "search_depth_result.json"
    _write_csv(aggregate_csv, aggregate)
    _write_csv(market_csv, flat_market_rows)
    _plot_result(
        chart_path,
        depths,
        market_records,
        aggregate,
        balance_depth,
        threshold_pct,
    )
    payload = {
        "strategy_id": strategy_name,
        "created_at": datetime.now().isoformat(),
        "experiment": {
            "variable": "random_solver_candidate_evaluations",
            "solver_id": "random",
            "nested_prefixes": True,
            "depths": depths,
            "random_seed": seed,
            "threshold_pct": threshold_pct,
            "ranking_only": True,
            "holdout_consulted": False,
            "production_phase1_random_samples": (
                constraints.genetic_search.phase1_random_samples
            ),
            "production_phase1_top_keep": (constraints.genetic_search.phase1_top_keep),
            "production_total_configured_evaluations": (
                constraints.genetic_search.phase1_random_samples
                + constraints.genetic_search.num_generations
                * constraints.genetic_search.offspring_size
            ),
        },
        "market_metadata": market_metadata,
        "market_records": market_records,
        "aggregate_records": aggregate,
        "balance_depth": balance_depth,
        "artifacts": {
            "aggregate_csv": str(aggregate_csv.resolve()),
            "market_csv": str(market_csv.resolve()),
            "chart": str(chart_path.resolve()),
        },
    }
    payload["artifacts"]["json"] = str(json_path.resolve())
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload
