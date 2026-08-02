from __future__ import annotations

import numpy as np
import pytest

from src.search import create_solver, list_solvers
from src.search.contracts import (
    EvaluationBatch,
    GateDecision,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    SearchProblem,
)


def _wide_schema() -> ParameterSchema:
    return ParameterSchema(
        tuple(
            ParameterSpec(
                name,
                ParameterKind.ORDINAL,
                values=tuple(range(20)),
            )
            for name in ("a", "b", "c", "d")
        )
    )


def _problem(schema: ParameterSchema, budget: int) -> SearchProblem:
    return SearchProblem(
        schema=schema,
        objective_id="local-genetic-test/1",
        gate_profile_id="test",
        budget=budget,
        data_hash="data",
        execution_hash="execution",
        window_hash="ranking-only",
        feature_hash="features",
    )


def _tell_feasible(solver, batch) -> None:
    scores = np.asarray(
        [
            -sum(
                (float(value) - 10.0) ** 2
                for value in batch.parameters_at(index).values()
                if not isinstance(value, bool)
            )
            for index in range(len(batch))
        ],
        dtype=np.float64,
    )
    solver.tell(
        EvaluationBatch(
            candidate_ids=batch.candidate_ids,
            raw_metrics=tuple({} for _ in scores),
            objective_scores=scores,
            gate_decisions=tuple(GateDecision(True) for _ in scores),
            feasible=np.ones(len(scores), dtype=bool),
            failure_reasons=tuple(() for _ in scores),
        )
    )


def _config() -> dict[str, object]:
    return {
        "random_seed": 23,
        "phase1_random_samples": 10,
        "phase1_top_keep": 10,
        "num_generations": 3,
        "population_size": 8,
        "offspring_size": 10,
        "crossover_rate": 0.7,
        "gene_mutation_rate": 0.15,
        "max_local_step": 3,
        "step_schedule": "linear_to_one",
        "random_immigrant_rate": 0.10,
        "duplicate_retry_limit": 64,
    }


def _run_sequence(batch_size: int):
    solver = create_solver("local_genetic")
    solver.initialize(_problem(_wide_schema(), 40), _config())
    sequence = []
    while not solver.should_stop():
        batch = solver.ask(batch_size)
        if not len(batch):
            break
        sequence.extend(
            (batch.parameters_at(index), batch.sources[index])
            for index in range(len(batch))
        )
        _tell_feasible(solver, batch)
    return solver, sequence


def test_parameter_spec_local_values_respect_kind_distance_and_bounds():
    ordinal = ParameterSpec(
        "ordinal",
        ParameterKind.ORDINAL,
        values=tuple(range(5)),
    )
    category = ParameterSpec(
        "category",
        ParameterKind.CATEGORICAL,
        values=("a", "b", "c"),
    )
    boolean = ParameterSpec(
        "boolean",
        ParameterKind.BOOLEAN,
        values=(False, True),
    )
    continuous = ParameterSpec(
        "continuous",
        ParameterKind.CONTINUOUS,
        low=0.0,
        high=1.0,
        step=0.1,
        mutation_step=0.2,
    )

    assert ordinal.local_values(2, 2) == (0, 1, 3, 4)
    assert ordinal.local_values(0, 3) == (1, 2, 3)
    assert category.local_values("b", 3) == ("a", "c")
    assert boolean.local_values(False, 3) == (True,)
    assert continuous.local_values(0.5, 2) == pytest.approx(
        (0.1, 0.3, 0.7, 0.9)
    )
    with pytest.raises(ValueError, match="max_levels"):
        ordinal.local_values(2, 0)


def test_local_step_schedule_shrinks_from_three_to_one():
    solver = create_solver("local_genetic")
    config = {**_config(), "num_generations": 5}
    solver.initialize(_problem(_wide_schema(), 60), config)

    assert [solver.local_step_limit(index) for index in range(1, 6)] == [
        3,
        3,
        2,
        2,
        1,
    ]


