"""Unified simulation and evaluation engine used by every strategy."""

from .engine import (
    Backtester,
    FastEvaluator,
    INDICATOR_NAMES,
    WalkForwardManager,
    build_trade_plan,
    buy_and_hold_nav,
    evaluate_all_groups,
    simulate_portfolio,
)

__all__ = [
    "Backtester",
    "FastEvaluator",
    "INDICATOR_NAMES",
    "WalkForwardManager",
    "build_trade_plan",
    "buy_and_hold_nav",
    "evaluate_all_groups",
    "simulate_portfolio",
]
