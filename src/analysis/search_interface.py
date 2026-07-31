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
    # Execution is persisted alongside an optimizer artifact rather than
    # encoded as a mutable config-level index.  Keeping the resolved amount on
    # the in-memory params means a later edit to the tier list cannot silently
    # alter an already activated strategy.
    execution_snapshot: dict[str, float | int | str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"_engine": self._engine, **self.values}

    @classmethod
    def from_dict(cls, d: dict, engine: str = "") -> Params:
        vals = {k: v for k, v in d.items() if not k.startswith("_")}
        return cls(values=vals, _engine=engine or d.get("_engine", ""))

    def decode(self, dim: ParamDim) -> float:
        return dim.decode(self.values.get(dim.name, 0))

    def clone(self) -> Params:
        return Params(
            values=dict(self.values),
            _engine=self._engine,
            execution_snapshot=dict(self.execution_snapshot),
        )


@dataclass
class StrategyMarketData:
    """Immutable strategy input shared by search, reports and live scanning."""

    indicator_matrix: np.ndarray
    dates: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    prices: np.ndarray | None = None
    highs: np.ndarray | None = None
    lows: np.ndarray | None = None
    benchmark_buy_prices: np.ndarray | None = None
    tradable: np.ndarray | None = None
    date_ordinals: np.ndarray | None = None


@dataclass
class TradePlan:
    """The single strategy-to-execution contract.

    Signals, strengths and cash limits travel together.  This prevents the
    optimizer, daily report and live scanner from independently decoding the
    same parameter set (the source of the previous percentile mismatch).
    """

    buy_signals: np.ndarray
    sell_signals: np.ndarray
    buy_priority: np.ndarray
    sell_priority: np.ndarray
    buy_cash_limit: float
    sell_cash_limit: float
    warmup_rows: int
    dates: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    execution: dict[str, float | int | str] = field(default_factory=dict)
    strategy_metadata: dict[str, object] = field(default_factory=dict)
    entry_events: np.ndarray | None = None
    exit_events: np.ndarray | None = None
    force_exit_signals: np.ndarray | None = None
    conviction: np.ndarray | None = None
    target_weights: np.ndarray | None = None
    risk_atr: np.ndarray | None = None
    buy_execution_prices: np.ndarray | None = None
    sell_execution_prices: np.ndarray | None = None
    date_ordinals: np.ndarray | None = None

    def sliced(self, start: int, end: int) -> "TradePlan":
        def sliced_optional(value):
            return None if value is None else value[start:end].copy()

        buy_execution_prices = sliced_optional(self.buy_execution_prices)
        # A buy on the final test date has no in-window t+1 observation. Even
        # if full source history has a later row, the order remains pending.
        if buy_execution_prices is not None and len(buy_execution_prices):
            buy_execution_prices[-1] = np.nan
        return TradePlan(
            buy_signals=self.buy_signals[start:end],
            sell_signals=self.sell_signals[start:end],
            buy_priority=self.buy_priority[start:end],
            sell_priority=self.sell_priority[start:end],
            buy_cash_limit=self.buy_cash_limit,
            sell_cash_limit=self.sell_cash_limit,
            warmup_rows=self.warmup_rows,
            dates=self.dates[start:end],
            symbols=list(self.symbols),
            execution=dict(self.execution),
            strategy_metadata=dict(self.strategy_metadata),
            entry_events=sliced_optional(self.entry_events),
            exit_events=sliced_optional(self.exit_events),
            force_exit_signals=sliced_optional(self.force_exit_signals),
            conviction=sliced_optional(self.conviction),
            target_weights=sliced_optional(self.target_weights),
            risk_atr=sliced_optional(self.risk_atr),
            buy_execution_prices=buy_execution_prices,
            sell_execution_prices=sliced_optional(self.sell_execution_prices),
            date_ordinals=sliced_optional(self.date_ordinals),
        )


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
    final_prices: np.ndarray | None = None
    final_cash: float = 0.0
    pending_order_count: int = 0


