"""Backward-compatible entry point for the simplified live scanner."""

from ...search_interface import Params
from .engine import SimplifiedSearchStrategy


def scan_simplified_today(params: Params, today: dict, history=None) -> list[dict]:
    """Return canonical simplified alerts from its TradePlan."""
    return SimplifiedSearchStrategy().scan_today(params, today, history)
