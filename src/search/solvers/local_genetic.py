"""Genetic search with generation-aware local parameter mutations."""

from __future__ import annotations

import math
import random

from ..contracts import (
    Candidate,
    CandidateBatch,
    EvaluationBatch,
    finite_score,
    stable_hash,
)
from ..registry import register_solver
from ..solver import Solver
from .random import _as_tuple


@register_solver("local_genetic")
class LocalGeneticSolver(Solver):
    """Preserve GA diversity while explicitly exploring parent neighborhoods."""

    solver_id = "local_genetic"

    def initialize(self, problem, config=None) -> None:
        self.problem = problem
        self.config = dict(config or {})
        self.rng = random.Random(self.config.get("random_seed"))

        default_phase_one = min(problem.budget, max(1, problem.budget // 4))
        default_remaining = max(0, problem.budget - default_phase_one)
        default_generations = min(3, default_remaining) if default_remaining else 0
        default_offspring = (
            max(1, math.ceil(default_remaining / default_generations))
            if default_generations
            else 1
        )
        self.phase1_samples = min(
            max(
                1,
                int(
                    self.config.get(
                        "phase1_random_samples",
                        default_phase_one,
                    )
                ),
            ),
            problem.budget,
        )
        self.top_keep = min(
            self.phase1_samples,
            max(1, int(self.config.get("phase1_top_keep", 1000))),
        )
        self.generations = max(
            0,
            int(self.config.get("num_generations", default_generations)),
        )
        self.population_size = max(
            2,
            int(self.config.get("population_size", 1000)),
        )
        self.offspring_size = max(
            1,
            int(self.config.get("offspring_size", default_offspring)),
        )
        self.crossover_rate = self._rate("crossover_rate", 0.7)
        self.gene_mutation_rate = self._rate("gene_mutation_rate", 0.15)
        self.random_immigrant_rate = self._rate(
            "random_immigrant_rate",
            0.10,
        )
        if self.random_immigrant_rate >= 1.0:
            raise ValueError("random_immigrant_rate must be less than 1")
        self.max_local_step = max(
            1,
            int(self.config.get("max_local_step", 3)),
        )
        self.step_schedule = str(
            self.config.get("step_schedule", "linear_to_one")
        )
        if self.step_schedule != "linear_to_one":
            raise ValueError(
                "local_genetic step_schedule must be 'linear_to_one'"
            )
        self.duplicate_retry_limit = max(
            1,
            int(self.config.get("duplicate_retry_limit", 64)),
        )

        self.effective_config = {
            "budget": self.problem.budget,
            "phase1_random_samples": self.phase1_samples,
            "phase1_top_keep": self.top_keep,
            "num_generations": self.generations,
            "population_size": self.population_size,
            "offspring_size": self.offspring_size,
            "crossover_rate": self.crossover_rate,
            "gene_mutation_rate": self.gene_mutation_rate,
            "mandatory_local_mutation": True,
            "max_local_step": self.max_local_step,
            "step_schedule": self.step_schedule,
            "random_immigrant_rate": self.random_immigrant_rate,
            "duplicate_retry_limit": self.duplicate_retry_limit,
            "random_seed": self.config.get("random_seed"),
        }
        self.config_hash = stable_hash(self.effective_config)

        self.phase = "random"
        self.generation = 0
        self.phase_goal = self.phase1_samples
        self.phase_issued = 0
        self.phase_told = 0
        self.total_issued = 0
        self.stop = False
        self.stop_reason: str | None = None
        self.candidates: dict[str, dict[str, object]] = {}
        self.observations: dict[str, tuple[float, bool]] = {}
        self.population: list[str] = []
        self.seen_parameter_keys: set[str] = set()

    def _rate(self, name: str, default: float) -> float:
        value = float(self.config.get(name, default))
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
        return value

    def local_step_limit(self, generation: int | None = None) -> int:
        """Return the inclusive local step radius for one generation."""
        current = int(generation if generation is not None else self.generation)
        if current < 1:
            return self.max_local_step
        if self.generations <= 1:
            return 1
        remaining = self.generations - min(current, self.generations) + 1
        return max(
            1,
            int(math.ceil(self.max_local_step * remaining / self.generations)),
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

    def _parameter_key(self, values: dict[str, object]) -> str:
        normalized = self.problem.schema.validate(values)
        return stable_hash(
            {
                "schema": self.problem.schema.hash,
                "parameters": normalized,
            }
        )

    def _candidate(
        self,
        values: dict[str, object],
        source: str,
    ) -> Candidate | None:
        normalized = self.problem.schema.validate(values)
        key = self._parameter_key(normalized)
        if key in self.seen_parameter_keys:
            return None
        candidate = Candidate.create(
            normalized,
            self.problem.schema,
            source,
            nonce=str(self.total_issued),
        )
        self.seen_parameter_keys.add(key)
        self.candidates[candidate.candidate_id] = dict(candidate.parameters)
        return candidate

    def _random_unique(self, source: str) -> Candidate | None:
        for _attempt in range(self.duplicate_retry_limit):
            candidate = self._candidate(
                self.problem.schema.sample(self.rng),
                source,
            )
            if candidate is not None:
                return candidate
        return None

    def _crossover(self) -> dict[str, object]:
        parents = self.population[: self.population_size]
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
        return self.problem.schema.validate(values)

    def _mutate_locally(self, values: dict[str, object]) -> dict[str, object]:
        current = self.problem.schema.validate(values)
        step_limit = self.local_step_limit()
        movable = [
            parameter
            for parameter in self.problem.schema.parameters
            if parameter.is_active(current)
            and parameter.local_values(current[parameter.name], step_limit)
        ]
        if not movable:
            return current

        primary = self.rng.choice(movable)
        primary_values = primary.local_values(current[primary.name], step_limit)
        current[primary.name] = self.rng.choice(primary_values)
        current = self.problem.schema.validate(current)

        for parameter in self.problem.schema.parameters:
            if parameter.name == primary.name:
                continue
            if (
                not parameter.is_active(current)
                or self.rng.random() >= self.gene_mutation_rate
            ):
                continue
            alternatives = parameter.local_values(
                current[parameter.name],
                step_limit,
            )
            if not alternatives:
                continue
            current[parameter.name] = self.rng.choice(alternatives)
            current = self.problem.schema.validate(current)
        return current

    def _immigrant_quota(self) -> int:
        return min(
            self.phase_goal,
            int(math.floor(self.phase_goal * self.random_immigrant_rate + 0.5)),
        )

    def _is_immigrant_position(self, position: int) -> bool:
        quota = self._immigrant_quota()
        if quota <= 0:
            return False
        before = position * quota // self.phase_goal
        after = (position + 1) * quota // self.phase_goal
        return after > before

    def _generation_unique(self, position: int) -> Candidate | None:
        if self._is_immigrant_position(position):
            return self._random_unique(
                f"local_genetic/generation-{self.generation}/immigrant"
            )
        step_limit = self.local_step_limit()
        for _attempt in range(self.duplicate_retry_limit):
            values = self._mutate_locally(self._crossover())
            candidate = self._candidate(
                values,
                (
                    f"local_genetic/generation-{self.generation}"
                    f"/local-step-{step_limit}"
                ),
            )
            if candidate is not None:
                return candidate
        return self._random_unique(
            f"local_genetic/generation-{self.generation}/duplicate-fallback"
        )

    def ask(self, batch_size: int) -> CandidateBatch:
        if self.should_stop():
            return CandidateBatch.from_candidates([], self.problem.schema)
        count = min(
            max(1, int(batch_size)),
            self.phase_goal - self.phase_issued,
            self.problem.budget - self.total_issued,
        )
        candidates = []
        for _index in range(max(0, count)):
            if self.phase == "random":
                candidate = self._random_unique("local_genetic/initialize")
            else:
                candidate = self._generation_unique(self.phase_issued)
            if candidate is None:
                self.stop = True
                self.stop_reason = "search_stalled"
                break
            candidates.append(candidate)
            self.phase_issued += 1
            self.total_issued += 1
        return CandidateBatch.from_candidates(candidates, self.problem.schema)

    def _finish(self, reason: str) -> None:
        self.stop = True
        self.stop_reason = reason

    def _start_generation(self, generation: int) -> None:
        self.phase = "genetic"
        self.generation = generation
        self.phase_issued = 0
        self.phase_told = 0
        self.phase_goal = min(
            self.offspring_size,
            self.problem.budget - self.total_issued,
        )
        if self.phase_goal <= 0:
            self._finish("completed_budget")

    def tell(self, evaluations: EvaluationBatch) -> None:
        for candidate_id, score, feasible in zip(
            evaluations.candidate_ids,
            evaluations.objective_scores,
            evaluations.feasible,
        ):
            self.observations[candidate_id] = (
                finite_score(score),
                bool(feasible),
            )
            self.phase_told += 1
        self._prune_phase_state()
        if self.stop or self.phase_told < self.phase_issued:
            return
        if self.phase_issued < self.phase_goal:
            return

        if self.phase == "random":
            self.population = self._rank(self.observations)[: self.top_keep]
            self._prune_to_population()
            if self.total_issued >= self.problem.budget:
                self._finish("completed_budget")
            elif self.generations <= 0:
                self._finish("completed_generations")
            elif len(self.population) < 2:
                self._finish("insufficient_feasible_population")
            else:
                self._start_generation(1)
            return

        self.population = self._rank(self.observations)[: self.population_size]
        self._prune_to_population()
        if self.total_issued >= self.problem.budget:
            self._finish("completed_budget")
        elif self.generation >= self.generations:
            self._finish("completed_generations")
        elif len(self.population) < 2:
            self._finish("insufficient_feasible_population")
        else:
            self._start_generation(self.generation + 1)

    def should_stop(self) -> bool:
        return self.stop

    def finalists(self, limit=None) -> tuple[str, ...]:
        ranked = self._rank(self.observations)
        if limit is not None:
            ranked = ranked[: max(0, int(limit))]
        return tuple(ranked)

    def state_dict(self) -> dict[str, object]:
        return {
            "solver_id": self.solver_id,
            "problem_hash": self.problem.contract_hash,
            "config_hash": self.config_hash,
            "phase": self.phase,
            "generation": self.generation,
            "phase_goal": self.phase_goal,
            "phase_issued": self.phase_issued,
            "phase_told": self.phase_told,
            "total_issued": self.total_issued,
            "stop": self.stop,
            "stop_reason": self.stop_reason,
            "candidates": self.candidates,
            "observations": self.observations,
            "population": self.population,
            "seen_parameter_keys": sorted(self.seen_parameter_keys),
            "rng_state": self.rng.getstate(),
        }

    def load_state_dict(self, state: dict[str, object]) -> None:
        if state.get("solver_id") != self.solver_id:
            raise ValueError("local genetic solver checkpoint type mismatch")
        if state.get("problem_hash") != self.problem.contract_hash:
            raise ValueError(
                "local genetic solver checkpoint/search problem mismatch"
            )
        if state.get("config_hash") != self.config_hash:
            raise ValueError(
                "local genetic solver checkpoint/config mismatch"
            )
        for name in (
            "phase",
            "generation",
            "phase_goal",
            "phase_issued",
            "phase_told",
            "total_issued",
            "stop",
            "stop_reason",
            "candidates",
            "observations",
            "population",
        ):
            setattr(self, name, state[name])
        self.generation = int(self.generation)
        self.phase_goal = int(self.phase_goal)
        self.phase_issued = int(self.phase_issued)
        self.phase_told = int(self.phase_told)
        self.total_issued = int(self.total_issued)
        self.stop = bool(self.stop)
        self.candidates = {
            str(candidate_id): self.problem.schema.validate(dict(values))
            for candidate_id, values in dict(self.candidates).items()
        }
        self.observations = {
            str(candidate_id): (finite_score(value[0]), bool(value[1]))
            for candidate_id, value in dict(self.observations).items()
        }
        self.population = list(self.population)
        self.seen_parameter_keys = set(state.get("seen_parameter_keys", ()))
        self.rng.setstate(_as_tuple(state["rng_state"]))
