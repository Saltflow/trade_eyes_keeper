"""搜参信号函数接口 + 共享流水线。
    
SignalFn = 唯一的替换点。共享流水线 (ScoreMatrix → Decisions → PortfolioTrace → EvalMetrics)
对所有 SignalFn 实现都是一样的。

验收标准 1: 默认 global 引擎输出版式不变的日报。
验收标准 4: 旧路径已标记 deprecated 但仍可用。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════
# 类型定义
# ═══════════════════════════════════════════════════

@dataclass
class ParamDim:
    """单个参数维度。"""
    name: str
    levels: int           # 离散级别数 (0..levels-1)
    lo: float = 0.0
    hi: float = 1.0

    def decode(self, level: int) -> float:
        """整数 level → 浮点值。"""
        if self.levels <= 1:
            return self.lo
        return round(self.lo + (level / (self.levels - 1)) * (self.hi - self.lo), 6)


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
        """扁平编码需要的整数个数。"""
        return len(self.dims)

    def random(self, rng=None) -> "Params":
        r = rng or __import__("random")
        return Params(values={
            d.name: r.randint(0, max(d.levels - 1, 0)) for d in self.dims
        })


@dataclass
class Params:
    """一组具体的参数值。纯数据，可序列化到 YAML。"""
    values: dict[str, int]
    _engine: str = ""

    def to_dict(self) -> dict:
        return {"_engine": self._engine, **self.values}

    @classmethod
    def from_dict(cls, d: dict, engine: str = "") -> "Params":
        vals = {k: v for k, v in d.items() if not k.startswith("_")}
        return cls(values=vals, _engine=engine or d.get("_engine", ""))

    def decode(self, dim: ParamDim) -> float:
        return dim.decode(self.values.get(dim.name, 0))

    def clone(self) -> "Params":
        return Params(values=dict(self.values), _engine=self._engine)


@dataclass
class PortfolioTrace:
    """组合仿真轨迹 — 所有引擎共用。"""
    daily_values: np.ndarray       # (T,) 日净值
    daily_dates: list[str]         # 日日期标签
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


@dataclass
class EvalMetrics:
    """评估指标 — 从 PortfolioTrace 提炼。"""
    excess_return_pct: float
    test_excess_return: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    avg_position_pct: float
    benchmark_returns: dict[str, float]
    strategy_return: float
    final_position_pct: float
    final_holdings: list[dict] = field(default_factory=list)
    final_cash: float = 0.0
    total_nav: float = 0.0
    quarterly_holdings: list[dict] = field(default_factory=list)


# ═══════════════════════════════════════════════════
# SignalFn — 唯一可替换的接口
# ═══════════════════════════════════════════════════

class SignalFn(ABC):
    """信号函数 —— 搜参唯一需要替换的组件。

    契约:
    - evaluate() 是纯函数: Params × MarketData → ScoreMatrix
    - param_space 定义搜索边界
    - to_human_readable 保证日报可展示

    H4 保留: 评分是连续的, 但执行决策保持在二值 (买/不买)。
    H5 实施: 每个标的独立评分, 线性加权 → 单标评分, 决策时再二值化。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎标识 (global / percentile)。"""
        ...

    @property
    @abstractmethod
    def param_space(self) -> ParamSpace:
        """参数搜索空间。"""
        ...

    @abstractmethod
    def evaluate(
        self, params: Params,
        indicator_matrix: np.ndarray,  # (T, N, K)
    ) -> np.ndarray:
        """Params × Indicator → ScoreMatrix(T, N) = 每只标的每个时刻的评分。

        返回值: (T, N) float32, 值越大表示越看多。
        """
        ...

    @abstractmethod
    def to_human_readable(self, params: Params) -> str:
        """参数 → 人话描述。"""
        ...

    # ── genome 编解码（遗传搜索用；默认基于 param_space）──

    def random_params(self, rng=None) -> Params:
        """随机生成一组参数。"""
        p = self.param_space.random(rng)
        p._engine = self.name
        return p

    def crossover(self, p1: Params, p2: Params, rng=None) -> Params:
        """均匀交叉两组参数。"""
        r = rng or __import__("random")
        child = {}
        for d in self.param_space.dims:
            child[d.name] = p1.values.get(d.name, 0) if r.random() < 0.5 \
                else p2.values.get(d.name, 0)
        return Params(values=child, _engine=self.name)

    def mutate(self, params: Params, rate: float = 0.15, rng=None) -> Params:
        """按位随机重采样变异。"""
        r = rng or __import__("random")
        new_vals = dict(params.values)
        for d in self.param_space.dims:
            if r.random() < rate:
                new_vals[d.name] = r.randint(0, max(d.levels - 1, 0))
        return Params(values=new_vals, _engine=self.name)

    # ── 信号扫描 + 规则描述（显示层用）──

    def scan_signals(
        self,
        params: Params,
        today: dict[str, float],
        history=None,  # pd.DataFrame | None，标的历史（用于分位/prev 值）
    ) -> list[dict]:
        """用引擎自身逻辑判断单只标的今日是否触发买/卖信号。

        Args:
            params: 该策略的参数（引擎自有格式）
            today: 该标的今日指标 {rsi, adx, deviation, vol_ratio, ...}
            history: 该标的历史 DataFrame（分位引擎计算滚动分位需要）

        Returns:
            [{"side": "buy"|"sell", "label": 引擎自定义信号名, "detail": 触发详情}]
            默认实现返回空（引擎需覆盖）。
        """
        return []

    def describe_rules(self, params: Params) -> dict:
        """把参数翻译成买卖规则的人类可读名称（供报告/飞书展示）。

        Returns:
            {"buy": [规则名, ...], "sell": [规则名, ...]}
        """
        return {"buy": [], "sell": []}

    def engine_brief(self) -> str:
        """引擎简介（criterion 3：飞书交互 /switch_optimizer 展示买卖标准）。"""
        return self.name

    def execution_params(self, params: Params) -> dict:
        """从参数解码执行层阈值（买/卖分数阈值 + 仓位比例）。

        供 SignalFnSearchEngine + 共享流水线仿真使用。
        默认: 买卖阈值 0（评分>0即触发）, 仓位 15%。引擎可覆盖。
        """
        return {"buy_threshold": 0.0, "sell_threshold": 0.0, "position_frac": 0.15}


# ═══════════════════════════════════════════════════
# 共享流水线 — 所有 SignalFn 共用
# ═══════════════════════════════════════════════════

try:
    from numba import njit as _njit
    _HAS_NUMBA = True
except Exception:  # pragma: no cover
    _HAS_NUMBA = False

    def _njit(*args, **kwargs):
        def _wrap(fn):
            return fn
        if args and callable(args[0]):
            return args[0]
        return _wrap


@_njit(cache=True)
def _score_sim_core(
    buy_scores, sell_scores, price,
    initial_cash, buy_threshold, sell_threshold,
    position_frac, lot_size, monthly_limit, commission_rate,
    q_interval,
):
    """numba JIT 决策仿真核心（纯数值, 季度快照存 numpy 数组）。"""
    T, N = buy_scores.shape
    shares = np.zeros(N, dtype=np.float64)
    cb = np.zeros(N, dtype=np.float64)
    cash = float(initial_cash)
    daily_vals = np.zeros(T, dtype=np.float64)
    total_trades = 0
    pos_sum = 0.0
    pos_cnt = 0

    n_q = T // q_interval + 1
    q_shares = np.zeros((n_q, N), dtype=np.float64)
    q_cash = np.zeros(n_q, dtype=np.float64)
    q_nav = np.zeros(n_q, dtype=np.float64)
    q_cb = np.zeros((n_q, N), dtype=np.float64)      # 季度时点成本基础快照
    q_price = np.zeros((n_q, N), dtype=np.float64)   # 季度时点价格快照
    q_count = 0
    month_spent = 0.0

    for t in range(T):
        if t % 21 == 0:
            month_spent = 0.0

        pos_val = 0.0
        for i in range(N):
            pos_val += shares[i] * price[t, i]
        nav = cash + pos_val
        daily_vals[t] = nav
        if nav > 0:
            pos_sum += pos_val / nav * 100.0
            pos_cnt += 1

        if t > 0 and t % q_interval == 0 and q_count < n_q:
            for i in range(N):
                q_shares[q_count, i] = shares[i]
                q_cb[q_count, i] = cb[i]           # 该时点累计成本
                q_price[q_count, i] = price[t, i]  # 该时点价格
            q_cash[q_count] = cash
            q_nav[q_count] = nav
            q_count += 1

        # 买入：3日收盘均价执行；同日既触发买又触发卖 → 跳过（同日互斥）
        for i in range(N):
            buy_sig = buy_scores[t, i] > buy_threshold
            sell_sig = sell_scores[t, i] > sell_threshold
            if buy_sig and not sell_sig:
                remaining = monthly_limit - month_spent
                if remaining <= 0:
                    break
                # 买入执行价 = 近3日收盘最高价（含滑点）
                lo = t - 2 if t >= 2 else 0
                pmax = 0.0
                for tt in range(lo, t + 1):
                    pv = price[tt, i]
                    if pv > 0 and not np.isnan(pv) and pv > pmax:
                        pmax = pv
                if pmax <= 0:
                    continue
                exec_price = pmax
                # 买入额 = min(仓位比例×现金, 剩余月度额度, 现金)
                amt = position_frac * cash
                if amt > remaining:
                    amt = remaining
                if amt > cash:
                    amt = cash
                qty = int(amt / exec_price / lot_size) * lot_size
                if qty <= 0:
                    continue
                cost = qty * exec_price
                fee = cost * commission_rate
                if cash >= cost + fee and month_spent + cost + fee <= monthly_limit:
                    shares[i] += qty
                    cash -= cost + fee
                    cb[i] += cost + fee
                    month_spent += cost + fee
                    total_trades += 1

        # 卖出：单日收盘价执行；同日既买又卖 → 跳过（同日互斥）
        for i in range(N):
            buy_sig = buy_scores[t, i] > buy_threshold
            sell_sig = sell_scores[t, i] > sell_threshold
            if sell_sig and not buy_sig and shares[i] > 0:
                exec_price = price[t, i]
                if exec_price <= 0 or np.isnan(exec_price):
                    continue
                qty = int(shares[i] * position_frac / lot_size) * lot_size
                if qty > int(shares[i]):
                    qty = int(shares[i])
                if qty <= 0:
                    continue
                val = qty * exec_price
                fee = val * commission_rate
                cash += val - fee
                sold_frac = qty / shares[i] if shares[i] > 0 else 0.0
                cb[i] -= cb[i] * sold_frac
                shares[i] -= qty
                total_trades += 1

    avg_pos = pos_sum / pos_cnt if pos_cnt > 0 else 0.0
    return (daily_vals, total_trades, avg_pos, shares, cash, cb,
            q_shares[:q_count], q_cash[:q_count], q_nav[:q_count],
            q_cb[:q_count], q_price[:q_count])


def score_to_decisions_numba(
    buy_scores: np.ndarray,
    sell_scores: np.ndarray,
    price: np.ndarray,
    positions: np.ndarray,
    initial_cash: float,
    buy_threshold: float,
    sell_threshold: float,
    position_frac: float,
    lot_size: int,
    monthly_limit: float,
    commission_rate: float,
):
    """决策仿真（numba 核心 + Python 季度快照封装）。"""
    q_interval = 63
    (daily_vals, total_trades, avg_pos, shares, cash, cb,
     q_sh, q_ca, q_nav, q_cb, q_price) = _score_sim_core(
        np.ascontiguousarray(buy_scores, dtype=np.float64),
        np.ascontiguousarray(sell_scores, dtype=np.float64),
        np.ascontiguousarray(price, dtype=np.float64),
        float(initial_cash), float(buy_threshold), float(sell_threshold),
        float(position_frac), int(lot_size), float(monthly_limit),
        float(commission_rate), int(q_interval),
    )
    # numpy 季度数组 → list（保持原返回契约）
    q_shares_list = [q_sh[i].copy() for i in range(q_sh.shape[0])]
    q_cash_list = [float(q_ca[i]) for i in range(q_ca.shape[0])]
    q_nav_list = [float(q_nav[i]) for i in range(q_nav.shape[0])]
    q_cb_list = [q_cb[i].copy() for i in range(q_cb.shape[0])]
    q_price_list = [q_price[i].copy() for i in range(q_price.shape[0])]
    return (daily_vals, total_trades, float(avg_pos), shares, cash, cb,
            q_shares_list, q_cash_list, q_nav_list, q_interval,
            q_cb_list, q_price_list)


def simulate_portfolio(
    buy_scores: np.ndarray,
    sell_scores: np.ndarray,
    price: np.ndarray,
    initial_cash: float,
    buy_threshold: float,
    sell_threshold: float,
    position_frac: float,
    lot_size: int,
    monthly_limit: float,
    commission_rate: float,
    dates: list[str],
    stock_codes: list[str],
    quarterly_interval: int = 63,
) -> PortfolioTrace:
    """评分矩阵 → 仿真轨迹。

    纯函数 — 不依赖类状态 — 与 GlobalThresholdEvaluator / PercentileEvaluator 都可使用。
    """
    T, N = buy_scores.shape
    positions = np.zeros(N, dtype=np.float64)

    daily_values, total_trades, avg_pos_pct, final_shares, final_cash, cost_basis, \
        q_shares_list, q_cash_list, q_nav_list, q_interval, \
        q_cb_list, q_price_list = score_to_decisions_numba(
            buy_scores, sell_scores, price, positions, initial_cash,
            buy_threshold, sell_threshold, position_frac, lot_size,
            monthly_limit, commission_rate,
        )

    # 构建季末持仓（用季度时点的成本/价格快照，避免时点错配）
    quarterly_holdings: list[dict] = []
    for qi in range(len(q_shares_list)):
        qpos = []
        q_cb_arr = q_cb_list[qi]
        q_px_arr = q_price_list[qi]
        for i, code in enumerate(stock_codes):
            sh = q_shares_list[qi][i]
            if sh > 0.5:
                cbi = float(q_cb_arr[i])          # 该季度时点累计成本
                px = float(q_px_arr[i])           # 该季度时点价格
                unit_cost = cbi / sh if cbi > 0 and sh > 0 else 0.0
                mkt_val = sh * px
                qpos.append({
                    "code": code, "shares": round(float(sh), 1),
                    "cost": round(unit_cost, 2),
                    "price": round(px, 2),
                    "value": round(mkt_val, 2),
                    "pnl": round(mkt_val - cbi, 2) if cbi > 0 else 0.0,
                    "pnl_pct": round((mkt_val / cbi - 1) * 100, 1) if cbi > 0 else 0.0,
                })
        qp = round(q_shares_list[qi].dot(q_px_arr) / max(q_nav_list[qi], 1.0) * 100, 1) if q_nav_list[qi] > 0 else 0.0
        quarterly_holdings.append({
            "quarter": qi + 1, "day": qi * q_interval,
            "cash": round(float(q_cash_list[qi]), 2),
            "nav": round(float(q_nav_list[qi]), 2),
            "pos_pct": qp,
            "positions": qpos,
        })

    # 计算回报/回撤/夏普
    nav = daily_values
    total_return = float((nav[-1] - nav[0]) / nav[0] * 100) if nav[0] > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_series = np.where(peak > 0, (nav - peak) / peak * 100.0, 0.0)
    dd_series = dd_series[np.isfinite(dd_series)]
    dd = float(np.min(dd_series)) if len(dd_series) > 0 else 0.0
    sharpe = 0.0
    if len(nav) > 5:
        rets = np.diff(nav) / nav[:-1]
        rets = rets[~np.isnan(rets) & ~np.isinf(rets)]
        if len(rets) > 5 and np.std(rets, ddof=1) > 1e-10:
            sharpe = float(np.mean(rets) / np.std(rets, ddof=1) * np.sqrt(252))

    final_pos_pct = float(np.dot(final_shares, price[-1]) / max(nav[-1], 1.0) * 100) if nav[-1] > 0 else 0.0

    return PortfolioTrace(
        daily_values=nav,
        daily_dates=dates,
        total_trades=total_trades,
        avg_position_pct=round(avg_pos_pct, 2),
        max_drawdown_pct=round(dd, 2),
        sharpe_ratio=round(sharpe, 4),
        total_return_pct=round(total_return, 2),
        final_position_pct=round(final_pos_pct, 2),
        quarterly_holdings=quarterly_holdings,
        composition=stock_codes,
        nav_series=[round(float(v), 2) for v in nav],
        nav_dates=dates,
        cost_basis=cost_basis,
        final_shares=final_shares,
        final_cash=float(final_cash),
    )


def compute_metrics(
    trace: PortfolioTrace,
    benchmark_series: dict[str, np.ndarray] | None = None,
    risk_free_rate: float = 0.02,
) -> EvalMetrics:
    """PortfolioTrace → EvalMetrics。

    各基准收益 + 超额收益 — 与具体引擎无关。
    """
    nav = trace.daily_values
    strategy_return = (nav[-1] - nav[0]) / nav[0] * 100 if nav[0] > 0 else 0.0

    bench_returns: dict[str, float] = {}
    excess_return = strategy_return
    if benchmark_series:
        for lbl, bs in benchmark_series.items():
            if bs is not None and len(bs) > 1 and bs[0] > 0:
                br = (bs[-1] - bs[0]) / bs[0] * 100
                bench_returns[lbl] = round(br, 2)
        if bench_returns:
            primary = next(iter(bench_returns))
            excess_return = strategy_return - bench_returns[primary]

    pos_val = 0.0
    if trace.final_shares is not None and trace.composition:
        for i in range(len(trace.final_shares)):
            sh = trace.final_shares[i]
            if i < len(trace.composition):
                pass

    return EvalMetrics(
        excess_return_pct=round(excess_return, 2),
        test_excess_return=round(excess_return, 2),
        max_drawdown_pct=trace.max_drawdown_pct,
        sharpe_ratio=trace.sharpe_ratio,
        total_trades=trace.total_trades,
        avg_position_pct=trace.avg_position_pct,
        benchmark_returns=bench_returns,
        strategy_return=round(strategy_return, 2),
        final_position_pct=trace.final_position_pct,
        final_cash=trace.final_cash,
        quarterly_holdings=trace.quarterly_holdings,
    )