@dataclass
class EvaluationReport:
    """单组策略评估完整报告 — 邮件/IM 渲染段的唯一数据源"""

    group: str  # "a_share" / "hk" / "us"
    engine_name: str  # "percentile" / "builder" / "simplified"
    strategy_label: str  # "分位评分"
    timestamp: str  # "2026-07-25T19:00:00"

    # 策略表现
    total_return: float
    excess_return: float
    max_drawdown: float
    sharpe_ratio: float
    trade_count: int
    avg_cash_pct: float
    pending_order_count: int = 0

    initial_asset: float = 0.0
    final_asset: float = 0.0
    final_cash: float = 0.0
    final_holdings_value: float = 0.0
    final_position_pct: float = 0.0
    final_holdings: list[dict] = field(default_factory=list)

    # 基准比较
    benchmark_returns: dict[str, float] = field(default_factory=dict)
    benchmark_win_rates: dict[str, float] = field(default_factory=dict)
    benchmark_excess_returns: dict[str, float] = field(default_factory=dict)
    benchmark_details: dict[str, dict[str, object]] = field(default_factory=dict)
    benchmark_raw_returns: dict[str, float] = field(default_factory=dict)
    primary_benchmark: str = ""

    # 组合构成
    composition: list[str] = field(default_factory=list)
    eligible_codes: list[str] = field(default_factory=list)
    warming_codes: list[str] = field(default_factory=list)
    eligible_from: dict[str, str] = field(default_factory=dict)

    # Immutable optimizer-selection metadata copied from the active artifact.
    # It documents why these parameters were selected, while the performance
    # fields above remain the strictly held-out daily validation result.
    selection_diagnostics: dict[str, object] = field(default_factory=dict)

    # 可视化
    nav_series: list[float] = field(default_factory=list)
    nav_dates: list[str] = field(default_factory=list)
    weekly_nav_ohlc: dict[str, list] = field(default_factory=dict)
    quarterly_holdings: list[dict] = field(default_factory=list)

    def to_cache_dict(self) -> dict:
        """序列化为 session._yaml_eval_cache 兼容格式"""
        return {
            "total_return": self.total_return,
            "excess_return": self.excess_return,
            "dd": self.max_drawdown,
            "sharpe": self.sharpe_ratio,
            "benchmark_returns": dict(self.benchmark_returns),
            "benchmark_win_rates": dict(self.benchmark_win_rates),
            "benchmark_excess_returns": dict(self.benchmark_excess_returns),
            "benchmark_details": dict(self.benchmark_details),
            "benchmark_raw_returns": dict(self.benchmark_raw_returns),
            "primary_benchmark": self.primary_benchmark,
            "trades": self.trade_count,
            "pending_order_count": self.pending_order_count,
            "initial_asset": self.initial_asset,
            "final_asset": self.final_asset,
            "final_cash": self.final_cash,
            "final_holdings_value": self.final_holdings_value,
            "final_position_pct": self.final_position_pct,
            "final_holdings": list(self.final_holdings),
            "weekly_nav_ohlc": dict(self.weekly_nav_ohlc),
            "composition": list(self.composition),
            "eligible_codes": list(self.eligible_codes),
            "warming_codes": list(self.warming_codes),
            "selection_diagnostics": dict(self.selection_diagnostics),
        }


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
    warmup_rows: int = 60

    def with_execution_dims(self, dims: list[ParamDim]) -> ParamSpace:
        """Attach the shared cash-tier dimensions to a strategy's signal space.

        A strategy owns only its signal/ranking parameters.  The execution
        model is deliberately supplied by the base class so adding a strategy
        never requires a branch in the optimizer, CLI or notification paths.
        """
        from .config import get_constraints

        tiers = get_constraints().discrete_search
        return ParamSpace(
            [
                *dims,
                ParamDim("buy_cash_tier", len(tiers.buy_limit_levels)),
                ParamDim("sell_cash_tier", len(tiers.sell_limit_levels)),
            ]
        )

    def execution_params(self, params: Params) -> dict[str, float | int | str]:
        """Resolve immutable per-trade cash caps for ``params``.

        Native artifacts carry a numeric snapshot.  Unsnapshotted params are
        only encountered while optimizing or in interactive previews and are
        decoded from the current tier configuration.
        """
        snapshot = dict(getattr(params, "execution_snapshot", {}) or {})
        if (
            snapshot.get("model") == "cash_cap"
            and "buy_cash_limit" in snapshot
            and "sell_cash_limit" in snapshot
        ):
            return snapshot

        from .config import get_constraints

        tiers = get_constraints().discrete_search
        buy_levels = tiers.buy_limit_levels or [10000.0]
        sell_levels = tiers.sell_limit_levels or [10000.0]
        buy_level = int(params.values.get("buy_cash_tier", 0))
        sell_level = int(params.values.get("sell_cash_tier", 0))
        return {
            "model": "cash_cap",
            "buy_cash_limit": float(buy_levels[buy_level % len(buy_levels)]),
            "sell_cash_limit": float(sell_levels[sell_level % len(sell_levels)]),
            "buy_cash_tier": buy_level,
            "sell_cash_tier": sell_level,
        }

    def make_signals(
        self, params: Params, market_data: StrategyMarketData
    ) -> TradePlan:
        """Build the only strategy decision object used by every caller."""
        if not isinstance(market_data, StrategyMarketData):
            raise TypeError("make_signals requires StrategyMarketData")
        indicator_matrix = market_data.indicator_matrix
        buy_signals, sell_signals = self._make_signal_arrays(
            params, indicator_matrix
        )
        buy_signals = np.asarray(buy_signals, dtype=bool).copy()
        sell_signals = np.asarray(sell_signals, dtype=bool).copy()
        scores = np.asarray(self.evaluate(params, indicator_matrix), dtype=float)
        if scores.shape[:2] != buy_signals.shape or scores.shape[-1] < 2:
            scores = np.zeros((*buy_signals.shape, 2), dtype=float)

        # A simultaneous signal is an exit decision, never a sell-then-buy
        # churn transaction.  Invalid/pre-warmup rows cannot generate orders.
        buy_signals[sell_signals] = False
        valid = np.isfinite(indicator_matrix[:, :, 0])
        observed = np.cumsum(valid, axis=0)
        eligible = observed >= max(1, int(self.warmup_rows))
        buy_signals &= eligible
        sell_signals &= eligible

        buy_priority = np.where(buy_signals, scores[:, :, 0], -np.inf)
        sell_priority = np.where(sell_signals, scores[:, :, 1], -np.inf)
        # Rule-based strategies may deliberately return zero score.  Their
        # active signals still receive a deterministic neutral priority.
        buy_priority[buy_signals & ~np.isfinite(buy_priority)] = 1.0
        sell_priority[sell_signals & ~np.isfinite(sell_priority)] = 1.0
        buy_priority[buy_signals & (buy_priority == 0)] = 1.0
        sell_priority[sell_signals & (sell_priority == 0)] = 1.0

        execution = self.execution_params(params)
        return TradePlan(
            buy_signals=buy_signals,
            sell_signals=sell_signals,
            buy_priority=buy_priority.astype(np.float32),
            sell_priority=sell_priority.astype(np.float32),
            buy_cash_limit=float(execution["buy_cash_limit"]),
            sell_cash_limit=float(execution["sell_cash_limit"]),
            warmup_rows=int(self.warmup_rows),
            dates=list(market_data.dates),
            symbols=list(market_data.symbols),
            execution=dict(execution),
            strategy_metadata={
                "strategy_id": self.name,
                "strategy_label": self.label,
                "parameters": dict(params.values),
            },
        )

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
    def _make_signal_arrays(
        self, params: Params, indicator_matrix: np.ndarray,
    ) -> tuple:
        """Params × 指标 → (buy_signals, sell_signals) 各 (T,N) bool。
        这是 optimizer 和 backtester 的入口。每个策略自己管理编解码逻辑。
        """
        ...

    # ══════════ 展示 ══════════

    def scan_today(
        self, params: Params, today: dict, history=None,
    ) -> list[dict]:
        """Emit the final row of the exact same plan used for backtests."""
        if history is None or len(history) < self.warmup_rows:
            return []
        try:
            from src.data.technical_indicators import compute_all
            from .backtester import _build_indicator_matrix

            computed = compute_all({"scan": history})
            indicator, _prices, _dates, _tradable = _build_indicator_matrix(
                computed, ["scan"]
            )
            if len(indicator) == 0:
                return []
            market_data = StrategyMarketData(
                indicator_matrix=indicator,
                dates=list(_dates),
                symbols=["scan"],
                prices=_prices,
                highs=(
                    indicator[:, :, 16]
                    if indicator.shape[2] > 16
                    else _prices
                ),
                lows=(
                    indicator[:, :, 17]
                    if indicator.shape[2] > 17
                    else _prices
                ),
                tradable=_tradable,
            )
            plan = self.make_signals(params, market_data)
        except (KeyError, TypeError, ValueError):
            return []

        row = len(plan.buy_signals) - 1
        results = []
        if plan.buy_signals[row, 0]:
            if plan.execution.get("model") == "target_weight":
                detail = (
                    f"buy conviction {plan.buy_priority[row, 0]:.2f}; "
                    f"target weight {plan.target_weights[row, 0]:.1%}"
                )
            else:
                detail = (
                    f"buy score {plan.buy_priority[row, 0]:.2f}; "
                    f"max cash {plan.buy_cash_limit:.0f}"
                )
            results.append(
                {
                    "side": "buy",
                    "label": self.name,
                    "priority": float(plan.buy_priority[row, 0]),
                    "detail": detail,
                }
            )
        if plan.sell_signals[row, 0]:
            results.append(
                {
                    "side": "sell",
                    "label": self.name,
                    "priority": float(plan.sell_priority[row, 0]),
                    "detail": (
                        f"sell score {plan.sell_priority[row, 0]:.2f}; "
                        f"max cash {plan.sell_cash_limit:.0f}"
                    ),
                }
            )
        return results

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

    def random_perturbations(
        self, params: Params, n: int = 10, rng=None
    ) -> list[Params]:
        """Create deterministic one-factor-at-a-time ±1 level variants."""
        variants: list[Params] = []
        for dim in self.param_space.dims:
            for delta in (-1, 1):
                values = dict(params.values)
                current = int(values.get(dim.name, 0))
                max_level = max(dim.levels - 1, 0)
                changed = max(0, min(current + delta, max_level))
                if changed == current:
                    continue
                values[dim.name] = changed
                variants.append(Params(values=values, _engine=self.name))
        return variants


def allocate_target_weights(
    scores: np.ndarray,
    active: np.ndarray,
    per_symbol_cap: float,
    total_exposure_cap: float,
) -> np.ndarray:
    """Allocate a shared exposure cap proportionally without order bias."""
    values = np.asarray(scores, dtype=float)
    enabled = np.asarray(active, dtype=bool) & np.isfinite(values) & (values > 0)
    weights = np.zeros(values.shape, dtype=np.float64)
    if not enabled.any():
        return weights
    remaining = min(max(float(total_exposure_cap), 0.0), 1.0)
    cap = min(max(float(per_symbol_cap), 0.0), 1.0)
    open_mask = enabled.copy()
    while remaining > 1e-12 and open_mask.any():
        open_scores = np.where(open_mask, values, 0.0)
        score_sum = float(open_scores.sum())
        if score_sum <= 0:
            break
        proposal = remaining * open_scores / score_sum
        room = np.maximum(cap - weights, 0.0)
        addition = np.minimum(proposal, room)
        weights += addition
        used = float(addition.sum())
        if used <= 1e-12:
            break
        remaining -= used
        open_mask &= weights < cap - 1e-12
    return np.round(weights, 12)
