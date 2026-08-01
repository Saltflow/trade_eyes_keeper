from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.search.gates import CandidateGatePipeline
from src.search.archive import SearchArchive
from src.search.contracts import (
    Candidate,
    CandidateBatch,
    EvaluationBatch,
    GateDecision,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    SearchProblem,
    SolverCapabilities,
)
from src.search.controller import SearchController
from src.search import create_solver, list_solvers, register_solver
from src.search.solver import Solver


def _schema() -> ParameterSchema:
    return ParameterSchema(
        (
            ParameterSpec(
                "x",
                ParameterKind.ORDINAL,
                values=tuple(range(5)),
                transfer_key="shared_x",
            ),
            ParameterSpec(
                "enabled",
                ParameterKind.BOOLEAN,
                values=(False, True),
            ),
            ParameterSpec(
                "child",
                ParameterKind.ORDINAL,
                values=(0, 1, 2),
                active_if=(("enabled", (True,)),),
            ),
        )
    )


def _problem(budget: int = 20) -> SearchProblem:
    return SearchProblem(
        schema=_schema(),
        objective_id="quadratic/1",
        gate_profile_id="test",
        budget=budget,
        data_hash="data",
        execution_hash="execution",
        window_hash="ranking-only",
        feature_hash="features",
    )


class FakeEvaluationService:
    capabilities = SimpleNamespace(gradients=False)

    def __init__(self):
        self.records = {}

    def evaluate_batch(self, candidates: CandidateBatch) -> EvaluationBatch:
        metrics = []
        scores = []
        for index, candidate_id in enumerate(candidates.candidate_ids):
            params = candidates.parameters_at(index)
            score = -float((params["x"] - 2) ** 2)
            raw = {"objective_score": score}
            metrics.append(raw)
            scores.append(score)
            self.records[candidate_id] = SimpleNamespace(
                parameters=params,
                ranking_stats=[],
                objective_score=score,
                raw_metrics=raw,
            )
        return EvaluationBatch(
            candidate_ids=candidates.candidate_ids,
            raw_metrics=tuple(metrics),
            objective_scores=np.asarray(scores),
            gate_decisions=tuple(GateDecision(True) for _ in scores),
            feasible=np.ones(len(scores), dtype=bool),
            failure_reasons=tuple(() for _ in scores),
        )


@pytest.mark.parametrize(
    ("solver_id", "budget", "config"),
    [
        ("random", 12, {"random_seed": 7}),
        (
            "genetic",
            12,
            {
                "random_seed": 7,
                "phase1_random_samples": 4,
                "phase1_top_keep": 4,
                "num_generations": 2,
                "population_size": 3,
                "offspring_size": 4,
            },
        ),
        (
            "simulated_annealing",
            12,
            {"random_seed": 7, "initialization_samples": 4},
        ),
    ],
)
def test_all_solvers_use_the_same_controller_contract(solver_id, budget, config):
    controller = SearchController(
        _problem(budget),
        create_solver(solver_id),
        FakeEvaluationService(),
        CandidateGatePipeline("empty", ()),
        solver_config=config,
        batch_size=3,
    )

    results = controller.run()

    assert results
    assert results[0].parameters["x"] == 2
    assert results[0].objective_score == 0.0


def test_annealing_is_single_candidate_and_checkpoint_deterministic():
    problem = _problem(15)
    config = {"random_seed": 19, "initialization_samples": 4}
    left = create_solver("simulated_annealing")
    left.initialize(problem, config)
    for _ in range(7):
        batch = left.ask(256)
        assert len(batch) == 1
        score = -float((batch.parameters_at(0)["x"] - 2) ** 2)
        left.tell(
            EvaluationBatch(
                batch.candidate_ids,
                ({"objective_score": score},),
                np.asarray([score]),
                (GateDecision(True),),
                np.asarray([True]),
                ((),),
            )
        )
    state = left.state_dict()
    right = create_solver("simulated_annealing")
    right.initialize(problem, config)
    right.load_state_dict(state)

    assert left.temperature() == right.temperature()
    assert left.ask(256).parameters_at(0) == right.ask(1).parameters_at(0)


def test_search_controller_restores_checkpoint_and_replays_ranking_finalists(
    tmp_path: Path,
):
    checkpoint = tmp_path / "search.yaml"
    config = {"random_seed": 19, "initialization_samples": 4}
    first = SearchController(
        _problem(9),
        create_solver("simulated_annealing"),
        FakeEvaluationService(),
        CandidateGatePipeline("empty", ()),
        solver_config=config,
        checkpoint_path=checkpoint,
    ).run()

    restored_service = FakeEvaluationService()
    restored = SearchController(
        _problem(9),
        create_solver("simulated_annealing"),
        restored_service,
        CandidateGatePipeline("empty", ()),
        solver_config=config,
        checkpoint_path=checkpoint,
    ).run()

    assert checkpoint.exists()
    assert [item.candidate_id for item in restored] == [
        item.candidate_id for item in first
    ]
    assert [item.parameters for item in restored] == [item.parameters for item in first]
    assert set(restored_service.records) == {item.candidate_id for item in restored}


def test_schema_conditional_parameter_is_canonical_and_neighbor_is_legal():
    schema = _schema()
    inactive = schema.validate({"x": 2, "enabled": False, "child": 2})
    assert inactive["child"] == 0
    for seed in range(20):
        neighbor = schema.neighbor(inactive, __import__("random").Random(seed))
        assert schema.validate(neighbor) == neighbor


