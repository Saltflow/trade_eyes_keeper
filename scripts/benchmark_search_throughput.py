#!/usr/bin/env python3
"""Benchmark scalar versus prepared CPU search evaluation on full A-share data."""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import (  # noqa: E402
    _has_optimizer_history,
    _load_optimizer_benchmarks,
    _optimizer_lookback_days,
    load_config,
)
from src.backtest.engine import FastEvaluator, WalkForwardManager  # noqa: E402
from src.search.config import get_constraints  # noqa: E402
from src.search.evaluator import EvaluationService  # noqa: E402
from src.search.workflow import _partition_window_indexes  # noqa: E402
from src.search.resources import ResourcePlanner  # noqa: E402
from src.search.contracts import Candidate, CandidateBatch  # noqa: E402
from src.strategy import get_strategy  # noqa: E402
from src.experiments.strategy_benchmark import _configured_codes  # noqa: E402
from src.data.data_source import DataSource  # noqa: E402


class _ScalarOnlyStrategy:
    """Delegate correctness behavior while deliberately hiding ``prepare``."""

    prepare = None

    def __init__(self, strategy):
        self._strategy = strategy

    def __getattr__(self, name):
        return getattr(self._strategy, name)

    def make_signals(self, params, market_data):
        return self._strategy.make_signals(params, market_data)


class _PeakRSS:
    def __init__(self):
        self.process = psutil.Process()
        self.peak = self.process.memory_info().rss
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self.stop.wait(0.02):
            self.peak = max(self.peak, self.process.memory_info().rss)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join()
        self.peak = max(self.peak, self.process.memory_info().rss)


def _prepare_a_share(config: dict, constraints):
    configured = _configured_codes(config)["a_share"]
    data_source = DataSource(config)
    lookback_days = _optimizer_lookback_days(constraints)
    stocks_data = {}
    missing = []
    for code in configured:
        data = data_source.fetch_stock_data(code, days=lookback_days)
        if data is None or data.empty or not _has_optimizer_history(data, constraints):
            missing.append(code)
        else:
            stocks_data[code] = data
    if not stocks_data:
        raise RuntimeError("no full-horizon A-share data available")
    benchmarks = _load_optimizer_benchmarks(
        data_source,
        constraints,
        "a_share",
        lookback_days,
    )
    return configured, missing, stocks_data, benchmarks


def _candidate_rows(strategy, count: int, seed: int) -> list[Candidate]:
    schema = strategy.parameter_schema
    rng = random.Random(seed)
    return [
        Candidate.create(
            schema.sample(rng),
            schema,
            source="throughput/frozen-random",
            nonce=str(index),
        )
        for index in range(count)
    ]


def _reset_service(service: EvaluationService) -> None:
    service.cache.clear()
    service.records.clear()
    for key in service.timings:
        service.timings[key] = 0 if key in {"cache_hits", "evaluated"} else 0.0


