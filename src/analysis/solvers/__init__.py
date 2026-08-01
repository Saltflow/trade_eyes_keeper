"""Solver plugin discovery.

Any module placed in this package can register a solver with
``@register_solver``.  Discovery is automatic, so adding an algorithm never
requires a change to SearchController or to this module.
"""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules
from pathlib import Path

from .base import Solver, create_solver, list_solvers, register_solver


def _discover() -> None:
    package_dir = Path(__file__).parent
    for module in iter_modules([str(package_dir)]):
        if module.name.startswith("_") or module.name == "base":
            continue
        import_module(f"{__name__}.{module.name}")


_discover()

__all__ = ["Solver", "create_solver", "list_solvers", "register_solver"]