def test_generation_has_exact_immigrant_quota_and_mandatory_local_move():
    solver = create_solver("local_genetic")
    config = {**_config(), "gene_mutation_rate": 0.0}
    solver.initialize(_problem(_wide_schema(), 40), config)
    initial = solver.ask(10)
    _tell_feasible(solver, initial)
    base = {"a": 10, "b": 10, "c": 10, "d": 10}
    solver._crossover = lambda: dict(base)

    generation = solver.ask(10)

    immigrant_indexes = [
        index
        for index, source in enumerate(generation.sources)
        if source.endswith("/immigrant")
    ]
    local_indexes = [
        index
        for index, source in enumerate(generation.sources)
        if "/local-step-3" in source
    ]
    assert immigrant_indexes == [9]
    assert len(local_indexes) == 9
    for index in local_indexes:
        values = generation.parameters_at(index)
        changed = [
            name for name in base if values[name] != base[name]
        ]
        assert len(changed) == 1
        assert 1 <= abs(values[changed[0]] - base[changed[0]]) <= 3


def test_candidate_sequence_is_independent_of_transport_batch_size():
    left, left_sequence = _run_sequence(1)
    right, right_sequence = _run_sequence(7)

    assert left_sequence == right_sequence
    assert left.finalists() == right.finalists()
    assert left.stop_reason == right.stop_reason == "completed_budget"
    assert left.total_issued == right.total_issued == 40


def test_checkpoint_restores_rng_seen_parameters_and_generation_state():
    problem = _problem(_wide_schema(), 40)
    config = _config()
    left = create_solver("local_genetic")
    left.initialize(problem, config)
    for size in (6, 4, 3):
        batch = left.ask(size)
        _tell_feasible(left, batch)

    state = left.state_dict()
    right = create_solver("local_genetic")
    right.initialize(problem, config)
    right.load_state_dict(state)

    left_next = left.ask(5)
    right_next = right.ask(5)
    assert left_next.candidate_ids == right_next.candidate_ids
    assert left_next.sources == right_next.sources
    assert [
        left_next.parameters_at(index) for index in range(len(left_next))
    ] == [
        right_next.parameters_at(index) for index in range(len(right_next))
    ]

    mismatched = create_solver("local_genetic")
    mismatched.initialize(problem, {**config, "max_local_step": 2})
    with pytest.raises(ValueError, match="config mismatch"):
        mismatched.load_state_dict(state)


def test_finite_parameter_space_stops_instead_of_spending_duplicate_budget():
    schema = ParameterSchema(
        (
            ParameterSpec(
                "x",
                ParameterKind.BOOLEAN,
                values=(False, True),
            ),
        )
    )
    solver = create_solver("local_genetic")
    solver.initialize(
        _problem(schema, 10),
        {
            "random_seed": 2,
            "phase1_random_samples": 10,
            "duplicate_retry_limit": 4,
        },
    )

    batch = solver.ask(10)
    _tell_feasible(solver, batch)

    assert len(batch) == 2
    assert solver.total_issued == 2
    assert solver.stop_reason == "search_stalled"
    assert solver.should_stop()


def test_old_genetic_fixed_seed_candidate_sequence_is_unchanged():
    schema = ParameterSchema(
        (
            ParameterSpec("x", ParameterKind.ORDINAL, values=tuple(range(5))),
            ParameterSpec("y", ParameterKind.ORDINAL, values=tuple(range(5))),
        )
    )
    solver = create_solver("genetic")
    solver.initialize(
        _problem(schema, 8),
        {
            "random_seed": 7,
            "phase1_random_samples": 4,
            "phase1_top_keep": 4,
            "num_generations": 1,
            "population_size": 3,
            "offspring_size": 4,
            "crossover_rate": 0.7,
            "mutation_rate": 0.3,
            "gene_mutation_rate": 0.15,
        },
    )

    initial = solver.ask(10)
    assert [initial.parameters_at(index) for index in range(len(initial))] == [
        {"x": 2, "y": 1},
        {"x": 3, "y": 0},
        {"x": 0, "y": 4},
        {"x": 0, "y": 2},
    ]
    scores = np.arange(len(initial), dtype=np.float64)
    solver.tell(
        EvaluationBatch(
            candidate_ids=initial.candidate_ids,
            raw_metrics=tuple({} for _ in scores),
            objective_scores=scores,
            gate_decisions=tuple(GateDecision(True) for _ in scores),
            feasible=np.ones(len(scores), dtype=bool),
            failure_reasons=tuple(() for _ in scores),
        )
    )
    generation = solver.ask(10)
    assert [generation.parameters_at(index) for index in range(len(generation))] == [
        {"x": 0, "y": 0},
        {"x": 0, "y": 4},
        {"x": 3, "y": 2},
        {"x": 3, "y": 0},
    ]
    assert "local_genetic" in list_solvers()
