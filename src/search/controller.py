"""Stable orchestration loop shared by every solver."""

from __future__ import annotations

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
                self.candidate_parameters[candidate_id] = candidates.parameters_at(
                    index
                )
                if retain_all_metadata:
                    self.adjusted_scores[candidate_id] = float(adjusted[index])
                    self.gate_decisions[candidate_id] = decisions[index]
                    self.candidate_feasible[candidate_id] = bool(feasible[index])
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
            self._batch_count += 1
            self._checkpoint()

        finalist_ids = list(self.solver.finalists(finalist_limit))
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
        self.solver.load_state_dict(dict(payload.get("solver", {})))
        controller_state = payload.get("controller", {}) or {}
        parameters = controller_state.get("candidate_parameters", {}) or {}
        self.candidate_parameters = {
            str(candidate_id): self.problem.schema.validate(dict(values))
            for candidate_id, values in parameters.items()
        }

    def _materialize_checkpoint_finalists(self, finalist_ids: list[str]) -> None:
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
