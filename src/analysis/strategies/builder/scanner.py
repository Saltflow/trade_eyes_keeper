"""Backward-compatible entry point for the builder live scanner."""

from ...search_interface import Params
from .engine import BuilderSearchStrategy


def scan_builder_today(params: Params, today: dict, history=None) -> list[dict]:
    """Return canonical builder alerts from its TradePlan."""
    return BuilderSearchStrategy().scan_today(params, today, history)
