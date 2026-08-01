"""Uniform legal sampling baseline."""

from __future__ import annotations

import random

from ..contracts import Candidate, CandidateBatch, EvaluationBatch, finite_score
from ..solver import Solver
from ..registry import register_solver


@register_solver("random")
class RandomSolver(Solver):
    solver_id = "random"

    def initialize(self, problem, config=None) -> None:
        self.problem = problem
        self.config = dict(config or {})
        self.rng = random.Random(self.config.get("random_seed"))
        self.issued = 0
        self.observations: dict[str, tuple[float, bool]] = {}

    def ask(self, batch_size: int) -> CandidateBatch:
        count = min(max(1, int(batch_size)), self.problem.budget - self.issued)
        candidates = []
        for _ in range(max(0, count)):
            nonce = str(self.issued)
            candidates.append(
                Candidate.create(
                    self.problem.schema.sample(self.rng),
                    self.problem.schema,
                    source="random",
                    nonce=nonce,
                )
            )
            self.issued += 1
        return CandidateBatch.from_candidates(candidates, self.problem.schema)

    def tell(self, evaluations: EvaluationBatch) -> None:
        for candidate_id, score, feasible in zip(
            evaluations.candidate_ids,
            evaluations.objective_scores,
            evaluations.feasible,
        ):
            self.observations[candidate_id] = (finite_score(score), bool(feasible))

    def should_stop(self) -> bool:
        return self.issued >= self.problem.budget

    def finalists(self, limit=None) -> tuple[str, ...]:
        ranked = sorted(
            (
                (score, candidate_id)
                for candidate_id, (score, feasible) in self.observations.items()
                if feasible
            ),
            reverse=True,
        )
        if limit is not None:
            ranked = ranked[: max(0, int(limit))]
        return tuple(candidate_id for _score, candidate_id in ranked)

    def state_dict(self) -> dict[str, object]:
        return {
            "solver_id": self.solver_id,
            "problem_hash": self.problem.contract_hash,
            "issued": self.issued,
            "observations": dict(self.observations),
            "rng_state": self.rng.getstate(),
        }

    def load_state_dict(self, state: dict[str, object]) -> None:
        if state.get("problem_hash") != self.problem.contract_hash:
            raise ValueError("random solver checkpoint/search problem mismatch")
        self.issued = int(state.get("issued", 0))
        self.observations = dict(state.get("observations", {}))
        self.rng.setstate(_as_tuple(state["rng_state"]))


def _as_tuple(value):
    if isinstance(value, list):
        return tuple(_as_tuple(item) for item in value)
    return value
