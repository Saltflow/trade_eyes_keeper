"""Public API for solver-neutral parameter search.

Adding an optimization algorithm means adding one decorated module under
``src/search/solvers``. SearchController and application entry points must not
be modified.
"""

from .api import (
    Candidate,
    CandidateBatch,
    EvaluationBatch,
    EvaluatorCapabilities,
    GateDecision,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    SearchProblem,
    Solver,
    SolverCapabilities,
    finite_score,
    stable_hash,
)
from .config import get_constraints, get_execution_config
from .controller import SearchController, SearchResult
from .workflow import run_optimizer
from .registry import create_solver, list_solvers, register_solver

__all__ = [
    "Candidate",
    "CandidateBatch",
    "EvaluationBatch",
    "EvaluatorCapabilities",
    "GateDecision",
    "ParameterKind",
    "ParameterSchema",
    "ParameterSpec",
    "SearchController",
    "SearchProblem",
    "SearchResult",
    "Solver",
    "SolverCapabilities",
    "create_solver",
    "finite_score",
    "get_constraints",
    "get_execution_config",
    "list_solvers",
    "register_solver",
    "run_optimizer",
    "stable_hash",
]
