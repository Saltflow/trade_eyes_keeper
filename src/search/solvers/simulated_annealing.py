"""Deterministic strict single-chain simulated annealing solver."""

from __future__ import annotations

import math
import random

import numpy as np

from ..contracts import Candidate, CandidateBatch, EvaluationBatch, finite_score
from ..solver import Solver
from ..registry import register_solver
from .random import _as_tuple


@register_solver("simulated_annealing")
class SimulatedAnnealingSolver(Solver):
    solver_id = "simulated_annealing"

    def initialize(self, problem, config=None) -> None:
        self.problem = problem
        self.config = dict(config or {})
        self.rng = random.Random(self.config.get("random_seed"))
        self.initialization_count = min(
            max(1, int(self.config.get("initialization_samples", 64))),
            problem.budget,
        )
        self.issued = 0
        self.told = 0
        self.phase = "initialize"
        self.pending_id: str | None = None
        self.candidates: dict[str, dict[str, object]] = {}
        self.observations: dict[str, tuple[float, bool]] = {}
        self.current_id: str | None = None
        self.best_id: str | None = None
        self.temperature_start = 1.0
        self.temperature_end = 0.01
        self.temperature_scale = 1.0
        self.move_index = 0
        self.accepted_moves = 0

    def ask(self, batch_size: int) -> CandidateBatch:
        # Strict single-line semantics: never expose more than one candidate,
        # even when the controller offers a larger transport batch.
        if self.should_stop() or self.pending_id is not None:
            return CandidateBatch.from_candidates([], self.problem.schema)
        if self.phase == "initialize":
            values = self.problem.schema.sample(self.rng)
            source = "annealing/initialize"
        else:
            values = self.problem.schema.neighbor(
                self.candidates[self.current_id], self.rng
            )
            source = "annealing/neighbor"
        candidate = Candidate.create(
            values,
            self.problem.schema,
            source,
            nonce=str(self.issued),
        )
        self.candidates[candidate.candidate_id] = dict(candidate.parameters)
        self.pending_id = candidate.candidate_id
        self.issued += 1
        return CandidateBatch.from_candidates([candidate], self.problem.schema)

    def tell(self, evaluations: EvaluationBatch) -> None:
        if len(evaluations.candidate_ids) != 1:
            raise ValueError("single-chain annealing requires one evaluation per tell")
        candidate_id = evaluations.candidate_ids[0]
        if candidate_id != self.pending_id:
            raise ValueError("annealing tell does not match the pending candidate")
        score = finite_score(evaluations.objective_scores[0])
        feasible = bool(evaluations.feasible[0])
        self.observations[candidate_id] = (score, feasible)
        self.told += 1
        self.pending_id = None

        if self.phase == "initialize":
            if self.told >= self.initialization_count:
                feasible_ids = [
                    key for key, (_score, ok) in self.observations.items() if ok
                ]
                pool = feasible_ids or list(self.observations)
                self.current_id = max(
                    pool,
                    key=lambda key: (self.observations[key][0], key),
                )
                self.best_id = self.current_id
                self._calibrate_temperature()
                self.phase = "anneal"
            return

        current_score = self.observations[self.current_id][0]
        delta = score - current_score
        temperature = self.temperature()
        accepted = feasible and (
            delta >= 0.0
            or self.rng.random() < math.exp(delta / max(temperature, 1e-12))
        )
        if accepted:
            self.current_id = candidate_id
            self.accepted_moves += 1
        if feasible and (
            self.best_id is None or score > self.observations[self.best_id][0]
        ):
            self.best_id = candidate_id
        self.move_index += 1

    def _calibrate_temperature(self) -> None:
        scores = sorted(
            score for score, feasible in self.observations.values() if feasible
        )
        deltas = [
            abs(right - left)
            for left, right in zip(scores, scores[1:])
            if abs(right - left) > 1e-12
        ]
        self.temperature_scale = float(np.median(deltas)) if deltas else 1.0
        # exp(-delta/T)=p -> T=-delta/log(p)
        self.temperature_start = -self.temperature_scale / math.log(0.80)
        self.temperature_end = -self.temperature_scale / math.log(0.01)

    def temperature(self) -> float:
        moves = max(self.problem.budget - self.initialization_count, 1)
        progress = min(max(self.move_index / max(moves - 1, 1), 0.0), 1.0)
        return (
            self.temperature_start
            * (self.temperature_end / self.temperature_start) ** progress
        )

    def should_stop(self) -> bool:
        return self.issued >= self.problem.budget and self.pending_id is None

    def finalists(self, limit=None) -> tuple[str, ...]:
        ranked = sorted(
            (
                candidate_id
                for candidate_id, (_score, feasible) in self.observations.items()
                if feasible
            ),
            key=lambda key: (self.observations[key][0], key),
            reverse=True,
        )
        if limit is not None:
            ranked = ranked[: max(0, int(limit))]
        return tuple(ranked)

    def state_dict(self) -> dict[str, object]:
        return {
            "solver_id": self.solver_id,
            "problem_hash": self.problem.contract_hash,
            "issued": self.issued,
            "told": self.told,
            "phase": self.phase,
            "pending_id": self.pending_id,
            "candidates": self.candidates,
            "observations": self.observations,
            "current_id": self.current_id,
            "best_id": self.best_id,
            "temperature_start": self.temperature_start,
            "temperature_end": self.temperature_end,
            "temperature_scale": self.temperature_scale,
            "move_index": self.move_index,
            "accepted_moves": self.accepted_moves,
            "rng_state": self.rng.getstate(),
        }

    def load_state_dict(self, state: dict[str, object]) -> None:
        if state.get("problem_hash") != self.problem.contract_hash:
            raise ValueError("annealing checkpoint/search problem mismatch")
        for name in (
            "issued",
            "told",
            "phase",
            "pending_id",
            "candidates",
            "observations",
            "current_id",
            "best_id",
            "temperature_start",
            "temperature_end",
            "temperature_scale",
            "move_index",
            "accepted_moves",
        ):
            setattr(self, name, state[name])
        self.issued = int(self.issued)
        self.told = int(self.told)
        self.move_index = int(self.move_index)
        self.accepted_moves = int(self.accepted_moves)
        self.candidates = dict(self.candidates)
        self.observations = dict(self.observations)
        self.rng.setstate(_as_tuple(state["rng_state"]))
