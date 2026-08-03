from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from src.search.archive import SearchArchive
from src.search.config import SearchRuntimeConfig
from src.search.contracts import (
    CandidateBatch,
    EvaluationBatch,
    GateDecision,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    SearchProblem,
)
from src.search.controller import SearchController
from src.search.evaluator import EvaluationService
from src.search.gates import CandidateGatePipeline
from src.search.registry import create_solver


def _schema() -> ParameterSchema:
    return ParameterSchema(
        (
            ParameterSpec(
                "x",
                ParameterKind.ORDINAL,
                values=tuple(range(100)),
            ),
        )
    )


def _problem(budget: int) -> SearchProblem:
    return SearchProblem(
        schema=_schema(),
        objective_id="retention-test",
        gate_profile_id="empty",
        budget=budget,
        data_hash="data",
        execution_hash="execution",
        window_hash="ranking-only",
        feature_hash="features",
    )


class RetainingEvaluationService:
    capabilities = SimpleNamespace(gradients=False)

    def __init__(self):
        self.records = {}
        self.retained_sizes = []

    def evaluate_batch(self, candidates: CandidateBatch) -> EvaluationBatch:
        metrics = []
        scores = []
        for index, candidate_id in enumerate(candidates.candidate_ids):
            parameters = candidates.parameters_at(index)
            score = float(parameters["x"])
            raw = {"objective_score": score}
            metrics.append(raw)
            scores.append(score)
            self.records[candidate_id] = SimpleNamespace(
                candidate_id=candidate_id,
                parameters={},
                ranking_stats=[],
                objective_score=score,
                raw_metrics=raw,
                failure_reasons=(),
                materialized=False,
            )
        return EvaluationBatch(
            candidate_ids=candidates.candidate_ids,
            raw_metrics=tuple(metrics),
            objective_scores=np.asarray(scores, dtype=np.float64),
            gate_decisions=tuple(GateDecision(True) for _ in scores),
            feasible=np.ones(len(scores), dtype=bool),
            failure_reasons=tuple(() for _ in scores),
        )

    def retain_records(self, candidate_ids) -> None:
        retained = set(candidate_ids)
        self.records = {
            candidate_id: record
            for candidate_id, record in self.records.items()
            if candidate_id in retained
        }
        self.retained_sizes.append(len(self.records))

    def materialize_batch(self, candidates: CandidateBatch) -> EvaluationBatch:
        result = self.evaluate_batch(candidates)
        for candidate_id in result.candidate_ids:
            self.records[candidate_id].parameters = candidates.parameters_at(
                result.candidate_ids.index(candidate_id)
            )
            self.records[candidate_id].materialized = True
        return result


def test_runtime_retention_ratio_defaults_and_validation():
    assert SearchRuntimeConfig({}, genetic={}).candidate_retention_ratio == 0.05
    assert (
        SearchRuntimeConfig(
            {"candidate_retention_ratio": 0.2}, genetic={}
        ).candidate_retention_ratio
        == 0.2
    )
    for invalid in (0, -0.1, 1.01):
        with pytest.raises(ValueError, match="candidate_retention_ratio"):
            SearchRuntimeConfig(
                {"candidate_retention_ratio": invalid}, genetic={}
            )


def test_controller_bounds_replay_reservoir_and_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "search.yaml"
    service = RetainingEvaluationService()
    controller = SearchController(
        _problem(20),
        create_solver("local_genetic"),
        service,
        CandidateGatePipeline("empty", ()),
        solver_config={
            "random_seed": 7,
            "phase1_random_samples": 8,
            "phase1_top_keep": 4,
            "num_generations": 2,
            "population_size": 3,
            "offspring_size": 6,
        },
        batch_size=4,
        checkpoint_path=checkpoint,
        archive=SearchArchive(tmp_path / "archive.jsonl", _problem(20)),
        retention_ratio=0.25,
    )

    results = controller.run(finalist_limit=2)

    assert len(results) == 2
    assert controller.retained_candidate_count == 2
    assert max(service.retained_sizes) <= 5
    state = yaml.safe_load(checkpoint.read_text(encoding="utf-8"))
    assert len(state["controller"]["candidate_parameters"]) == 5
    assert state["controller"]["retention_ratio"] == 0.25
    assert len(state["solver"]["candidates"]) <= 3
    assert len(state["solver"]["observations"]) <= 3


@pytest.mark.parametrize("solver_id", ["genetic", "local_genetic"])
def test_genetic_solvers_prune_each_phase_to_live_parent_pool(solver_id):
    problem = _problem(40)
    solver = create_solver(solver_id)
    config = {
        "random_seed": 19,
        "phase1_random_samples": 10,
        "phase1_top_keep": 5,
        "num_generations": 2,
        "population_size": 4,
        "offspring_size": 15,
        "random_immigrant_rate": 0.1,
    }
    solver.initialize(problem, config)
    maximum_live = 0
    while not solver.should_stop():
        batch = solver.ask(3)
        if not len(batch):
            break
        scores = np.asarray(
            [float(batch.parameters_at(index)["x"]) for index in range(len(batch))]
        )
        solver.tell(
            EvaluationBatch(
                batch.candidate_ids,
                tuple({"objective_score": score} for score in scores),
                scores,
                tuple(GateDecision(True) for _ in scores),
                np.ones(len(scores), dtype=bool),
                tuple(() for _ in scores),
            )
        )
        maximum_live = max(maximum_live, len(solver.candidates))
        assert len(solver.candidates) == len(solver.observations)
        assert len(solver.candidates) <= 9

    assert maximum_live <= 9
    assert len(solver.candidates) <= 4


def test_evaluation_service_retain_records_prunes_both_indexes():
    service = EvaluationService.__new__(EvaluationService)
    first = SimpleNamespace(candidate_id="first")
    second = SimpleNamespace(candidate_id="second")
    service.records = {"first": first, "second": second}
    service.cache = {(1,): first, (2,): second}

    service.retain_records(["second"])

    assert service.records == {"second": second}
    assert service.cache == {(2,): second}
