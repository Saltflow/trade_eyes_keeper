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
from .config import (
    MarketOptimizerConfig,
    get_constraints,
    get_execution_config,
    get_market_optimizer_config,
    get_market_optimizer_configs,
)
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
    "get_market_optimizer_config",
    "get_market_optimizer_configs",
    "MarketOptimizerConfig",
    "list_solvers",
    "register_solver",
    "run_optimizer",
    "stable_hash",
]
