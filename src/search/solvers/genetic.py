"""Batch genetic solver migrated from the historical optimizer."""

from __future__ import annotations

import random

from ..contracts import Candidate, CandidateBatch, EvaluationBatch, finite_score
from ..solver import Solver
from ..registry import register_solver
from .random import _as_tuple


@register_solver("genetic")
class GeneticSolver(Solver):
    solver_id = "genetic"

    def initialize(self, problem, config=None) -> None:
        self.problem = problem
        self.config = dict(config or {})
        self.rng = random.Random(self.config.get("random_seed"))
        self.phase1_samples = min(
            int(self.config.get("phase1_random_samples", problem.budget)),
            problem.budget,
        )
        self.top_keep = max(1, int(self.config.get("phase1_top_keep", 1000)))
        self.generations = max(0, int(self.config.get("num_generations", 3)))
        self.population_size = max(2, int(self.config.get("population_size", 1000)))
        self.offspring_size = max(1, int(self.config.get("offspring_size", 5000)))
        self.crossover_rate = float(self.config.get("crossover_rate", 0.7))
        self.mutation_rate = float(self.config.get("mutation_rate", 0.3))
        self.gene_mutation_rate = float(self.config.get("gene_mutation_rate", 0.15))
        self.phase = "random"
        self.generation = 0
        self.phase_issued = 0
        self.phase_told = 0
        self.total_issued = 0
        self.stop = False
        self.candidates: dict[str, dict[str, object]] = {}
        self.observations: dict[str, tuple[float, bool]] = {}
        self.population: list[str] = []

    def _phase_target(self) -> int:
        if self.phase == "random":
            return self.phase1_samples
        remaining = self.problem.budget - self.total_issued
        return self.phase_issued + min(
            self.offspring_size - self.phase_issued, remaining
        )

    def _rank(self, ids) -> list[str]:
        return sorted(
            (
                candidate_id
                for candidate_id in ids
                if self.observations.get(candidate_id, (-float("inf"), False))[1]
            ),
            key=lambda candidate_id: self.observations[candidate_id][0],
            reverse=True,
        )

    def candidate_parameters(
        self, candidate_id: str
    ) -> dict[str, object] | None:
        parameters = self.candidates.get(candidate_id)
        return dict(parameters) if parameters is not None else None

    def _prune_phase_state(self) -> None:
        limit = self.top_keep if self.phase == "random" else self.population_size
        retained = set(self.population)
        retained.update(self._rank(self.observations)[:limit])
        self.candidates = {
            candidate_id: parameters
            for candidate_id, parameters in self.candidates.items()
            if candidate_id in retained
        }
        self.observations = {
            candidate_id: observation
            for candidate_id, observation in self.observations.items()
            if candidate_id in retained
        }

    def _prune_to_population(self) -> None:
        retained = set(self.population)
        self.candidates = {
            candidate_id: parameters
            for candidate_id, parameters in self.candidates.items()
            if candidate_id in retained
        }
        self.observations = {
            candidate_id: observation
            for candidate_id, observation in self.observations.items()
            if candidate_id in retained
        }

    def _random_candidate(self) -> Candidate:
        return Candidate.create(
            self.problem.schema.sample(self.rng),
            self.problem.schema,
            "genetic/random",
            nonce=str(self.total_issued),
        )

    def _child(self) -> Candidate:
        parents = self.population[: self.population_size]
        if len(parents) < 2:
            return self._random_candidate()
        left_id, right_id = self.rng.sample(parents, 2)
        left = self.candidates[left_id]
        right = self.candidates[right_id]
        if self.rng.random() < self.crossover_rate:
            values = {
                name: left[name] if self.rng.random() < 0.5 else right[name]
                for name in self.problem.schema.names
            }
        else:
            values = dict(self.candidates[self.rng.choice(parents)])
        if self.rng.random() < self.mutation_rate:
            for parameter in self.problem.schema.parameters:
                if (
                    parameter.is_active(values)
                    and self.rng.random() < self.gene_mutation_rate
                ):
                    values[parameter.name] = parameter.sample(self.rng)
        return Candidate.create(
            values,
            self.problem.schema,
            f"genetic/generation-{self.generation + 1}",
            nonce=str(self.total_issued),
        )

    def ask(self, batch_size: int) -> CandidateBatch:
        if self.stop:
            return CandidateBatch.from_candidates([], self.problem.schema)
        target = self._phase_target()
        count = min(
            max(1, int(batch_size)),
            target - self.phase_issued,
            self.problem.budget - self.total_issued,
        )
        candidates = []
        for _ in range(max(0, count)):
            candidate = (
                self._random_candidate() if self.phase == "random" else self._child()
            )
            candidates.append(candidate)
            self.candidates[candidate.candidate_id] = dict(candidate.parameters)
            self.phase_issued += 1
            self.total_issued += 1
        return CandidateBatch.from_candidates(candidates, self.problem.schema)

    def tell(self, evaluations: EvaluationBatch) -> None:
        for candidate_id, score, feasible in zip(
            evaluations.candidate_ids,
            evaluations.objective_scores,
            evaluations.feasible,
        ):
            self.observations[candidate_id] = (finite_score(score), bool(feasible))
            self.phase_told += 1
        self._prune_phase_state()
        if self.phase_told < self.phase_issued:
            return
        target = self._phase_target()
        if self.phase_issued < target:
            return
        if self.phase == "random":
            self.population = self._rank(self.observations)[: self.top_keep]
            self._prune_to_population()
            if self.generations <= 0 or len(self.population) < 2:
                self.stop = True
                return
            self.phase = "genetic"
            self.phase_issued = 0
            self.phase_told = 0
            return

        # Dict insertion order preserves the historical stable tie-breaker.
        self.population = self._rank(self.observations)[: self.population_size]
        self._prune_to_population()
        self.generation += 1
        if (
            self.generation >= self.generations
            or self.total_issued >= self.problem.budget
            or len(self.population) < 2
        ):
            self.stop = True
        else:
            self.phase_issued = 0
            self.phase_told = 0

    def should_stop(self) -> bool:
        return self.stop or self.total_issued >= self.problem.budget

    def finalists(self, limit=None) -> tuple[str, ...]:
        ranked = self._rank(self.observations)
        if limit is not None:
            ranked = ranked[: max(0, int(limit))]
        return tuple(ranked)

    def state_dict(self) -> dict[str, object]:
        return {
            "solver_id": self.solver_id,
            "problem_hash": self.problem.contract_hash,
            "phase": self.phase,
            "generation": self.generation,
            "phase_issued": self.phase_issued,
            "phase_told": self.phase_told,
            "total_issued": self.total_issued,
            "stop": self.stop,
            "candidates": self.candidates,
            "observations": self.observations,
            "population": self.population,
            "rng_state": self.rng.getstate(),
        }

    def load_state_dict(self, state: dict[str, object]) -> None:
        if state.get("problem_hash") != self.problem.contract_hash:
            raise ValueError("genetic solver checkpoint/search problem mismatch")
        for name in (
            "phase",
            "generation",
            "phase_issued",
            "phase_told",
            "total_issued",
            "stop",
            "candidates",
            "observations",
            "population",
        ):
            setattr(self, name, state[name])
        self.generation = int(self.generation)
        self.phase_issued = int(self.phase_issued)
        self.phase_told = int(self.phase_told)
        self.total_issued = int(self.total_issued)
        self.stop = bool(self.stop)
        self.candidates = dict(self.candidates)
        self.observations = dict(self.observations)
        self.population = list(self.population)
        self.rng.setstate(_as_tuple(state["rng_state"]))
