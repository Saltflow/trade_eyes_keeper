"""Solver-blind strategy and execution evaluation service."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

import numpy as np
from joblib import Parallel, delayed

from .gates import aggregate_ranking_metrics
from .workflow import (
    _compute_ranking_wf_score,
    _evaluate_params_wf,
    _prepare_wf_evaluation_contexts,
)
from .contracts import (
    CandidateBatch,
    EvaluationBatch,
    EvaluatorCapabilities,
    GateDecision,
)
from ..strategy import Params


@dataclass
class EvaluationRecord:
    candidate_id: str
    parameters: dict[str, object]
    all_stats: list[object]
    ranking_stats: list[object]
    objective_score: float
    raw_metrics: dict[str, object]
    failure_reasons: tuple[str, ...] = ()


class EvaluationService:
    """Evaluate candidates without exposing strategy internals to solvers."""

    capabilities = EvaluatorCapabilities(
        backends=("cpu_scalar", "cpu_batch"),
        active_backend="cpu_batch",
        batched=True,
        gradients=False,
        gpu=False,
    )

    def __init__(
        self,
        strategy,
        constraints,
        wf_manager,
        evaluator,
        ranking_windows: list,
        workers: int = 1,
    ):
        self.strategy = strategy
        self.constraints = constraints
        self.wf_manager = wf_manager
        self.evaluator = evaluator
        self.ranking_windows = list(ranking_windows)
        self.workers = max(1, int(workers))
        self.cache: dict[tuple[object, ...], EvaluationRecord] = {}
        self.records: dict[str, EvaluationRecord] = {}
        self._prepared_kernel = None
        self._prepared_checked = False
        self._wall_started = perf_counter()
        self.evaluation_order: list[str] = []
        self.progress_events: list[dict[str, float | int]] = []
        self.timings = {
            "scoring_seconds": 0.0,
            "simulation_seconds": 0.0,
            "scheduling_seconds": 0.0,
            "cache_hits": 0,
            "evaluated": 0,
        }

    def performance_snapshot(self) -> dict[str, object]:
        """Return serializable timing and cache telemetry for artifacts."""
        snapshot = dict(self.timings)
        total = int(snapshot["evaluated"]) + int(snapshot["cache_hits"])
        snapshot["cache_hit_rate"] = (
            float(snapshot["cache_hits"]) / total if total else 0.0
        )
        snapshot["evaluator_capabilities"] = self.capabilities.__dict__
        return snapshot

    def _key(self, parameters: dict[str, object]) -> tuple[object, ...]:
        return tuple(parameters[name] for name in sorted(parameters))

    def _params(self, parameters: dict[str, object]) -> Params:
        return Params(values=dict(parameters), _engine=self.strategy.name)

    def evaluate_one(
        self, candidate_id: str, parameters: dict[str, object]
    ) -> EvaluationRecord:
        key = self._key(parameters)
        cached = self.cache.get(key)
        if cached is not None:
            self.timings["cache_hits"] += 1
            record = EvaluationRecord(
                candidate_id,
                dict(parameters),
                cached.all_stats,
                cached.ranking_stats,
                cached.objective_score,
                cached.raw_metrics,
                cached.failure_reasons,
            )
            self.records[candidate_id] = record
            return record
        params = self._params(parameters)
        started = perf_counter()
        result = _evaluate_params_wf(
            params,
            self.strategy,
            self.ranking_windows,
            self.constraints,
            self.evaluator,
            self.wf_manager,
            validation_window_count=0,
        )
        self.timings["simulation_seconds"] += perf_counter() - started
        self.timings["evaluated"] += 1
        if result is None:
            record = EvaluationRecord(
                candidate_id,
                dict(parameters),
                [],
                [],
                -float("inf"),
                {},
                ("evaluation_failed",),
            )
        else:
            all_stats, ranking_stats, _validation, score = result
            metrics = aggregate_ranking_metrics(
                ranking_stats,
                score,
                self.constraints.walk_forward.ranking_weights(len(ranking_stats)),
            )
            record = EvaluationRecord(
                candidate_id,
                dict(parameters),
                all_stats,
                ranking_stats,
                float(score),
                metrics,
            )
        self.cache[key] = record
        self.records[candidate_id] = record
        return record

    def evaluate_batch(self, candidates: CandidateBatch) -> EvaluationBatch:
        started = perf_counter()
        requests = [
            (candidates.candidate_ids[index], candidates.parameters_at(index))
            for index in range(len(candidates))
        ]
        self.evaluation_order.extend(candidate_id for candidate_id, _params in requests)
        records_by_index: list[EvaluationRecord | None] = [None] * len(requests)
        missing = []
        for index, (candidate_id, parameters) in enumerate(requests):
            cached = self.cache.get(self._key(parameters))
            if cached is None:
                missing.append((index, candidate_id, parameters))
                continue
            self.timings["cache_hits"] += 1
            record = EvaluationRecord(
                candidate_id,
                dict(parameters),
                cached.all_stats,
                cached.ranking_stats,
                cached.objective_score,
                cached.raw_metrics,
                cached.failure_reasons,
            )
            records_by_index[index] = record
            self.records[candidate_id] = record

        prepared_records = self._evaluate_prepared(missing)
        if prepared_records is None:
            if self.workers > 1 and len(missing) > 1:
                produced = Parallel(
                    n_jobs=self.workers,
                    prefer="threads",
                    batch_size=max(1, min(16, len(missing) // self.workers)),
                )(
                    delayed(self.evaluate_one)(candidate_id, parameters)
                    for _index, candidate_id, parameters in missing
                )
            else:
                produced = [
                    self.evaluate_one(candidate_id, parameters)
                    for _index, candidate_id, parameters in missing
                ]
        else:
            produced = prepared_records
        for (index, candidate_id, parameters), record in zip(missing, produced):
            records_by_index[index] = record
            self.cache[self._key(parameters)] = record
            self.records[candidate_id] = record
        records = [record for record in records_by_index if record is not None]
        self.timings["scheduling_seconds"] += perf_counter() - started
        self.progress_events.append(
            {
                "requested_candidates": len(self.evaluation_order),
                "unique_evaluations": len(self.cache),
                "wall_seconds": perf_counter() - self._wall_started,
            }
        )
        neutral = tuple(
            GateDecision(feasible=not record.failure_reasons) for record in records
        )
        return EvaluationBatch(
            candidate_ids=tuple(record.candidate_id for record in records),
            raw_metrics=tuple(record.raw_metrics for record in records),
            objective_scores=np.asarray(
                [record.objective_score for record in records], dtype=np.float64
            ),
            gate_decisions=neutral,
            feasible=np.asarray(
                [not record.failure_reasons for record in records], dtype=bool
            ),
            failure_reasons=tuple(record.failure_reasons for record in records),
        )

    def _evaluate_prepared(self, missing):
        """Use a strategy's optional columnar signal kernel when available."""
        state_scope = str(getattr(self.strategy, "window_state_scope", "continuous"))
        if not missing or state_scope == "train":
            return None
        prepare = getattr(self.strategy, "prepare", None)
        if not callable(prepare):
            return None
        contexts = _prepare_wf_evaluation_contexts(
            self.ranking_windows,
            "continuous",
            self.constraints,
            self.evaluator,
            self.wf_manager,
        )
        started = perf_counter()
        if not self._prepared_checked:
            self._prepared_kernel = prepare(contexts["full_market_data"])
            self._prepared_checked = True
        kernel = self._prepared_kernel
        evaluate_batch = getattr(kernel, "evaluate_batch", None)
        if not callable(evaluate_batch):
            return None
        params = [self._params(parameters) for _index, _id, parameters in missing]
        plans = evaluate_batch(params)
        if len(plans) != len(missing):
            raise ValueError("prepared strategy returned the wrong plan count")
        self.timings["scoring_seconds"] += perf_counter() - started
        stats_by_candidate = [[] for _plan in plans]
        simulation_started = perf_counter()
        for window, context in zip(self.ranking_windows, contexts["windows"]):
            window_plans = [
                plan.sliced(window.test_start, window.test_end) for plan in plans
            ]
            window_stats = self.evaluator.evaluate_batch(
                window_plans,
                workers=self.workers,
                indicator_matrix=context["test_indicators"],
                price_matrix=context["test_prices"],
                cash_baseline=context["cash_baseline"],
                execution_prices=context["execution_prices"],
                benchmark_series=context["benchmark_series"],
                benchmark_initial_values=context["benchmark_initial_values"],
                benchmark_raw_returns=context["benchmark_raw_returns"],
            )
            for index, stat in enumerate(window_stats):
                stats_by_candidate[index].append(stat)
        self.timings["simulation_seconds"] += perf_counter() - simulation_started
        self.timings["evaluated"] += len(plans)

        records = []
        for all_stats, (_index, candidate_id, parameters) in zip(
            stats_by_candidate, missing
        ):
            score = _compute_ranking_wf_score(all_stats, self.constraints)
            if score is None:
                records.append(
                    EvaluationRecord(
                        candidate_id,
                        dict(parameters),
                        [],
                        [],
                        -float("inf"),
                        {},
                        ("evaluation_failed",),
                    )
                )
                continue
            metrics = aggregate_ranking_metrics(
                all_stats,
                score,
                self.constraints.walk_forward.ranking_weights(len(all_stats)),
            )
            records.append(
                EvaluationRecord(
                    candidate_id,
                    dict(parameters),
                    all_stats,
                    all_stats,
                    float(score),
                    metrics,
                )
            )
        return records
