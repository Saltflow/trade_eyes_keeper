"""Parallel, publication-free benchmark for registered technical strategies.

The benchmark intentionally mirrors the search-depth experiment: every
strategy receives the same deterministic phase-one candidate budget and only
the 11 ranking windows may select parameters.  The two overlapping windows and
the final holdout are evaluated once after selection and never feed back into
the search.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .backtester import FastEvaluator, WalkForwardManager
from .config import WindowStats, get_constraints
from .helpers import _detect_fine_group, get_skip_search
from .benchmark_search import run_ranking_benchmark_search
from .optimizer import (
    StrategyEncoding,
    _evaluate_encoding_wf,
    _partition_window_indexes,
)
from .search_interface import Params
from .strategies import get_strategy

logger = logging.getLogger(__name__)

BENCHMARK_GROUPS = ("a_share", "hk", "us")


@dataclass
class BenchmarkCandidate:
    """One phase-one candidate with ranking-only selection diagnostics."""

    encoding: StrategyEncoding
    ranking_stats: list[WindowStats]
    wf_score: float
    ranking_diagnostics: dict[str, float | int]
    return_gate_passed: bool
    hard_constraints_passed: bool

    @property
    def ranking_eligible(self) -> bool:
        return self.return_gate_passed and self.hard_constraints_passed


def select_benchmark_candidate(
    candidates: Iterable[BenchmarkCandidate],
) -> tuple[BenchmarkCandidate, str]:
    """Select the best eligible candidate, or raw best for diagnostics.

    A benchmark must remain informative when a strategy produces no deployable
    candidate at the chosen budget.  In that case the raw winner is reported
    explicitly as non-eligible rather than silently disappearing.
    """

    pool = list(candidates)
    if not pool:
        raise ValueError("benchmark produced no evaluable candidate")
    eligible = [candidate for candidate in pool if candidate.ranking_eligible]
    if eligible:
        return max(eligible, key=lambda item: item.wf_score), "best_eligible"
    return max(pool, key=lambda item: item.wf_score), "best_raw_no_eligible"


def summarize_windows(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate already-serialized windows without changing metric meaning."""

    rows = list(records)
    if not rows:
        return {
            "window_count": 0,
            "mean_return_pct": None,
            "mean_excess_pct": None,
            "worst_excess_pct": None,
            "winning_windows": 0,
            "worst_drawdown_pct": None,
            "mean_sharpe": None,
            "total_trades": 0,
            "mean_final_asset": None,
        }

    def mean(name: str) -> float:
        return float(np.mean([float(row[name]) for row in rows]))

    return {
        "window_count": len(rows),
        "mean_return_pct": mean("strategy_return_pct"),
        "mean_excess_pct": mean("strongest_benchmark_excess_pct"),
        "worst_excess_pct": min(
            float(row["strongest_benchmark_excess_pct"]) for row in rows
        ),
        "winning_windows": sum(
            float(row["strongest_benchmark_excess_pct"]) > 0.0 for row in rows
        ),
        "worst_drawdown_pct": min(float(row["max_drawdown_pct"]) for row in rows),
        "mean_sharpe": mean("sharpe_ratio"),
        "total_trades": sum(int(row["trade_count"]) for row in rows),
        "mean_final_asset": mean("final_asset"),
    }


