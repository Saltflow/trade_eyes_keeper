"""Solver interface for strategy-independent parameter optimization."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .contracts import (
    CandidateBatch,
    EvaluationBatch,
    SearchProblem,
    SolverCapabilities,
)


class Solver(ABC):
    """Strategy-agnostic ask/tell optimization algorithm."""

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

    def candidate_parameters(
        self, candidate_id: str
    ) -> dict[str, object] | None:
        """Return live Solver parameters for finalist replay when available."""
        return None


def assert_capabilities(
    solver: Solver, problem: SearchProblem, evaluator_has_gradients: bool = False
) -> None:
    """Reject incompatible Solver/Evaluator contracts before search starts."""
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
