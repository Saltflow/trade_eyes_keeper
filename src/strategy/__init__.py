"""Public API for trading-strategy plugins.

Add implementations under :mod:`src.strategy.plugins` and register them with
``@register_strategy``. Callers should import strategy contracts and registry
functions from this package, never from a concrete plugin module.
"""

from .api import (
    ArraySignalStrategy,
    EvaluationReport,
    ParamDim,
    ParamSpace,
    Params,
    PortfolioTrace,
    StrategyMarketData,
    TradePlan,
    TradingStrategy,
    allocate_target_weights,
)
from .registry import (
    get_strategy,
    list_strategies,
    list_strategy_ids,
    register_strategy,
)

__all__ = [
    "ArraySignalStrategy",
    "EvaluationReport",
    "ParamDim",
    "ParamSpace",
    "Params",
    "PortfolioTrace",
    "StrategyMarketData",
    "TradePlan",
    "TradingStrategy",
    "allocate_target_weights",
    "get_strategy",
    "list_strategies",
    "list_strategy_ids",
    "register_strategy",
]
