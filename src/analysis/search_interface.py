"""搜参策略接口 — 所有策略唯一的替换点。

契约：
  - evaluate(params, indicator_matrix) → (buy_scores, sell_scores)  纯函数
  - scan_today(params, today, history) → list[dict]                 告警接口
  - param_space / to_human_readable                               元数据

中间状态（锁/重置/持仓日）由各策略内部管理，系统不干预。
遗传搜索的编码/交叉/变异由基类提供默认实现（基于 param_space）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


# ═══════════════════════════════════════════════════════
# 参数空间定义
# ═══════════════════════════════════════════════════════

@dataclass
class ParamDim:
    """单个参数维度。"""
    name: str
    levels: int  # 离散级别数 (0..levels-1)
    lo: float = 0.0
    hi: float = 1.0

    def decode(self, level: int) -> float:
        if self.levels <= 1:
            return self.lo
        return round(
            self.lo + (level / (self.levels - 1)) * (self.hi - self.lo), 6
        )


@dataclass
class ParamSpace:
    """参数搜索空间。"""
    dims: list[ParamDim]

    def total_levels(self) -> int:
        n = 1
        for d in self.dims:
            n *= d.levels
        return n

    def flat_size(self) -> int:
        return len(self.dims)

    def random(self, rng=None) -> Params:
        r = rng or __import__("random")
        return Params(
            values={
                d.name: r.randint(0, max(d.levels - 1, 0)) for d in self.dims
            }
        )


@dataclass
class Params:
    """一组具体的参数值。纯数据，可序列化到 YAML。"""
    values: dict[str, int]
    _engine: str = ""

    def to_dict(self) -> dict:
        return {"_engine": self._engine, **self.values}

    @classmethod
    def from_dict(cls, d: dict, engine: str = "") -> Params:
        vals = {k: v for k, v in d.items() if not k.startswith("_")}
        return cls(values=vals, _engine=engine or d.get("_engine", ""))

    def decode(self, dim: ParamDim) -> float:
        return dim.decode(self.values.get(dim.name, 0))

    def clone(self) -> Params:
        return Params(values=dict(self.values), _engine=self._engine)


# ═══════════════════════════════════════════════════════
# 回测结果载体
# ═══════════════════════════════════════════════════════

@dataclass
class PortfolioTrace:
    """组合仿真轨迹 — 所有引擎共用。"""
    daily_values: np.ndarray
    daily_dates: list[str]
    total_trades: int
    avg_position_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_return_pct: float
    final_position_pct: float
    quarterly_holdings: list[dict]
    composition: list[str]
    nav_series: list[float] = field(default_factory=list)
    nav_dates: list[str] = field(default_factory=list)
    cost_basis: np.ndarray | None = None
    final_shares: np.ndarray | None = None
    final_cash: float = 0.0


# ═══════════════════════════════════════════════════════
# 搜参策略抽象基类
# ═══════════════════════════════════════════════════════

class SearchStrategy(ABC):
    """搜参策略 —— 系统唯一可替换的接口。

    每个策略只需实现 4 个核心方法：
      name, param_space, evaluate, scan_today, to_human_readable。

    遗传搜索操作（random_params / crossover / mutate）由基类提供默认实现，
    基于 param_space 自动完成编解码。
    """

    # ── 核心接口（子类必须实现）──

    @property
    @abstractmethod
    def name(self) -> str:
        """策略标识 (percentile / builder / simplified)。"""
        ...

    @property
    @abstractmethod
    def param_space(self) -> ParamSpace:
        """参数搜索空间。"""
        ...

    @abstractmethod
    def evaluate(
        self,
        params: Params,
        indicator_matrix: np.ndarray,  # (T, N, K)
    ) -> np.ndarray:
        """Params × 天级指标矩阵 → 评分矩阵。

        Returns:
            (T, N, 2) float32 — [:, :, 0] = buy 评分, [:, :, 1] = sell 评分。
            评分值域 [0, 1]，越高表示信号越强。
        """
        ...

    @abstractmethod
    def scan_today(
        self,
        params: Params,
        today: dict[str, float],
        history=None,
    ) -> list[dict]:
        """今日单票告警扫描。

        Returns:
            [{"side": "buy"|"sell", "label": ..., "detail": ...}, ...]
            空列表 = 今日无信号。
        """
        ...

    @abstractmethod
    def to_human_readable(self, params: Params) -> str:
        """参数 → 人类可读描述。"""
        ...

    # ── 遗传操作用默认实现（基于 param_space）──

    def random_params(self, rng=None) -> Params:
        p = self.param_space.random(rng)
        p._engine = self.name
        return p

    def crossover(self, p1: Params, p2: Params, rng=None) -> Params:
        """均匀交叉两组参数。"""
        r = rng or __import__("random")
        child = {}
        for d in self.param_space.dims:
            child[d.name] = (
                p1.values.get(d.name, 0)
                if r.random() < 0.5
                else p2.values.get(d.name, 0)
            )
        return Params(values=child, _engine=self.name)

    def mutate(
        self, params: Params, rate: float = 0.15, rng=None
    ) -> Params:
        """按位随机重采样变异。"""
        r = rng or __import__("random")
        new_vals = dict(params.values)
        for d in self.param_space.dims:
            if r.random() < rate:
                new_vals[d.name] = r.randint(0, max(d.levels - 1, 0))
        return Params(values=new_vals, _engine=self.name)
