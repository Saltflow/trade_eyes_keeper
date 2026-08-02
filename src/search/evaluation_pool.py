"""Persistent process workers for pure candidate evaluation.

The controller and Solver remain in the parent process. Workers receive one
read-only ranking-data snapshot at startup; per-task IPC contains only a
columnar ``CandidateBatch`` and compact ranking results. ``ProcessPoolExecutor``
uses multiprocessing call/result queues backed by OS pipes, providing the
bounded producer/consumer transport without exposing that transport to a
Solver.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import multiprocessing
from time import perf_counter, process_time
from typing import Any

from .contracts import CandidateBatch


@dataclass(frozen=True)
class ProcessEvaluationRow:
    """Compact result returned over IPC for one candidate."""

    candidate_id: str
    objective_score: float
    raw_metrics: dict[str, object]
    failure_reasons: tuple[str, ...]
    all_stats: tuple[object, ...] | None = None
    ranking_stats: tuple[object, ...] | None = None


@dataclass(frozen=True)
class ProcessEvaluationResult:
    """Ordered worker output plus process-side timing telemetry."""

    rows: tuple[ProcessEvaluationRow, ...]
    worker_cpu_seconds: float
    worker_wall_seconds: float
    worker_simulation_seconds: float
    task_count: int


@dataclass(frozen=True)
class _ProcessTask:
    task_id: int
    offset: int
    candidates: CandidateBatch
    materialize: bool


@dataclass(frozen=True)
class _ProcessTaskResult:
    task_id: int
    offset: int
    rows: tuple[ProcessEvaluationRow, ...]
    cpu_seconds: float
    wall_seconds: float
    simulation_seconds: float


_WORKER_SERVICE = None
_WORKER_THREAD_LIMIT = None


def _initialize_worker(
    strategy,
    constraints,
    wf_manager,
    ranking_windows: tuple[object, ...],
    market_group: str,
) -> None:
    """Build one reusable scalar evaluator around a worker-local snapshot."""

    global _WORKER_SERVICE, _WORKER_THREAD_LIMIT

    # Each candidate process is the selected parallel axis. Prevent Numba and
    # BLAS from opening nested pools that would oversubscribe the machine.
    try:
        import numba

        numba.set_num_threads(1)
    except (ImportError, RuntimeError, ValueError):
        pass
    try:
        from threadpoolctl import threadpool_limits

        _WORKER_THREAD_LIMIT = threadpool_limits(limits=1)
    except ImportError:
        _WORKER_THREAD_LIMIT = None

    from ..backtest.engine import FastEvaluator
    from .evaluator import EvaluationService

    evaluator = FastEvaluator(constraints.execution, market_group)
    _WORKER_SERVICE = EvaluationService(
        strategy,
        constraints,
        wf_manager,
        evaluator,
        list(ranking_windows),
        workers=1,
        evaluation_backend="scalar",
    )


def _evaluate_process_task(task: _ProcessTask) -> _ProcessTaskResult:
    """Evaluate one shard without accessing Solver, Gate or holdout state."""

    if _WORKER_SERVICE is None:
        raise RuntimeError("candidate evaluation worker was not initialized")
    started_wall = perf_counter()
    started_cpu = process_time()
    simulation_before = float(_WORKER_SERVICE.timings["simulation_seconds"])
    rows = []
    try:
        for index, candidate_id in enumerate(task.candidates.candidate_ids):
            parameters = task.candidates.parameters_at(index)
            record = _WORKER_SERVICE.evaluate_one(candidate_id, parameters)
            rows.append(
                ProcessEvaluationRow(
                    candidate_id=candidate_id,
                    objective_score=float(record.objective_score),
                    raw_metrics=dict(record.raw_metrics),
                    failure_reasons=tuple(record.failure_reasons),
                    all_stats=(tuple(record.all_stats) if task.materialize else None),
                    ranking_stats=(
                        tuple(record.ranking_stats) if task.materialize else None
                    ),
                )
            )
    finally:
        # A production search can evaluate six figures of candidates. The
        # parent owns the compact cache; retaining every WindowStats object in
        # every worker would turn process parallelism into a memory leak.
        _WORKER_SERVICE.cache.clear()
        _WORKER_SERVICE.records.clear()
    return _ProcessTaskResult(
        task_id=task.task_id,
        offset=task.offset,
        rows=tuple(rows),
        cpu_seconds=process_time() - started_cpu,
        wall_seconds=perf_counter() - started_wall,
        simulation_seconds=(
            float(_WORKER_SERVICE.timings["simulation_seconds"]) - simulation_before
        ),
    )


class ProcessEvaluationPool:
    """Bounded, deterministic producer/consumer pool for candidate batches."""

    def __init__(
        self,
        *,
        strategy,
        constraints,
        wf_manager,
        ranking_windows: list,
        market_group: str,
        workers: int,
        tasks_per_worker: int = 2,
    ):
        self.workers = max(1, int(workers))
        self.tasks_per_worker = max(1, int(tasks_per_worker))
        self._closed = False
        self._executor = ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_worker,
            initargs=(
                strategy,
                constraints,
                wf_manager,
                tuple(ranking_windows),
                str(market_group),
            ),
        )

    def evaluate(
        self,
        candidates: CandidateBatch,
        *,
        materialize: bool = False,
    ) -> ProcessEvaluationResult:
        if self._closed:
            raise RuntimeError("candidate process pool is closed")
        if not candidates.candidate_ids:
            return ProcessEvaluationResult((), 0.0, 0.0, 0.0, 0)

        task_count = min(len(candidates), self.workers * self.tasks_per_worker)
        chunk_size = (len(candidates) + task_count - 1) // task_count
        tasks = []
        for task_id, start in enumerate(range(0, len(candidates), chunk_size)):
            tasks.append(
                _ProcessTask(
                    task_id=task_id,
                    offset=start,
                    candidates=candidates.sliced(start, start + chunk_size),
                    materialize=bool(materialize),
                )
            )

        futures = [
            self._executor.submit(_evaluate_process_task, task) for task in tasks
        ]
        try:
            # Waiting in submission order keeps result assembly deterministic;
            # all submitted tasks still execute concurrently.
            completed = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            self.close(cancel_futures=True)
            raise

        completed.sort(key=lambda item: item.task_id)
        ordered: list[ProcessEvaluationRow | None] = [None] * len(candidates)
        for result in completed:
            for relative_index, row in enumerate(result.rows):
                ordered[result.offset + relative_index] = row
        if any(row is None for row in ordered):
            raise RuntimeError("candidate worker pool returned an incomplete batch")
        return ProcessEvaluationResult(
            rows=tuple(row for row in ordered if row is not None),
            worker_cpu_seconds=sum(item.cpu_seconds for item in completed),
            worker_wall_seconds=sum(item.wall_seconds for item in completed),
            worker_simulation_seconds=sum(
                item.simulation_seconds for item in completed
            ),
            task_count=len(completed),
        )

    def close(self, *, cancel_futures: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=cancel_futures)

    def __enter__(self) -> "ProcessEvaluationPool":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close(cancel_futures=exc is not None)
