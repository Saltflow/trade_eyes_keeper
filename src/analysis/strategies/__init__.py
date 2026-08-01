"""搜参策略注册表 — 加新策略只需在此 STRATEGIES dict 加 1 行。

main.py / optimizer.py / handlers.py 通过 get_strategy() / list_strategies()
发现策略，不直接 import 任何具体策略类。
"""

from .percentile.engine import PercentileSearchStrategy
from .builder.engine import BuilderSearchStrategy
from .simplified.engine import SimplifiedSearchStrategy
from .regime_pullback.engine import RegimePullbackStrategy
from .technical_ensemble.engine import TechnicalEnsembleStrategy

STRATEGIES: dict[str, type] = {
    "percentile": PercentileSearchStrategy,
    "builder": BuilderSearchStrategy,
    "simplified": SimplifiedSearchStrategy,
    "regime_pullback": RegimePullbackStrategy,
    "technical_ensemble": TechnicalEnsembleStrategy,
}


def get_strategy(name: str):
    """按名获取策略实例。不存在的 key 返回 None。"""
    cls = STRATEGIES.get(name)
    return cls() if cls else None


def list_strategies() -> list[dict]:
    """返回所有可用策略的元信息列表，供 /mode 等展示。"""
    result = []
    for key, cls in STRATEGIES.items():
        inst = cls()
        result.append({
            "key": key,
            "label": getattr(inst, "label", key),
            "description": getattr(inst, "description", ""),
        })
    return result
