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

    每个策略实现 8 个方法，系统不关心任何内部细节。
    加新策略 = 新建文件夹 + strategies/__init__.py 注册 1 行，其余零改动。
    """

    # ══════════ 元信息（每个策略类自己声明） ══════════

    name: str = ""
    label: str = ""
    description: str = ""

    # ══════════ 搜索空间 ══════════

    @property
    @abstractmethod
    def param_space(self) -> ParamSpace:
        """参数搜索空间。"""
        ...

    # ══════════ 核心：连续评分 ══════════

    @abstractmethod
    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> np.ndarray:
        """Params × 指标 → (T, N, 2) float32 连续评分矩阵。"""
        ...

    # ══════════ 核心：评分→Bool信号（optimizer 的唯一调用） ══════════

    @abstractmethod
    def make_signals(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> tuple:
        """Params × 指标 → (buy_signals, sell_signals) 各 (T,N) bool。
        这是 optimizer 和 backtester 的入口。每个策略自己管理编解码逻辑。
        """
        ...

    # ══════════ 展示 ══════════

    @abstractmethod
    def scan_today(
        self, params: Params, today: dict, history=None,
    ) -> list[dict]:
        """单票今日告警。返回 [{"side","label","detail"},...]。"""
        ...

    @abstractmethod
    def to_human_readable(self, params: Params) -> str:
        """参数 → 人类可读描述。"""
        ...

    # ══════════ 默认实现（通用GA操作） ══════════

    def random_params(self, rng=None) -> Params:
        p = self.param_space.random(rng)
        p._engine = self.name
        return p

    def crossover(self, p1: Params, p2: Params, rng=None) -> Params:
        r = rng or __import__("random")
        child = {}
        for d in self.param_space.dims:
            child[d.name] = (
                p1.values.get(d.name, 0)
                if r.random() < 0.5
                else p2.values.get(d.name, 0)
            )
        return Params(values=child, _engine=self.name)

    def mutate(self, params: Params, rate: float = 0.15, rng=None) -> Params:
        r = rng or __import__("random")
        new_vals = dict(params.values)
        for d in self.param_space.dims:
            if r.random() < rate:
                new_vals[d.name] = r.randint(0, max(d.levels - 1, 0))
        return Params(values=new_vals, _engine=self.name)
