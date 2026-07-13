"""SignalFnSearchEngine — 把 SignalFn 适配成遗传搜索器的 StrategyEngine 插件。

范围 A 核心桥接：让 PercentileSignalFn (以及任意 SignalFn) 真正进入遗传搜索。
- "编码" 就是 Params（引擎自有参数空间的整数级别 dict）
- evaluate_encoding: signal_fn.evaluate() → 共享流水线 simulate_portfolio/compute_metrics
  → WindowStats（遗传搜索器可直接排序/约束）

全局阈值引擎不使用本适配器（engine=None → 走旧向量化路径, criterion 1/2 逻辑零改动）。
"""
from __future__ import annotations

import logging
from collections import OrderedDict

import numpy as np

from .strategy_interface import StrategyEngine
from .signal_functions import (
    SignalFn, Params, simulate_portfolio, compute_metrics,
)
from .optimizer_constraints import WindowStats

logger = logging.getLogger(__name__)


class SignalFnSearchEngine(StrategyEngine):
    """将 SignalFn 包装为可搜索的 StrategyEngine。

    encoding 类型 = Params（signal_fn.param_space 的整数级别 dict）。
    """

    def __init__(self, signal_fn: SignalFn, initial_cash: float = 100000.0,
                 lot_size: int = 100, monthly_limit: float = 100000.0,
                 commission_rate: float = 0.005):  # 0.5% 含滑点
        self.signal_fn = signal_fn
        self.initial_cash = initial_cash
        self.lot_size = lot_size
        self.monthly_limit = monthly_limit
        self.commission_rate = commission_rate
        self._rng = __import__("random").Random(42)
        self.fx_rate = 1.0  # 汇率乘数（优化器按组设定）

    # ── 编码操作 ──

    def param_count(self) -> int:
        return self.signal_fn.param_space.flat_size()

    def random_encoding(self, ds_cfg) -> Params:
        return self.signal_fn.random_params(self._rng)

    def crossover_encoding(self, p1: Params, p2: Params) -> Params:
        return self.signal_fn.crossover(p1, p2, self._rng)

    def mutate_encoding(self, encoding: Params, ds_cfg) -> Params:
        return self.signal_fn.mutate(encoding, rng=self._rng)

    def to_human_readable(self, encoding: Params, ds_cfg) -> str:
        return self.signal_fn.to_human_readable(encoding)

    # ── 评估：SignalFn.evaluate → 共享流水线 → WindowStats ──

    def evaluate_encoding(
        self, encoding: Params, windows, ds_cfg, constraints,
        evaluator, wf_manager,
    ) -> tuple[list[WindowStats], float] | None:
        exec_p = self.signal_fn.execution_params(encoding)
        buy_th = float(exec_p.get("buy_threshold", 0.0))
        sell_th = float(exec_p.get("sell_threshold", 0.0))
        pos_frac = float(exec_p.get("position_frac", 0.15))

        lot = getattr(evaluator, "lot_size", self.lot_size)
        init_cash = getattr(evaluator, "initial_cash", self.initial_cash)
        monthly = self.monthly_limit  # 搜参月额度（默认 100000，与旧 global 搜参一致）
        comm = getattr(evaluator, "commission_rate", self.commission_rate)

        rf_rate = getattr(constraints, "risk_free_rate", 0.02)
        codes = list(getattr(wf_manager, "stock_codes", []))

        all_stats: list[WindowStats] = []
        for w in windows:
            test_ind = wf_manager.build_matrices(w, "test")
            test_price = wf_manager.get_price_matrix(w, "test")
            T, N = test_ind.shape[:2]
            if T == 0 or N == 0:
                continue

            # 评分矩阵 (T, N, 2) = [buy_scores, sell_scores]
            scores = self.signal_fn.evaluate(encoding, test_ind)
            buy_scores = np.ascontiguousarray(scores[:, :, 0], dtype=np.float64)
            sell_scores = np.ascontiguousarray(scores[:, :, 1], dtype=np.float64)
            price = np.ascontiguousarray(test_price, dtype=np.float64) * self.fx_rate

            trace = simulate_portfolio(
                buy_scores, sell_scores, price,
                init_cash, buy_th, sell_th, pos_frac,
                lot, monthly, comm,
                dates=[""] * T,
                stock_codes=codes[:N] if len(codes) >= N else [str(i) for i in range(N)],
            )

            # 基准序列（超额收益）
            train_ind = wf_manager.build_matrices(w, "train")
            rf_daily = rf_rate / 252.0
            train_end_cash = init_cash * (1.0 + rf_daily) ** train_ind.shape[0]
            benchmarks: OrderedDict = OrderedDict()
            for bcode in getattr(constraints, "benchmark_codes", []):
                if bcode == "risk_free":
                    benchmarks["risk_free"] = (
                        np.cumsum(np.ones(T) * train_end_cash * rf_daily) + train_end_cash
                    )
                else:
                    bc = wf_manager.get_benchmark_price(bcode, w, "test")
                    if bc is not None and len(bc) == T and not np.isnan(bc[0]):
                        benchmarks[bcode] = bc

            metrics = compute_metrics(
                trace, benchmark_series=benchmarks or None, risk_free_rate=rf_rate,
            )

            ws = WindowStats(
                test_excess_return=metrics.test_excess_return,
                max_drawdown_pct=metrics.max_drawdown_pct,
                avg_position_pct=metrics.avg_position_pct,
                sharpe_ratio=metrics.sharpe_ratio,
                total_trades=metrics.total_trades,
                test_months=getattr(constraints.walk_forward, "test_months", 9)
                if hasattr(constraints, "walk_forward") else 9,
                benchmark_returns=metrics.benchmark_returns,
                strategy_return=metrics.strategy_return,
                final_position_pct=metrics.final_position_pct,
                final_shares=trace.final_shares,
                final_cash=trace.final_cash,
                cost_basis=trace.cost_basis,
            )
            all_stats.append(ws)

        if not all_stats:
            return None

        wf_score = self._compute_wf_score(all_stats, constraints)
        return all_stats, wf_score

    @staticmethod
    def _compute_wf_score(stats_list: list[WindowStats], constraints) -> float:
        wf_cfg = getattr(constraints, "walk_forward", None)
        v_win = getattr(wf_cfg, "validation_windows", 0) if wf_cfg else 0
        ranking = stats_list[:-v_win] if v_win > 0 and len(stats_list) > v_win else stats_list
        returns = [s.test_excess_return for s in ranking]
        if not returns:
            return -float("inf")
        weights = list(getattr(wf_cfg, "window_weights", []) or [])[:len(returns)]
        if sum(weights) > 0:
            weights = [x / sum(weights) for x in weights]
        else:
            weights = [1.0 / len(returns)] * len(returns)
        mean_r = sum(r * x for r, x in zip(returns, weights))
        std_r = float(np.std(returns)) if len(returns) >= 2 else 0.0
        penalty = getattr(wf_cfg, "stability_penalty", 0.0) if wf_cfg else 0.0
        return mean_r - penalty * std_r
