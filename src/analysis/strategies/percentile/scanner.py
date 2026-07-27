"""Backward-compatible entry point for the percentile live scanner.

Live scanning must use the same ``TradePlan`` construction as optimization and
the daily backtest.  Keep this module for callers that used the old function,
but delegate rather than maintaining a second threshold decoder.
"""

from ...search_interface import Params
from .engine import PercentileSearchStrategy


def scan_percentile_today(
    params: Params, today: dict, history=None
) -> list[dict]:
    """Return canonical percentile alerts for the latest history row."""
    return PercentileSearchStrategy().scan_today(params, today, history)
