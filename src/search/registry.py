"""Automatic discovery and construction of Solver plugins."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules

from .solver import Solver

_SOLVERS: dict[str, Callable[[], Solver]] = {}
_DISCOVERED = False


def register_solver(solver_id: str):
    """Register one Solver class or zero-argument factory."""
    normalized = str(solver_id).strip().lower()
    if not normalized:
        raise ValueError("solver_id cannot be empty")

    def decorator(factory: Callable[[], Solver]):
        current = _SOLVERS.get(normalized)
        if current is not None and current is not factory:
            raise ValueError(f"duplicate solver registration: {normalized}")
        _SOLVERS[normalized] = factory
        return factory

    return decorator


def discover_solvers() -> None:
    """Import every concrete module under ``src.search.solvers`` once."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    package_dir = Path(__file__).parent / "solvers"
    for module in iter_modules([str(package_dir)]):
        if module.name.startswith("_"):
            continue
        import_module(f"{__package__}.solvers.{module.name}")
    _DISCOVERED = True


def create_solver(solver_id: str) -> Solver:
    discover_solvers()
    normalized = str(solver_id).strip().lower()
    try:
        return _SOLVERS[normalized]()
    except KeyError as exc:
        raise ValueError(
            f"unknown solver {solver_id!r}; available={sorted(_SOLVERS)}"
        ) from exc


def list_solvers() -> tuple[str, ...]:
    discover_solvers()
    return tuple(sorted(_SOLVERS))
