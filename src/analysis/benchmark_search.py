"""Publication-free ranking search used by cross-strategy benchmarks."""

from __future__ import annotations

from .candidate_gates import CandidateGatePipeline
from .evaluation_service import EvaluationService
from .resource_planner import ResourcePlanner
from .search_contracts import SearchProblem, stable_hash
from .search_controller import SearchController
from .solvers import create_solver


def run_ranking_benchmark_search(
    *,
    strategy,
    constraints,
    manager,
    evaluator,
    ranking_windows: list,
    group: str,
    search_depth: int,
    random_seed: int,
    evaluation_workers: int,
    input_fingerprints: dict[str, object],
    solver_id: str = "random",
    solver_config: dict[str, object] | None = None,
    batch_size: int | None = None,
):
    """Run any registered Solver under the configured Gate Profile.

    Infeasible candidates are retained only for benchmark diagnostics. Their
    feasibility bit remains false and they cannot be activated.
    """
    schema = strategy.search_parameter_schema
    effective_batch_size = (
        int(batch_size)
        if batch_size is not None
        else min(512, max(128, int(search_depth)))
    )
    planner = ResourcePlanner()
    resource_plan = planner.plan(
        "candidate_window",
        workers=max(1, int(evaluation_workers)),
        batch_size=effective_batch_size,
    )
    gate_pipeline = CandidateGatePipeline.from_config(
        constraints._raw_config, constraints.search.gate_profile
    )
    execution = constraints.execution
    problem = SearchProblem(
        schema=schema,
        objective_id="weighted-strongest-excess-stability-sharpe/1",
        gate_profile_id=gate_pipeline.hash,
        budget=int(search_depth),
        data_hash=stable_hash(input_fingerprints),
        execution_hash=stable_hash(
            {
                "initial_capital": execution.initial_capital,
                "commission_rate": execution.commission_rate,
                "min_holding_days": execution.min_holding_days,
                "lot_size": execution.lot_sizes.get(group, 100),
                "fx_rate": execution.fx_rates.get(group, 1.0),
            }
        ),
        window_hash=stable_hash(
            [
                {
                    "train_start": window.train_start,
                    "test_start": window.test_start,
                    "test_end": window.test_end,
                }
                for window in ranking_windows
            ]
        ),
        feature_hash=stable_hash(
            {
                "strategy_id": strategy.name,
                "features": list(getattr(strategy, "feature_dependencies", ()) or ()),
            }
        ),
        metadata={
            "strategy_id": strategy.name,
            "market": group,
            "resource_axis": resource_plan.axis,
            "resource_workers": resource_plan.outer_workers,
        },
    )
    effective_solver_config = _benchmark_solver_config(
        solver_id,
        int(search_depth),
        int(random_seed),
        solver_config,
    )
    service = EvaluationService(
        strategy,
        constraints,
        manager,
        evaluator,
        ranking_windows,
        workers=resource_plan.outer_workers,
    )
    controller = SearchController(
        problem,
        create_solver(solver_id),
        service,
        gate_pipeline,
        solver_config=effective_solver_config,
        batch_size=resource_plan.batch_size,
        include_infeasible_results=True,
    )
    with planner.apply(resource_plan):
        results = controller.run(finalist_limit=int(search_depth))
    return results, service, gate_pipeline, problem, effective_solver_config


def _benchmark_solver_config(
    solver_id: str,
    budget: int,
    random_seed: int,
    configured: dict[str, object] | None,
) -> dict[str, object]:
    """Create fair deterministic defaults while honoring explicit overrides."""
    defaults: dict[str, object] = {"random_seed": int(random_seed)}
    if solver_id == "genetic":
        phase_one = min(budget, max(64, budget // 4))
        remaining = max(0, budget - phase_one)
        generations = min(3, remaining) if remaining else 0
        offspring = max(1, remaining // max(generations, 1))
        defaults.update(
            {
                "phase1_random_samples": phase_one,
                "phase1_top_keep": min(128, phase_one),
                "num_generations": generations,
                "population_size": min(128, phase_one),
                "offspring_size": offspring,
            }
        )
    elif solver_id == "simulated_annealing":
        defaults["initialization_samples"] = min(64, budget)
    defaults.update(dict(configured or {}))
    return defaults
