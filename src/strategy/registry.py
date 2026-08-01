"""Automatic discovery and construction of trading-strategy plugins."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules

from .api import TradingStrategy

_STRATEGIES: dict[str, type[TradingStrategy]] = {}
_DISCOVERED = False


def register_strategy(strategy_id: str):
    """Register one concrete TradingStrategy class."""
    normalized = str(strategy_id).strip().lower()
    if not normalized:
        raise ValueError("strategy_id cannot be empty")

    def decorator(strategy_type: type[TradingStrategy]):
        if not issubclass(strategy_type, TradingStrategy):
            raise TypeError("registered strategy must inherit TradingStrategy")
        current = _STRATEGIES.get(normalized)
        if current is not None and current is not strategy_type:
            raise ValueError(f"duplicate strategy registration: {normalized}")
        if strategy_type.name and strategy_type.name != normalized:
            raise ValueError(
                f"strategy id mismatch: decorator={normalized!r}, "
                f"class={strategy_type.name!r}"
            )
        strategy_type.name = normalized
        _STRATEGIES[normalized] = strategy_type
        return strategy_type

    return decorator


def discover_strategies() -> None:
    """Import every concrete module under ``src.strategy.plugins`` once."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    package_dir = Path(__file__).parent / "plugins"
    for module in iter_modules([str(package_dir)]):
        if module.name.startswith("_"):
            continue
        import_module(f"{__package__}.plugins.{module.name}")
    _DISCOVERED = True


def get_strategy(name: str) -> TradingStrategy | None:
    discover_strategies()
    strategy_type = _STRATEGIES.get(str(name).strip().lower())
    return strategy_type() if strategy_type else None


def list_strategy_ids() -> tuple[str, ...]:
    discover_strategies()
    return tuple(sorted(_STRATEGIES))


def list_strategies() -> list[dict[str, str]]:
    discover_strategies()
    return [
        {
            "key": strategy_id,
            "label": strategy_type.label or strategy_id,
            "description": strategy_type.description,
        }
        for strategy_id, strategy_type in sorted(_STRATEGIES.items())
    ]