def aggregate_strategy_results(
    market_results: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build equal-market strategy comparisons from market-level results."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in market_results:
        grouped.setdefault(str(result["strategy_id"]), []).append(result)

    rows = []
    for strategy_id, markets in sorted(grouped.items()):
        ranking = [market["ranking_summary"] for market in markets]
        holdout = [market["holdout_summary"] for market in markets]
        rows.append(
            {
                "strategy_id": strategy_id,
                "market_count": len(markets),
                "search_depth_per_market": int(markets[0]["search_depth"]),
                "mean_wf_score": float(
                    np.mean([float(market["wf_score"]) for market in markets])
                ),
                "mean_ranking_return_pct": float(
                    np.mean([float(item["mean_return_pct"]) for item in ranking])
                ),
                "mean_ranking_excess_pct": float(
                    np.mean([float(item["mean_excess_pct"]) for item in ranking])
                ),
                "ranking_wins": sum(int(item["winning_windows"]) for item in ranking),
                "ranking_windows": sum(int(item["window_count"]) for item in ranking),
                "ranking_eligible_markets": sum(
                    bool(market["selected_candidate"]["ranking_eligible"])
                    for market in markets
                ),
                "mean_holdout_return_pct": float(
                    np.mean([float(item["mean_return_pct"]) for item in holdout])
                ),
                "mean_holdout_excess_pct": float(
                    np.mean([float(item["mean_excess_pct"]) for item in holdout])
                ),
                "holdout_wins": sum(int(item["winning_windows"]) for item in holdout),
                "holdout_windows": sum(int(item["window_count"]) for item in holdout),
                "elapsed_seconds_sum": sum(
                    float(market["elapsed_seconds"]) for market in markets
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            item["mean_holdout_excess_pct"],
            item["mean_ranking_excess_pct"],
        ),
        reverse=True,
    )
    return rows


def _configured_codes(config: dict) -> dict[str, list[str]]:
    from main import _stock_code

    skipped = get_skip_search(config)
    groups = {group: [] for group in BENCHMARK_GROUPS}
    for stock in config.get("stocks", []) or []:
        code = _stock_code(stock)
        if not code or code in skipped:
            continue
        group = _detect_fine_group(code)
        if group in groups:
            groups[group].append(code)
    return groups


def frame_fingerprint(frame: pd.DataFrame) -> str:
    """Hash the dated OHLC input used by every strategy in one process."""

    columns = [
        column
        for column in ("date", "open", "high", "low", "close")
        if column in frame.columns
    ]
    normalized = frame.loc[:, columns].copy()
    normalized.index = normalized.index.map(str)
    digest = hashlib.sha256()
    digest.update("|".join(columns).encode("utf-8"))
    digest.update(pd.util.hash_pandas_object(normalized, index=True).values.tobytes())
    return digest.hexdigest()


def prepare_benchmark_data(config: dict) -> dict[str, dict[str, Any]]:
    """Fetch each market once and return immutable worker snapshots."""

    from main import (
        _has_optimizer_history,
        _load_optimizer_benchmarks,
        _optimizer_lookback_days,
    )
    from src.data.data_source import DataSource

    constraints = get_constraints()
    data_source = DataSource(config)
    configured = _configured_codes(config)
    result = {}
    for group in BENCHMARK_GROUPS:
        constraints.set_group(group)
        lookback_days = _optimizer_lookback_days(constraints)
        stocks_data = {}
        missing = []
        for code in configured[group]:
            try:
                data = data_source.fetch_stock_data(code, days=lookback_days)
            except Exception as exc:
                logger.warning("Unable to prefetch %s: %s", code, exc)
                missing.append(code)
                continue
            if (
                data is None
                or data.empty
                or not _has_optimizer_history(data, constraints)
            ):
                missing.append(code)
            else:
                stocks_data[code] = data.copy(deep=True)
        benchmarks = _load_optimizer_benchmarks(
            data_source, constraints, group, lookback_days
        )
        result[group] = {
            "configured_codes": list(configured[group]),
            "stocks_data": stocks_data,
            "missing_or_short_history_codes": missing,
            "benchmarks": {
                code: data.copy(deep=True) for code, data in benchmarks.items()
            },
        }
    return result


def summarize_prepared_data(
    prepared: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return a JSON-safe inventory without serializing DataFrames."""

    return {
        group: {
            "available_codes": sorted(snapshot["stocks_data"]),
            "missing_or_short_history_codes": list(
                snapshot["missing_or_short_history_codes"]
            ),
            "benchmark_codes": sorted(snapshot["benchmarks"]),
        }
        for group, snapshot in prepared.items()
    }


def _holding_rows(stat: WindowStats, codes: list[str]) -> list[dict[str, Any]]:
    shares = np.asarray(getattr(stat, "final_shares", []), dtype=float)
    prices = np.asarray(getattr(stat, "final_prices", []), dtype=float)
    costs = np.asarray(getattr(stat, "cost_basis", []), dtype=float)
    final_asset = float(getattr(stat, "final_asset", 0.0) or 0.0)
    if not (len(shares) == len(prices) == len(costs) == len(codes)):
        return []
    result = []
    for code, quantity, price, cost in zip(codes, shares, prices, costs):
        if quantity <= 0 or not np.isfinite(price) or price <= 0:
            continue
        value = float(quantity * price)
        result.append(
            {
                "code": code,
                "shares": float(quantity),
                "cost": float(cost),
                "price": float(price),
                "value": value,
                "weight_pct": value / final_asset * 100.0 if final_asset else 0.0,
                "pnl": float((price - cost) * quantity),
            }
        )
    return result


def _serialize_window(
    index: int,
    partition: str,
    stat: WindowStats,
    window: Any,
    codes: list[str],
) -> dict[str, Any]:
    return {
        "window": index + 1,
        "partition": partition,
        "period": {
            "train_start": window.train_start_date,
            "train_end": window.train_end_date,
            "test_start": window.test_start_date,
            "test_end": window.test_end_date,
        },
        "strategy_return_pct": float(stat.strategy_return),
        "strongest_benchmark_excess_pct": float(stat.test_excess_return),
        "strongest_benchmark": str(stat.strongest_benchmark),
        "benchmark_returns": {
            str(name): float(value) for name, value in stat.benchmark_returns.items()
        },
        "max_drawdown_pct": float(stat.max_drawdown_pct),
        "sharpe_ratio": float(stat.sharpe_ratio),
        "trade_count": int(stat.total_trades),
        "initial_asset": float(stat.initial_asset),
        "final_asset": float(stat.final_asset),
        "final_cash": float(stat.final_cash),
        "final_position_pct": float(stat.final_position_pct),
        "pending_order_count": int(stat.pending_order_count),
        "final_holdings": _holding_rows(stat, codes),
    }


def run_market_benchmark(
    *,
    config: dict,
    strategy_name: str,
    group: str,
    search_depth: int = 1000,
    evaluation_workers: int = 1,
    prepared_market: dict[str, Any] | None = None,
    solver_id: str = "random",
    solver_config: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Benchmark one strategy/market pair without writing optimizer artifacts."""

    started = monotonic()
    strategy = get_strategy(strategy_name)
    if strategy is None:
        raise ValueError(f"unknown registered strategy: {strategy_name}")
    if group not in BENCHMARK_GROUPS:
        raise ValueError(f"unknown benchmark group: {group}")
    if search_depth <= 0:
        raise ValueError("search_depth must be positive")

    constraints = get_constraints()
    constraints.set_group(group)
    constraints.genetic_search.evaluation_workers = max(1, int(evaluation_workers))
    seed = constraints.genetic_search.random_seed
    if seed is None:
        raise ValueError("benchmark requires a fixed random seed")

    if prepared_market is None:
        from main import (
            _has_optimizer_history,
            _load_optimizer_benchmarks,
            _optimizer_lookback_days,
        )
        from src.data.data_source import DataSource

        configured = _configured_codes(config)[group]
        lookback_days = _optimizer_lookback_days(constraints)
        data_source = DataSource(config)
        stocks_data = {}
        missing_codes = []
        for code in configured:
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
        benchmarks = _load_optimizer_benchmarks(
            data_source, constraints, group, lookback_days
        )
    else:
        configured = list(prepared_market["configured_codes"])
        stocks_data = dict(prepared_market["stocks_data"])
        missing_codes = list(prepared_market["missing_or_short_history_codes"])
        benchmarks = dict(prepared_market["benchmarks"])
    if not stocks_data:
        raise RuntimeError(f"no full-horizon market data for {group}")

    stock_fingerprints = {
        code: frame_fingerprint(data) for code, data in stocks_data.items()
    }
    benchmark_fingerprints = {
        code: frame_fingerprint(data) for code, data in benchmarks.items()
    }
    manager = WalkForwardManager(
        stocks_data,
        constraints,
        list(stocks_data),
        benchmark_data=benchmarks,
    )
    manager.market_group = group
    windows = manager.iter_windows()
    ranking_indexes, purged_indexes, validation_indexes = _partition_window_indexes(
        windows, constraints
    )
    if (
        len(windows) != 14
        or len(ranking_indexes) != 11
        or len(purged_indexes) != 2
        or len(validation_indexes) != 1
    ):
        raise RuntimeError(
            "benchmark requires the authoritative 11+2+1 window partition"
        )
    ranking_windows = [windows[index] for index in ranking_indexes]
    evaluator = FastEvaluator(constraints.execution, group)
    (
        searched,
        service,
        gate_pipeline,
        search_problem,
        effective_solver_config,
    ) = run_ranking_benchmark_search(
        strategy=strategy,
        constraints=constraints,
        manager=manager,
        evaluator=evaluator,
        ranking_windows=ranking_windows,
        group=group,
        search_depth=search_depth,
        random_seed=seed,
        evaluation_workers=evaluation_workers,
        input_fingerprints={
            "stocks": stock_fingerprints,
            "benchmarks": benchmark_fingerprints,
        },
        solver_id=solver_id,
        solver_config=solver_config,
    )
    candidates = []
    for item in searched:
        params = Params(
            values=dict(item.parameters),
            _engine=strategy.name,
        )
        encoding = StrategyEncoding(
            genome=[int(params.values[dim.name]) for dim in strategy.param_space.dims],
            engine_name=strategy.name,
            params=params,
        )
        candidates.append(
            BenchmarkCandidate(
                encoding=encoding,
                ranking_stats=item.ranking_stats,
                wf_score=float(item.objective_score),
                ranking_diagnostics=dict(item.ranking_metrics),
                return_gate_passed=bool(item.gate_feasible),
                hard_constraints_passed=bool(item.gate_feasible),
            )
        )
    selected, selection_basis = select_benchmark_candidate(candidates)
    final_result = _evaluate_encoding_wf(
        selected.encoding,
        strategy,
        windows,
        constraints.discrete_search,
        constraints,
        evaluator,
        manager,
    )
    if final_result is None:
        raise RuntimeError("selected candidate failed full-window evaluation")
    all_stats, final_ranking_stats, validation_stats, wf_score = final_result

    partition_by_index = {
        **{index: "ranking" for index in ranking_indexes},
        **{index: "isolated" for index in purged_indexes},
        **{index: "holdout" for index in validation_indexes},
    }
    records = [
        _serialize_window(
            index,
            partition_by_index[index],
            stat,
            windows[index],
            list(manager.stock_codes),
        )
        for index, stat in enumerate(all_stats)
    ]
    ranking_records = [record for record in records if record["partition"] == "ranking"]
    isolated_records = [
        record for record in records if record["partition"] == "isolated"
    ]
    holdout_records = [record for record in records if record["partition"] == "holdout"]
    params = selected.encoding.to_params(strategy)
    return {
        "strategy_id": strategy_name,
        "strategy_label": strategy.label,
        "market": group,
        "search_depth": int(search_depth),
        "random_seed": int(seed),
        "solver_id": solver_id,
        "solver_config": effective_solver_config,
        "performance": service.performance_snapshot(),
        "gate_profile": gate_pipeline.profile_id,
        "gate_profile_hash": gate_pipeline.hash,
        "search_contract_hash": search_problem.contract_hash,
        "evaluation_workers": int(evaluation_workers),
        "configured_codes": configured,
        "evaluated_codes": list(manager.stock_codes),
        "missing_or_short_history_codes": missing_codes,
        "input_fingerprints": {
            "stocks": stock_fingerprints,
            "benchmarks": benchmark_fingerprints,
        },
        "unique_evaluations": len(service.cache),
        "evaluable_candidates": len(candidates),
        "return_gate_pass_count": sum(
            candidate.return_gate_passed for candidate in candidates
        ),
        "hard_constraint_pass_count": sum(
            candidate.hard_constraints_passed for candidate in candidates
        ),
        "eligible_candidate_count": sum(
            candidate.ranking_eligible for candidate in candidates
        ),
        "selection_basis": selection_basis,
        "wf_score": float(wf_score),
        "selected_candidate": {
            "params": {str(key): int(value) for key, value in params.values.items()},
            "execution": strategy.execution_params(params),
            "ranking_eligible": selected.ranking_eligible,
            "return_gate_passed": selected.return_gate_passed,
            "hard_constraints_passed": selected.hard_constraints_passed,
            "ranking_diagnostics": dict(selected.ranking_diagnostics),
        },
        "ranking_summary": summarize_windows(ranking_records),
        "isolated_summary": summarize_windows(isolated_records),
        "holdout_summary": summarize_windows(holdout_records),
        "windows": records,
        "full_window_counts": {
            "total": len(all_stats),
            "ranking": len(final_ranking_stats),
            "isolated": len(isolated_records),
            "holdout": len(validation_stats),
        },
        "elapsed_seconds": monotonic() - started,
    }


def run_market_benchmark_from_local_config(
    strategy_name: str,
    group: str,
    search_depth: int,
    evaluation_workers: int,
    solver_id: str = "random",
    solver_config: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Pickle-safe worker entry used by the Windows process pool."""

    from main import load_config

    return run_market_benchmark(
        config=load_config(),
        strategy_name=strategy_name,
        group=group,
        search_depth=search_depth,
        evaluation_workers=evaluation_workers,
        solver_id=solver_id,
        solver_config=solver_config,
    )


def run_market_benchmark_from_snapshot(
    strategy_name: str,
    group: str,
    prepared_market: dict[str, Any],
    search_depth: int,
    evaluation_workers: int,
    solver_id: str = "random",
    solver_config: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Pickle-safe worker that cannot call or mutate the data-source cache."""

    return run_market_benchmark(
        config={},
        strategy_name=strategy_name,
        group=group,
        search_depth=search_depth,
        evaluation_workers=evaluation_workers,
        prepared_market=prepared_market,
        solver_id=solver_id,
        solver_config=solver_config,
    )


def write_benchmark_artifacts(
    *,
    output_dir: Path,
    market_results: list[dict[str, Any]],
    search_depth: int,
    market_workers: int,
    evaluation_workers: int,
    wall_seconds: float,
    prefetch_summary: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Write canonical JSON and two compact CSV views."""

    output_dir.mkdir(parents=True, exist_ok=True)
    strategy_summary = aggregate_strategy_results(market_results)
    payload = {
        "created_at": datetime.now().isoformat(),
        "contract": {
            "technical_only": True,
            "search_depth_per_strategy_market": int(search_depth),
            "selection_windows": 11,
            "isolated_windows": 2,
            "holdout_windows": 1,
            "benchmarks": [
                "risk_free",
                "510300",
                "universe_equal_weight",
            ],
            "selection_uses_holdout": False,
            "market_weighting": "equal",
            "solver_ids": sorted({item["solver_id"] for item in market_results}),
        },
        "parallelism": {
            "market_workers": int(market_workers),
            "evaluation_workers_per_job": int(evaluation_workers),
        },
        "prefetch_summary": prefetch_summary or {},
        "wall_seconds": float(wall_seconds),
        "strategy_summary": strategy_summary,
        "market_results": sorted(
            market_results,
            key=lambda item: (item["strategy_id"], item["market"]),
        ),
    }
    json_path = output_dir / "technical_strategy_benchmark.json"
    strategy_csv = output_dir / "strategy_summary.csv"
    market_csv = output_dir / "market_summary.csv"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with strategy_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(strategy_summary[0]) if strategy_summary else [],
        )
        if strategy_summary:
            writer.writeheader()
            writer.writerows(strategy_summary)

    market_rows = []
    for result in market_results:
        market_rows.append(
            {
                "strategy_id": result["strategy_id"],
                "market": result["market"],
                "wf_score": result["wf_score"],
                "eligible_candidate_count": result["eligible_candidate_count"],
                "selection_basis": result["selection_basis"],
                "ranking_eligible": result["selected_candidate"]["ranking_eligible"],
                "ranking_mean_return_pct": result["ranking_summary"]["mean_return_pct"],
                "ranking_mean_excess_pct": result["ranking_summary"]["mean_excess_pct"],
                "ranking_wins": result["ranking_summary"]["winning_windows"],
                "holdout_return_pct": result["holdout_summary"]["mean_return_pct"],
                "holdout_excess_pct": result["holdout_summary"]["mean_excess_pct"],
                "holdout_wins": result["holdout_summary"]["winning_windows"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
        )
    with market_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(market_rows[0]) if market_rows else [],
        )
        if market_rows:
            writer.writeheader()
            writer.writerows(
                sorted(
                    market_rows,
                    key=lambda item: (item["strategy_id"], item["market"]),
                )
            )

    return {
        "json": str(json_path.resolve()),
        "strategy_csv": str(strategy_csv.resolve()),
        "market_csv": str(market_csv.resolve()),
    }