def test_schema_local_perturbations_change_one_parameter_by_one_level():
    schema = _schema()
    current = {"x": 2, "enabled": True, "child": 1}

    neighbors = schema.local_perturbations(current)

    assert neighbors
    assert {neighbor["x"] for neighbor in neighbors} >= {1, 3}
    assert {neighbor["child"] for neighbor in neighbors} >= {0, 2}
    for neighbor in neighbors:
        assert schema.validate(neighbor) == neighbor
        changed = [name for name in schema.names if neighbor[name] != current[name]]
        # Disabling a parent can also canonicalize its now-inactive child.
        assert len(changed) == 1 or (
            changed == ["enabled", "child"] and neighbor["enabled"] is False
        )


def test_gate_profiles_support_count_ratio_penalty_diagnostic_and_off():
    base = {
        "objective_score": 1.0,
        "strongest_benchmark_win_windows": 6,
        "strongest_benchmark_win_ratio": 6 / 11,
    }
    config = {
        "gate_profiles": {
            "seven": {
                "activation_eligible": True,
                "rules": [
                    {
                        "id": "wins",
                        "metric": "strongest_benchmark_win_windows",
                        "mode": "hard",
                        "operator": "ge",
                        "value": 7,
                    }
                ],
            },
            "six": {
                "activation_eligible": True,
                "rules": [
                    {
                        "id": "wins",
                        "metric": "strongest_benchmark_win_windows",
                        "mode": "hard",
                        "operator": "ge",
                        "value": 6,
                    }
                ],
            },
            "ratio": {
                "activation_eligible": False,
                "rules": [
                    {
                        "id": "ratio",
                        "metric": "strongest_benchmark_win_ratio",
                        "mode": "diagnostic",
                        "operator": "ge",
                        "value": 0.6,
                    },
                    {
                        "id": "score",
                        "metric": "objective_score",
                        "mode": "penalty",
                        "operator": "gt",
                        "value": 2,
                        "penalty": 0.5,
                    },
                ],
            },
            "off": {"activation_eligible": False, "rules": []},
        }
    }
    assert (
        not CandidateGatePipeline.from_config(config, "seven").evaluate(base).feasible
    )
    assert CandidateGatePipeline.from_config(config, "six").evaluate(base).feasible
    ratio = CandidateGatePipeline.from_config(config, "ratio").evaluate(base)
    assert ratio.feasible and ratio.penalty == 0.5
    assert CandidateGatePipeline.from_config(config, "off").evaluate(base).feasible

    config["gate_profiles"]["bad"] = {
        "rules": [
            {
                "id": "unsafe",
                "metric": "objective_score",
                "mode": "hard",
                "operator": "python_eval",
                "value": 0,
            }
        ]
    }
    with pytest.raises(ValueError, match="operator"):
        CandidateGatePipeline.from_config(config, "bad")

    config["gate_profiles"]["conflict"] = {
        "rules": [
            {
                "id": "lower",
                "metric": "strongest_benchmark_win_windows",
                "mode": "hard",
                "operator": "ge",
                "value": 7,
            },
            {
                "id": "upper",
                "metric": "strongest_benchmark_win_windows",
                "mode": "hard",
                "operator": "le",
                "value": 6,
            },
        ]
    }
    with pytest.raises(ValueError, match="conflicting hard rules"):
        CandidateGatePipeline.from_config(config, "conflict")


def test_search_archive_rejects_holdout_information(tmp_path: Path):
    archive = SearchArchive(tmp_path / "archive.jsonl", _problem())
    archive.append([{"candidate_id": "ok", "ranking_metrics": {"score": 1}}])
    with pytest.raises(ValueError, match="non-ranking"):
        archive.append([{"candidate_id": "bad", "holdout_return": 99}])


def test_solver_plugin_registration_does_not_change_search_controller():
    solver_id = "test_constant_solver"
    if solver_id not in list_solvers():

        @register_solver(solver_id)
        class ConstantSolver(Solver):
            solver_id = "test_constant_solver"

            def initialize(self, problem, config=None):
                self.problem = problem
                self.done = False
                self.ids = ()

            def ask(self, batch_size):
                candidate = Candidate.create(
                    {"x": 2, "enabled": False, "child": 0},
                    self.problem.schema,
                    "test",
                )
                self.done = True
                return CandidateBatch.from_candidates([candidate], self.problem.schema)

            def tell(self, evaluations):
                self.ids = evaluations.candidate_ids

            def should_stop(self):
                return self.done

            def finalists(self, limit=None):
                return self.ids

            def state_dict(self):
                return {"done": self.done}

            def load_state_dict(self, state):
                self.done = bool(state["done"])

    results = SearchController(
        _problem(1),
        create_solver(solver_id),
        FakeEvaluationService(),
        CandidateGatePipeline("empty", ()),
    ).run()
    assert results[0].objective_score == 0.0


def test_gradient_solver_is_rejected_before_initialization():
    solver_id = "test_gradient_solver"
    if solver_id not in list_solvers():

        @register_solver(solver_id)
        class GradientSolver(Solver):
            solver_id = "test_gradient_solver"
            capabilities = SolverCapabilities(requires_gradients=True)

            def initialize(self, problem, config=None):
                raise AssertionError("must be rejected before initialization")

            def ask(self, batch_size):
                raise AssertionError

            def tell(self, evaluations):
                raise AssertionError

            def should_stop(self):
                return False

            def finalists(self, limit=None):
                return ()

            def state_dict(self):
                return {}

            def load_state_dict(self, state):
                raise AssertionError

    controller = SearchController(
        _problem(1),
        create_solver(solver_id),
        FakeEvaluationService(),
        CandidateGatePipeline("empty", ()),
    )
    with pytest.raises(ValueError, match="requires gradients"):
        controller.run()
