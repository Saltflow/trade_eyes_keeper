"""统一执行配置读取器。

搜参（StrategyOptimizerV2/SignalFnSearchEngine）和日回报测（PortfolioEvaluator）
统一从此模块读取执行参数，禁止代码写死覆盖。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("config/optimizer_constraints.yaml")


@dataclass
class ExecutionConfig:
    """搜参/日回报测通用执行参数。"""
    monthly_buy_limit: float = 15000.0
    initial_capital: float = 100000.0
    commission_rate: float = 0.005
    lot_sizes: dict[str, int] = field(default_factory=lambda: {
        "a_share": 100, "hk": 100, "us": 1,
    })
    fx_rates: dict[str, float] = field(default_factory=lambda: {
        "a_share": 1.0, "hk": 0.9, "us": 7.0,
    })


def load_execution_config(
    path: Path | str | None = None,
) -> ExecutionConfig:
    """从 optimizer_constraints.yaml 读取执行参数，缺失键返回默认值。"""
    import yaml

    cfg_path = Path(path) if path else _DEFAULT_PATH
    if not cfg_path.exists():
        logger.warning("执行配置文件 %s 不存在，使用默认值", cfg_path)
        return ExecutionConfig()

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    ep = raw.get("execution_params", {}) or {}
    return ExecutionConfig(
        monthly_buy_limit=float(ep.get("monthly_buy_limit", 15000.0)),
        initial_capital=float(ep.get("initial_capital", 100000.0)),
        commission_rate=float(ep.get("commission_rate", 0.005)),
        lot_sizes=dict(ep.get("lot_sizes", {}) or {}),
        fx_rates=dict(ep.get("fx_rates", {}) or {}),
    )


# 模块级单例（首次访问时加载，避免重复 IO）
_exec_config: ExecutionConfig | None = None


def get_execution_config() -> ExecutionConfig:
    global _exec_config
    if _exec_config is None:
        _exec_config = load_execution_config()
    return _exec_config


def reload_execution_config(path: Path | str | None = None) -> ExecutionConfig:
    """强制重新加载（/config 命令修改后调用）。"""
    global _exec_config
    _exec_config = load_execution_config(path)
    return _exec_config
