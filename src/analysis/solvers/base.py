"""Solver protocol and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from ..search_contracts import (
    CandidateBatch,
    EvaluationBatch,
    SearchProblem,
    SolverCapabilities,
)


class Solver(ABC):
    """A strategy-agnostic ask/tell optimizer."""

    solver_id = ""
    capabilities = SolverCapabilities()

    @abstractmethod
    def initialize(
        self, problem: SearchProblem, config: dict[str, object] | None = None
    ) -> None: ...

    @abstractmethod
    def ask(self, batch_size: int) -> CandidateBatch: ...

    @abstractmethod
    def tell(self, evaluations: EvaluationBatch) -> None: ...

    @abstractmethod
    def should_stop(self) -> bool: ...

    @abstractmethod
    def finalists(self, limit: int | None = None) -> tuple[str, ...]: ...

    @abstractmethod
    def state_dict(self) -> dict[str, object]: ...

    @abstractmethod
    def load_state_dict(self, state: dict[str, object]) -> None: ...


_SOLVERS: dict[str, Callable[[], Solver]] = {}


def register_solver(solver_id: str):
    normalized = str(solver_id).strip().lower()
    if not normalized:
        raise ValueError("solver_id cannot be empty")

    def decorator(factory: Callable[[], Solver]):
        if normalized in _SOLVERS:
            raise ValueError(f"duplicate solver registration: {normalized}")
        _SOLVERS[normalized] = factory
        return factory

    return decorator


def create_solver(solver_id: str) -> Solver:
    try:
        return _SOLVERS[str(solver_id).strip().lower()]()
    except KeyError as exc:
        raise ValueError(
            f"unknown solver {solver_id!r}; available={sorted(_SOLVERS)}"
        ) from exc


def list_solvers() -> tuple[str, ...]:
    return tuple(sorted(_SOLVERS))


def assert_capabilities(
    solver: Solver, problem: SearchProblem, evaluator_has_gradients: bool = False
) -> None:
    capabilities = solver.capabilities
    if capabilities.requires_gradients and not evaluator_has_gradients:
        raise ValueError(
            f"solver {solver.solver_id!r} requires gradients, but the "
            "evaluation service is non-differentiable"
        )
    if (
        problem.requirements.conditional_parameters
        and not capabilities.conditional_parameters
    ):
        if any(parameter.active_if for parameter in problem.schema.parameters):
            raise ValueError(
                f"solver {solver.solver_id!r} cannot handle conditional parameters"
            )
