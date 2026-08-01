"""Stable public contracts for parameter-search plugins.

Concrete algorithms live in :mod:`src.search.solvers`.  Application code
should import these contracts from :mod:`src.search`.
"""

from .contracts import (
    Candidate,
    CandidateBatch,
    EvaluationBatch,
    EvaluatorCapabilities,
    GateDecision,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    SearchProblem,
    SolverCapabilities,
    finite_score,
    stable_hash,
)
from .solver import Solver

__all__ = [
    "Candidate",
    "CandidateBatch",
    "EvaluationBatch",
    "EvaluatorCapabilities",
    "GateDecision",
    "ParameterKind",
    "ParameterSchema",
    "ParameterSpec",
    "SearchProblem",
    "Solver",
    "SolverCapabilities",
    "finite_score",
    "stable_hash",
]
