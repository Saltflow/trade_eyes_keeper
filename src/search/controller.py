"""Stable orchestration loop shared by every solver."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from .archive import SearchArchive
from .contracts import (
    Candidate,
    CandidateBatch,
    EvaluationBatch,
    SearchProblem,
)
from .solver import Solver, assert_capabilities


@dataclass
class SearchResult:
    candidate_id: str
    parameters: dict[str, object]
    ranking_stats: list[object]
    objective_score: float
    selection_score: float
    ranking_metrics: dict[str, object]
    gate_results: tuple[dict[str, object], ...]
    gate_feasible: bool = True


class SearchController:
    """Budget/cache/checkpoint orchestration with no algorithm branches."""

    def __init__(
        self,
        problem: SearchProblem,
        solver: Solver,
        evaluation_service,
        gate_pipeline,
        solver_config: dict[str, object] | None = None,
        batch_size: int = 256,
        archive: SearchArchive | None = None,
        checkpoint_path: Path | str | None = None,
        retention_ratio: float = 1.0,
        include_infeasible_results: bool = False,
        materialize_finalists: bool = True,
    ):
        self.problem = problem
        self.solver = solver
        self.evaluation_service = evaluation_service
        self.gate_pipeline = gate_pipeline
        self.solver_config = dict(solver_config or {})
        self.batch_size = max(1, int(batch_size))
        self.archive = archive
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.adjusted_scores: dict[str, float] = {}
        self.gate_decisions = {}
        self.candidate_feasible: dict[str, bool] = {}
        self.candidate_parameters: dict[str, dict[str, object]] = {}
        self.include_infeasible_results = bool(include_infeasible_results)
        self.materialize_finalists = bool(materialize_finalists)
        self.retention_ratio = float(retention_ratio)
        if not 0.0 < self.retention_ratio <= 1.0:
            raise ValueError("retention_ratio must be greater than 0 and at most 1")
        self._retained_scores: dict[str, float] = {}
        self._retained_order: dict[str, int] = {}
        self._retention_sequence = 0
        self._evaluated_count = 0
        self._batch_count = 0

    def run(self, finalist_limit: int | None = None) -> list[SearchResult]:
        evaluator_capabilities = getattr(self.evaluation_service, "capabilities", None)
        assert_capabilities(
            self.solver,
            self.problem,
            evaluator_has_gradients=bool(
                getattr(evaluator_capabilities, "gradients", False)
            ),
        )
        self.solver.initialize(self.problem, self.solver_config)
        self._restore_checkpoint()
        retain_all_metadata = (
            self.include_infeasible_results or not self.materialize_finalists
        )
        while not self.solver.should_stop():
            candidates = self.solver.ask(self.batch_size)
            if len(candidates) == 0:
                if self.solver.should_stop():
                    break
                raise RuntimeError(
                    f"solver {self.solver.solver_id!r} returned no candidates "
                    "before stop"
                )
            raw = self.evaluation_service.evaluate_batch(candidates)
            decisions = tuple(
                self.gate_pipeline.evaluate(metrics) for metrics in raw.raw_metrics
            )
            feasible = np.asarray(
                [
                    bool(raw.feasible[index]) and decision.feasible
                    for index, decision in enumerate(decisions)
                ],
                dtype=bool,
            )
            adjusted = np.asarray(
                [
                    float(raw.objective_scores[index]) - decision.penalty
                    for index, decision in enumerate(decisions)
                ],
                dtype=np.float64,
            )
            reasons = tuple(
                tuple(raw.failure_reasons[index]) + decision.failure_reasons
                for index, decision in enumerate(decisions)
            )
            evaluated = EvaluationBatch(
                candidate_ids=raw.candidate_ids,
                raw_metrics=raw.raw_metrics,
                objective_scores=adjusted,
                gate_decisions=decisions,
                feasible=feasible,
                failure_reasons=reasons,
                fidelity=raw.fidelity,
            )
            for index, candidate_id in enumerate(evaluated.candidate_ids):
                parameters = candidates.parameters_at(index)
                self._evaluated_count += 1
                if retain_all_metadata:
                    self.candidate_parameters[candidate_id] = parameters
                    self.adjusted_scores[candidate_id] = float(adjusted[index])
                    self.gate_decisions[candidate_id] = decisions[index]
                    self.candidate_feasible[candidate_id] = bool(feasible[index])
                elif bool(feasible[index]):
                    self._retain_candidate(
                        candidate_id,
                        parameters,
                        float(adjusted[index]),
                    )
            if self.archive is not None:
                self.archive.append(
                    [
                        {
                            "candidate_id": candidate_id,
                            "parameters": candidates.parameters_at(index),
                            "ranking_metrics": raw.raw_metrics[index],
                            "objective_score": float(raw.objective_scores[index]),
                            "selection_score": float(adjusted[index]),
                            "feasible": bool(feasible[index]),
                            "gate_results": list(decisions[index].results),
                        }
                        for index, candidate_id in enumerate(evaluated.candidate_ids)
                    ]
                )
            self.solver.tell(evaluated)
            if not retain_all_metadata:
                self._prune_evaluator_records()
            self._batch_count += 1
            self._checkpoint()

        archived_finalists = (
            self.archive.top_records(finalist_limit)
            if self.archive is not None
            else []
        )
        if archived_finalists:
            finalist_ids = []
            for order, record in enumerate(archived_finalists):
                candidate_id = str(record["candidate_id"])
                finalist_ids.append(candidate_id)
                self.candidate_parameters[candidate_id] = (
                    self.problem.schema.validate(dict(record["parameters"]))
                )
                self._retained_scores[candidate_id] = float(
                    record["selection_score"]
                )
                self._retained_order.setdefault(candidate_id, order)
        else:
            retained_ids = sorted(
                self._retained_scores,
                key=lambda candidate_id: (
                    -self._retained_scores[candidate_id],
                    self._retained_order[candidate_id],
                ),
            )
            finalist_ids = list(self.solver.finalists(finalist_limit))
            finalist_ids.extend(
                candidate_id
                for candidate_id in retained_ids
                if candidate_id not in finalist_ids
            )
            if finalist_limit is not None:
                finalist_ids = finalist_ids[: max(0, int(finalist_limit))]
        if self.include_infeasible_results:
            seen = set(finalist_ids)
            extras = sorted(
                (key for key in self.adjusted_scores if key not in seen),
                key=lambda key: self.adjusted_scores[key],
                reverse=True,
            )
            finalist_ids.extend(extras)
            if finalist_limit is not None:
                finalist_ids = finalist_ids[: max(0, int(finalist_limit))]
        if self.materialize_finalists:
            self._materialize_checkpoint_finalists(finalist_ids)

        for candidate_id in finalist_ids:
            if candidate_id in self.gate_decisions:
                continue
            record = self.evaluation_service.records.get(candidate_id)
            if record is None:
                continue
            decision = self.gate_pipeline.evaluate(record.raw_metrics)
            self.gate_decisions[candidate_id] = decision
            self.candidate_feasible[candidate_id] = (
                not bool(getattr(record, "failure_reasons", ()))
                and decision.feasible
            )
            self.adjusted_scores[candidate_id] = (
                float(record.objective_score) - decision.penalty
            )

        results = []
        for candidate_id in finalist_ids:
            record = self.evaluation_service.records.get(candidate_id)
            if record is None:
                continue
            decision = self.gate_decisions[candidate_id]
            results.append(
                SearchResult(
                    candidate_id=candidate_id,
                    parameters=dict(
                        record.parameters
                        or self.candidate_parameters.get(candidate_id, {})
                    ),
                    ranking_stats=list(record.ranking_stats),
                    objective_score=float(record.objective_score),
                    selection_score=float(self.adjusted_scores[candidate_id]),
                    ranking_metrics=dict(record.raw_metrics),
                    gate_results=decision.results,
                    gate_feasible=self.candidate_feasible[candidate_id],
                )
            )
        return results

    @property
    def retained_candidate_count(self) -> int:
        return len(self.candidate_parameters)

    def _retain_candidate(
        self,
        candidate_id: str,
        parameters: dict[str, object],
        score: float,
    ) -> None:
        order = self._retention_sequence
        self._retention_sequence += 1
        self.candidate_parameters[candidate_id] = dict(parameters)
        self._retained_scores[candidate_id] = float(score)
        self._retained_order[candidate_id] = order
        capacity = max(
            1,
            int(math.ceil(self._evaluated_count * self.retention_ratio)),
        )
        while len(self._retained_scores) > capacity:
            discarded = min(
                self._retained_scores,
                key=lambda key: (
                    self._retained_scores[key],
                    -self._retained_order[key],
                ),
            )
            self.candidate_parameters.pop(discarded, None)
            self._retained_scores.pop(discarded, None)
            self._retained_order.pop(discarded, None)

    def _prune_evaluator_records(self) -> None:
        retain = getattr(self.evaluation_service, "retain_records", None)
        if not callable(retain):
            return
        current_limit = max(
            1,
            int(math.ceil(self._evaluated_count * self.retention_ratio)),
        )
        ranked = sorted(
            self._retained_scores,
            key=lambda candidate_id: (
                -self._retained_scores[candidate_id],
                self._retained_order[candidate_id],
            ),
        )
        retain(ranked[:current_limit])

    def _restore_checkpoint(self) -> None:
        if (
            self.checkpoint_path is None
            or not self.checkpoint_path.exists()
            or not self.solver.capabilities.checkpoint
        ):
            return
        payload = yaml.safe_load(self.checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid search checkpoint payload")
        if payload.get("search_contract_hash") != self.problem.contract_hash:
            return
        controller_state = payload.get("controller", {}) or {}
        stored_ratio = float(
            controller_state.get("retention_ratio", self.retention_ratio)
        )
        if not math.isclose(stored_ratio, self.retention_ratio):
            return
        self.solver.load_state_dict(dict(payload.get("solver", {})))
        parameters = controller_state.get("candidate_parameters", {}) or {}
        self.candidate_parameters = {
            str(candidate_id): self.problem.schema.validate(dict(values))
            for candidate_id, values in parameters.items()
        }
        self._retained_scores = {
            str(candidate_id): float(score)
            for candidate_id, score in (
                controller_state.get("retained_scores", {}) or {}
            ).items()
            if str(candidate_id) in self.candidate_parameters
        }
        self._retained_order = {
            str(candidate_id): int(order)
            for candidate_id, order in (
                controller_state.get("retained_order", {}) or {}
            ).items()
            if str(candidate_id) in self.candidate_parameters
        }
        self._retention_sequence = int(
            controller_state.get("retention_sequence", len(self._retained_order))
        )
        self._evaluated_count = int(controller_state.get("evaluated_count", 0))
        capacity = max(
            1,
            int(math.ceil(self._evaluated_count * self.retention_ratio)),
        )
        while len(self._retained_scores) > capacity:
            discarded = min(
                self._retained_scores,
                key=lambda key: (
                    self._retained_scores[key],
                    -self._retained_order[key],
                ),
            )
            self.candidate_parameters.pop(discarded, None)
            self._retained_scores.pop(discarded, None)
            self._retained_order.pop(discarded, None)

    def _materialize_checkpoint_finalists(self, finalist_ids: list[str]) -> None:
        retained = set(finalist_ids)
        self.candidate_parameters = {
            candidate_id: parameters
            for candidate_id, parameters in self.candidate_parameters.items()
            if candidate_id in retained
        }
        self._retained_scores = {
            candidate_id: score
            for candidate_id, score in self._retained_scores.items()
            if candidate_id in retained
        }
        self._retained_order = {
            candidate_id: order
            for candidate_id, order in self._retained_order.items()
            if candidate_id in retained
        }
        retain_records = getattr(self.evaluation_service, "retain_records", None)
        if callable(retain_records):
            retain_records(finalist_ids)
        missing = [
            candidate_id
            for candidate_id in finalist_ids
            if candidate_id not in self.evaluation_service.records
            or not bool(
                getattr(
                    self.evaluation_service.records[candidate_id],
                    "materialized",
                    True,
                )
            )
        ]
        for start in range(0, len(missing), self.batch_size):
            rows = []
            for candidate_id in missing[start: start + self.batch_size]:
                parameters = self.candidate_parameters.get(candidate_id)
                if parameters is None:
                    parameters = self.solver.candidate_parameters(candidate_id)
                if parameters is None:
                    raise ValueError(
                        f"checkpoint lacks parameters for finalist {candidate_id}"
                    )
                rows.append(
                    Candidate(
                        candidate_id,
                        parameters,
                        self.problem.schema.hash,
                        "checkpoint/replay",
                    )
                )
            batch = CandidateBatch.from_candidates(rows, self.problem.schema)
            materialize = getattr(
                self.evaluation_service,
                "materialize_batch",
                self.evaluation_service.evaluate_batch,
            )
            raw = materialize(batch)
            for index, candidate_id in enumerate(raw.candidate_ids):
                decision = self.gate_pipeline.evaluate(raw.raw_metrics[index])
                self.gate_decisions[candidate_id] = decision
                self.candidate_feasible[candidate_id] = (
                    bool(raw.feasible[index]) and decision.feasible
                )
                self.adjusted_scores[candidate_id] = (
                    float(raw.objective_scores[index]) - decision.penalty
                )

    def _checkpoint(self) -> None:
        if self.checkpoint_path is None or not self.solver.capabilities.checkpoint:
            return
        # Serializing a six-figure GA archive after every candidate batch is
        # more expensive than evaluation. Persist periodically and at stop.
        if self._batch_count % 100 and not self.solver.should_stop():
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "search_contract_hash": self.problem.contract_hash,
            "solver": self.solver.state_dict(),
            "controller": {
                "candidate_parameters": self.candidate_parameters,
                "retained_scores": self._retained_scores,
                "retained_order": self._retained_order,
                "retention_sequence": self._retention_sequence,
                "evaluated_count": self._evaluated_count,
                "retention_ratio": self.retention_ratio,
            },
        }
        temporary = self.checkpoint_path.with_suffix(
            self.checkpoint_path.suffix + ".tmp"
        )
        temporary.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint_path)