def _run_service(
    service: EvaluationService,
    candidates: list[Candidate],
    schema,
    batch_size: int,
) -> tuple[dict[str, float], float, float]:
    process = psutil.Process()
    before_cpu = sum(process.cpu_times()[:2])
    started = time.perf_counter()
    scores = {}
    with _PeakRSS() as memory:
        for start in range(0, len(candidates), batch_size):
            batch = CandidateBatch.from_candidates(
                candidates[start : start + batch_size],
                schema,
            )
            evaluated = service.evaluate_batch(batch)
            scores.update(
                zip(evaluated.candidate_ids, evaluated.objective_scores.tolist())
            )
    elapsed = time.perf_counter() - started
    cpu_seconds = sum(process.cpu_times()[:2]) - before_cpu
    physical_cores = max(1, psutil.cpu_count(logical=False) or 1)
    cpu_utilization = cpu_seconds / max(elapsed * physical_cores, 1e-12) * 100.0
    return scores, elapsed, memory.peak / (1024**3), cpu_utilization


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare scalar and prepared CPU evaluation for identical frozen "
            "technical_ensemble candidates on all full-history A-share symbols."
        )
    )
    parser.add_argument("--candidates", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20260801)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/analysis/search_throughput"),
    )
    parser.add_argument("--enforce", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.candidates <= 0:
        raise SystemExit("--candidates must be positive")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    constraints = get_constraints()
    constraints.set_group("a_share")
    configured, missing, stocks_data, benchmarks = _prepare_a_share(
        load_config(), constraints
    )
    manager = WalkForwardManager(
        stocks_data,
        constraints,
        list(stocks_data),
        benchmark_data=benchmarks,
    )
    manager.market_group = "a_share"
    windows = manager.iter_windows()
    ranking_indexes, isolated_indexes, holdout_indexes = _partition_window_indexes(
        windows, constraints
    )
    if (len(ranking_indexes), len(isolated_indexes), len(holdout_indexes)) != (
        11,
        2,
        1,
    ):
        raise RuntimeError("throughput benchmark requires the 11+2+1 contract")
    ranking_windows = [windows[index] for index in ranking_indexes]
    strategy = get_strategy("technical_ensemble")
    schema = strategy.parameter_schema
    candidates = _candidate_rows(strategy, args.candidates, args.seed)
    plan = ResourcePlanner().plan(
        "candidate_window",
        batch_size=args.batch_size,
    )
    evaluator = FastEvaluator(constraints.execution, "a_share")
    scalar = EvaluationService(
        _ScalarOnlyStrategy(strategy),
        constraints,
        manager,
        evaluator,
        ranking_windows,
        workers=1,
    )
    batched = EvaluationService(
        strategy,
        constraints,
        manager,
        evaluator,
        ranking_windows,
        workers=plan.outer_workers,
    )

    warmup = CandidateBatch.from_candidates([candidates[0]], schema)
    scalar.evaluate_batch(warmup)
    batched.evaluate_batch(warmup)
    _reset_service(scalar)
    _reset_service(batched)

    with ResourcePlanner().apply(plan):
        scalar_scores, scalar_seconds, scalar_rss, scalar_cpu = _run_service(
            scalar,
            candidates,
            schema,
            1,
        )
        batch_scores, batch_seconds, batch_rss, batch_cpu = _run_service(
            batched,
            candidates,
            schema,
            plan.batch_size,
        )
    if scalar_scores.keys() != batch_scores.keys():
        raise RuntimeError("scalar and batch candidate IDs differ")
    max_score_delta = max(
        abs(scalar_scores[key] - batch_scores[key]) for key in scalar_scores
    )
    speedup = scalar_seconds / max(batch_seconds, 1e-12)
    result = {
        "created_at": datetime.now().isoformat(),
        "contract": {
            "strategy_id": strategy.name,
            "market": "a_share",
            "configured_symbols": configured,
            "evaluated_symbols": list(manager.stock_codes),
            "missing_or_short_history_symbols": missing,
            "candidate_count": args.candidates,
            "candidate_seed": args.seed,
            "ranking_windows": ranking_indexes,
            "isolated_and_holdout_evaluated": False,
            "batch_size": plan.batch_size,
            "workers": plan.outer_workers,
        },
        "scalar": {
            "seconds": scalar_seconds,
            "candidates_per_second": args.candidates / scalar_seconds,
            "peak_rss_gib": scalar_rss,
            "normalized_process_cpu_pct": scalar_cpu,
            "performance": scalar.performance_snapshot(),
        },
        "batch": {
            "seconds": batch_seconds,
            "candidates_per_second": args.candidates / batch_seconds,
            "peak_rss_gib": batch_rss,
            "normalized_process_cpu_pct": batch_cpu,
            "performance": batched.performance_snapshot(),
        },
        "comparison": {
            "speedup": speedup,
            "max_objective_score_delta": max_score_delta,
            "exact_objective_match": max_score_delta == 0.0,
            "speedup_requirement_met": speedup >= 2.0,
            "rss_requirement_met": batch_rss < 1.0,
        },
    }
    output_dir = args.output_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "throughput_benchmark.json"
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result["comparison"], ensure_ascii=False, indent=2))
    print(f"json={output_path.resolve()}")
    if args.enforce and not all(
        (
            result["comparison"]["exact_objective_match"],
            result["comparison"]["speedup_requirement_met"],
            result["comparison"]["rss_requirement_met"],
        )
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
