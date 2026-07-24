"""SignalFnSearchEngine — 把 SignalFn 适配成遗传搜索器的 StrategyEngine 插件。

范围 A 核心桥接：让 PercentileSignalFn (以及任意 SignalFn) 真正进入遗传搜索。
- encoding = Params（引擎自有参数空间的整数级别 dict）
- evaluate_encoding: signal_fn.evaluate() → 阈值二值化 → FastEvaluator.evaluate()
  → 统一模拟引擎 → WindowStats（统一产出：含季度持仓快照）
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .optimizer import StrategyEncoding
    from .backtester import FastEvaluator
    from .config import StrategyConstraints, DiscreteSearchConfig

from .signal_functions import (
    SignalFn,
    Params,
)
from .config import WindowStats

logger = logging.getLogger(__name__)


class StrategyEngine(ABC):
    """策略评估的纯函数接口。
    每个引擎实例管理一种参数空间 + 参数→评估的翻译逻辑。
    """

    @abstractmethod
    def param_count(self) -> int:
        ...

    @abstractmethod
    def random_encoding(self, ds_cfg) -> "StrategyEncoding":
        ...

    @abstractmethod
    def evaluate_encoding(
        self,
        encoding: "StrategyEncoding",
        windows,
        ds_cfg: "DiscreteSearchConfig",
        constraints: "StrategyConstraints",
        evaluator: "FastEvaluator",
        wf_manager,
    ) -> tuple[list["WindowStats"], float] | None:
        ...

    @abstractmethod
    def crossover_encoding(
        self, p1: "StrategyEncoding", p2: "StrategyEncoding",
    ) -> "StrategyEncoding":
        ...

    @abstractmethod
    def mutate_encoding(
        self, encoding: "StrategyEncoding", ds_cfg,
    ) -> "StrategyEncoding":
        ...

    @abstractmethod
    def to_human_readable(self, encoding: "StrategyEncoding", ds_cfg) -> str:
        ...


class SignalFnSearchEngine(StrategyEngine):
    """将 SignalFn 包装为可搜索的 StrategyEngine。

    encoding 类型 = Params（signal_fn.param_space 的整数级别 dict）。
    """

    def __init__(
        self,
        signal_fn: SignalFn,
        initial_cash: float = 100000.0,
        lot_size: int = 100,
        monthly_limit: float | None = None,
        commission_rate: float | None = None,
    ):
        from .config import get_execution_config

        cfg = get_execution_config()
        self.signal_fn = signal_fn
        self.initial_cash = initial_cash
        self.lot_size = lot_size
        self.monthly_limit = (
            monthly_limit if monthly_limit is not None else cfg.monthly_buy_limit
        )
        self.commission_rate = (
            commission_rate if commission_rate is not None else cfg.commission_rate
        )
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
        self,
        encoding: Params,
        windows,
        ds_cfg,
        constraints,
        evaluator,
        wf_manager,
    ) -> tuple[list[WindowStats], float] | None:
        exec_p = self.signal_fn.execution_params(encoding)
        buy_th = float(exec_p.get("buy_threshold", 0.0))
        sell_th = float(exec_p.get("sell_threshold", 0.0))
        pos_frac = float(exec_p.get("position_frac", 0.15))

        rf_rate = getattr(constraints, "risk_free_rate", 0.02)
        all_stats: list[WindowStats] = []

        for w in windows:
            test_ind = wf_manager.build_matrices(w, "test")
            test_price = wf_manager.get_price_matrix(w, "test")
            T, N = test_ind.shape[:2]
            if T == 0 or N == 0:
                continue

            # 评分矩阵 → boolean 信号
            scores = self.signal_fn.evaluate(encoding, test_ind)
            buy_scores = scores[:, :, 0]
            sell_scores = scores[:, :, 1]
            buy_signals = buy_scores > buy_th
            sell_signals = sell_scores > sell_th

            # 现金基准线（与 genetic_searcher 一致）
            train_ind = wf_manager.build_matrices(w, "train")
            rf_daily = rf_rate / 252.0
            train_end_cash = evaluator.initial_cash * (1.0 + rf_daily) ** train_ind.shape[0]
            cash_baseline = (
                np.cumsum(np.ones(T) * train_end_cash * rf_daily) + train_end_cash
            )

            # 基准序列
            from collections import OrderedDict

            benchmark_series = OrderedDict()
            for bcode in getattr(constraints, "benchmark_codes", []):
                if bcode == "risk_free":
                    benchmark_series["risk_free"] = (
                        np.cumsum(np.ones(T) * train_end_cash * rf_daily)
                        + train_end_cash
                    )
                else:
                    bc = wf_manager.get_benchmark_price(bcode, w, "test")
                    if bc is not None and len(bc) == T and not np.isnan(bc[0]):
                        benchmark_series[bcode] = bc

            # 统一引擎评估
            stats = evaluator.evaluate(
                test_ind,
                test_price,
                cash_baseline,
                buy_score_signals=buy_signals,
                sell_score_signals=sell_signals,
                buy_fracs=[pos_frac],
                sell_fracs=[pos_frac],
                benchmark_series=benchmark_series if benchmark_series else None,
            )
            all_stats.append(stats)

        if not all_stats:
            return None

        wf_score = self._compute_wf_score(all_stats, constraints)
        return all_stats, wf_score

    @staticmethod
    def _compute_wf_score(stats_list: list[WindowStats], constraints) -> float:
        wf_cfg = getattr(constraints, "walk_forward", None)
        v_win = getattr(wf_cfg, "validation_windows", 0) if wf_cfg else 0
        ranking = (
            stats_list[:-v_win] if v_win > 0 and len(stats_list) > v_win else stats_list
        )
        returns = [s.test_excess_return for s in ranking]
        if not returns:
            return -float("inf")
        weights = list(getattr(wf_cfg, "window_weights", []) or [])[: len(returns)]
        if sum(weights) > 0:
            weights = [x / sum(weights) for x in weights]
        else:
            weights = [1.0 / len(returns)] * len(returns)
        mean_r = sum(r * x for r, x in zip(returns, weights))
        std_r = float(np.std(returns)) if len(returns) >= 2 else 0.0
        penalty = getattr(wf_cfg, "stability_penalty", 0.0) if wf_cfg else 0.0
        return mean_r - penalty * std_r
