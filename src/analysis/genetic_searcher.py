"""
遗传搜索器 (GeneticSearcher)

三阶段策略优化:
  Phase 1 (粗筛): 生成 N 个随机策略，用向量化评估器在所有 Walk-Forward 窗口打分
  Phase 2 (遗传): 从 Top-K 通过交叉/变异生成新策略，迭代 G 代
  Phase 3 (精确验证): 对最终 Top-K 用精确回测验证

用法:
    from src.analysis.genetic_searcher import GeneticSearcher
    searcher = GeneticSearcher(constraints, wf_manager, fast_evaluator)
    results = searcher.run()
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .optimizer_constraints import (
    StrategyConstraints,
    WalkForwardConfig,
    GeneticSearchConfig,
    DiscreteSearchConfig,
    WindowStats,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# 策略编码
# ════════════════════════════════════════════════════════════


@dataclass
class StrategyEncoding:
    """策略的离散编码（买入+卖出+仓位目标）

    买入规则: (builder_idx, threshold_level, frac_level) × num_buy_rules
    卖出规则: (builder_idx, threshold_level, frac_level) × num_sell_rules
    仓位控制: position_slope (0-19), position_bias (0-19)
    """

    # 买入
    buy_builders: list[int]
    buy_thresholds: list[int]
    buy_fracs: list[int]

    # 卖出
    sell_builders: list[int] = field(default_factory=list)
    sell_thresholds: list[int] = field(default_factory=list)
    sell_fracs: list[int] = field(default_factory=list)

    # 仓位目标模型（可选，0=未启用）
    position_slope: int = 0
    position_bias: int = 0

    @property
    def uses_position_target(self) -> bool:
        """是否启用仓位目标模式"""
        return self.position_slope > 0 or self.position_bias != 0

    @property
    def n_buy_rules(self) -> int:
        return len(self.buy_builders)

    @property
    def n_sell_rules(self) -> int:
        return len(self.sell_builders)

    def to_flat(self) -> list[int]:
        """扁平化为一维列表（含仓位参数）"""
        result = []
        for i in range(self.n_buy_rules):
            result.extend([self.buy_builders[i], self.buy_thresholds[i], self.buy_fracs[i]])
        for i in range(self.n_sell_rules):
            result.extend([self.sell_builders[i], self.sell_thresholds[i], self.sell_fracs[i]])
        # 仓位目标参数（2维）
        result.extend([self.position_slope, self.position_bias])
        return result

    @classmethod
    def from_flat(
        cls, flat: list[int], n_buy: int = 5, n_sell: int = 3,
    ) -> "StrategyEncoding":
        """从一维列表恢复"""
        total_expected = n_buy * 3 + n_sell * 3 + 2
        p = 0
        buy_builders = [flat[p + i * 3] for i in range(n_buy)]
        buy_thresholds = [flat[p + i * 3 + 1] for i in range(n_buy)]
        buy_fracs = [flat[p + i * 3 + 2] for i in range(n_buy)]
        p = n_buy * 3
        sell_builders = [flat[p + i * 3] for i in range(n_sell)]
        sell_thresholds = [flat[p + i * 3 + 1] for i in range(n_sell)]
        sell_fracs = [flat[p + i * 3 + 2] for i in range(n_sell)]
        p = n_buy * 3 + n_sell * 3
        # 仓位目标参数（兼容旧编码：无此字段时默认 0,0）
        pos_slope = flat[p] if len(flat) > p else 0
        pos_bias = flat[p + 1] if len(flat) > p + 1 else 0
        return cls(
            buy_builders=buy_builders, buy_thresholds=buy_thresholds, buy_fracs=buy_fracs,
            sell_builders=sell_builders, sell_thresholds=sell_thresholds, sell_fracs=sell_fracs,
            position_slope=pos_slope, position_bias=pos_bias,
        )

    def to_buy_params(self, ds_cfg: DiscreteSearchConfig) -> tuple[list[str], list[float], list[float]]:
        """转换为 FastEvaluator 买入参数"""
        builder_names = [ds_cfg.buy_builders[i] for i in self.buy_builders]
        threshold_vals = [i / (ds_cfg.threshold_levels - 1) if ds_cfg.threshold_levels > 1 else 0.0
                          for i in self.buy_thresholds]
        frac_vals = [ds_cfg.frac_levels[i] for i in self.buy_fracs]
        return builder_names, threshold_vals, frac_vals

    def to_sell_params(self, ds_cfg: DiscreteSearchConfig) -> tuple[list[str], list[float], list[float]]:
        """转换为 FastEvaluator 卖出参数"""
        if not self.sell_builders:
            return [], [], []
        builder_names = [ds_cfg.sell_builders[i] for i in self.sell_builders]
        threshold_vals = [i / (ds_cfg.threshold_levels - 1) if ds_cfg.threshold_levels > 1 else 0.0
                          for i in self.sell_thresholds]
        frac_vals = [ds_cfg.sell_frac_levels[i] for i in self.sell_fracs]
        return builder_names, threshold_vals, frac_vals

    def to_position_params(self, ds_cfg: DiscreteSearchConfig) -> tuple[float, float]:
        """转换为 Position-Target 模式参数（slope, bias 浮点值）"""
        slope_min, slope_max = 0.5, 10.0
        bias_min, bias_max = -3.0, 3.0
        slope = slope_min + (self.position_slope / max(ds_cfg.position_slope_levels - 1, 1)) * (slope_max - slope_min)
        bias = bias_min + (self.position_bias / max(ds_cfg.position_bias_levels - 1, 1)) * (bias_max - bias_min)
        return slope, bias

    def clone(self) -> "StrategyEncoding":
        return StrategyEncoding(
            buy_builders=list(self.buy_builders),
            buy_thresholds=list(self.buy_thresholds),
            buy_fracs=list(self.buy_fracs),
            sell_builders=list(self.sell_builders),
            sell_thresholds=list(self.sell_thresholds),
            sell_fracs=list(self.sell_fracs),
            position_slope=self.position_slope,
            position_bias=self.position_bias,
        )


# ════════════════════════════════════════════════════════════
# 并行评估辅助（模块级，确保 Windows multiprocessing 可 pickle）
# ════════════════════════════════════════════════════════════

def _eval_encoding_worker(args: tuple) -> tuple[list, float] | None:
    """Pickle-safe worker: 在子进程中评估单个 StrategyEncoding。

    Args:
        args: (encoding_flat, window_data, ds_cfg_raw, constraints_raw, eval_kwargs, mode)

    Returns:
        (window_stats_list, wf_score) or None if all windows empty
    """
    encoding_flat, window_data, ds_cfg_raw, c_raw, eval_kwargs, use_pt = args

    # 重建 encoding（Worker 进程无共享内存，需新建）
    encoding = StrategyEncoding.from_flat(encoding_flat, n_buy=ds_cfg_raw['num_buy_rules'], n_sell=ds_cfg_raw['num_sell_rules'])

    # 重建 DiscreteSearchConfig
    ds_cfg = DiscreteSearchConfig(ds_cfg_raw)

    # 重建 StrategyConstraints
    from .optimizer_constraints import StrategyConstraints
    constraints = StrategyConstraints(c_raw)

    # 重建 FastEvaluator
    from .fast_evaluator import FastEvaluator
    evaluator = FastEvaluator(**eval_kwargs)

    all_stats: list = []
    buy_names, buy_thresh, buy_fracs = encoding.to_buy_params(ds_cfg)
    sell_names, sell_thresh, sell_fracs = encoding.to_sell_params(ds_cfg)
    pos_slope, pos_bias = encoding.to_position_params(ds_cfg) if use_pt else (1.0, 0.0)

    for test_ind, test_price, cash_baseline, benchmarks in window_data:
        if test_ind.shape[0] == 0 or test_ind.shape[1] == 0:
            continue

        if use_pt and benchmarks:
            stats = evaluator.evaluate_position_target(
                test_ind, test_price, cash_baseline,
                buy_names, buy_thresh,
                sell_names, sell_thresh,
                position_slope=pos_slope, position_bias=pos_bias,
                benchmark_series=benchmarks,
            )
        elif use_pt:
            stats = evaluator.evaluate_position_target(
                test_ind, test_price, cash_baseline,
                buy_names, buy_thresh,
                sell_names, sell_thresh,
                position_slope=pos_slope, position_bias=pos_bias,
            )
        elif benchmarks:
            stats = evaluator.evaluate(
                test_ind, test_price, cash_baseline,
                buy_names, buy_thresh, buy_fracs,
                sell_names, sell_thresh, sell_fracs,
                benchmark_series=benchmarks,
            )
        else:
            stats = evaluator.evaluate(
                test_ind, test_price, cash_baseline,
                buy_names, buy_thresh, buy_fracs,
                sell_names, sell_thresh, sell_fracs,
            )
        all_stats.append(stats)

    if not all_stats:
        return None

    # 计算 WF 得分
    returns = [s.test_excess_return for s in all_stats]
    weights = constraints.walk_forward.window_weights[:len(returns)]
    total_w = sum(weights)
    weights = [w / total_w for w in weights] if total_w > 0 else [1.0 / len(returns)] * len(returns)
    mean_return = sum(r * w for r, w in zip(returns, weights))
    std_return = float(np.std(returns)) if len(returns) >= 2 else 0.0
    wf_score = mean_return - constraints.walk_forward.stability_penalty * std_return

    return all_stats, wf_score


# ════════════════════════════════════════════════════════════
# 主搜索器
# ════════════════════════════════════════════════════════════


class GeneticSearcher:
    """遗传搜索器

    在 Walk-Forward 框架下，使用遗传算法搜索最优买卖策略。
    """

    def __init__(
        self,
        constraints: StrategyConstraints,
        wf_manager,  # WalkForwardManager
        fast_evaluator,  # FastEvaluator
    ):
        self.cfg = constraints.genetic_search
        self.ds_cfg = constraints.discrete_search
        self.wf_cfg = constraints.walk_forward
        self.wf_manager = wf_manager
        self.evaluator = fast_evaluator
        self.constraints = constraints
        self._rng = random.Random(42)  # 可复现种子
        self._window_data_cache: list | None = None  # 并行评估预提取

    # ════════════════════════════════════════════════════════
    # 并行评估辅助
    # ════════════════════════════════════════════════════════

    def _prepare_window_data(self, windows) -> list:
        """预提取 Walk-Forward 矩阵为可 pickle 的数据包。

        每个窗口打包: (test_indicator, test_price, cash_baseline, benchmark_series)
        避免向子进程传递 WalkForwardManager（含大矩阵，pickle 开销大）。
        """
        from collections import OrderedDict

        if self._window_data_cache is not None:
            return self._window_data_cache

        data = []
        rf_rate = getattr(self.constraints, "risk_free_rate", 0.02)

        for w in windows:
            train_ind = self.wf_manager.build_matrices(w, "train")
            test_ind = self.wf_manager.build_matrices(w, "test")
            test_price = self.wf_manager.get_price_matrix(w, "test")
            T_test = test_ind.shape[0]

            if T_test == 0 or test_ind.shape[1] == 0:
                data.append((test_ind, test_price, np.array([]), {}))
                continue

            # cash_baseline
            rf_daily = rf_rate / 252.0
            train_end_cash = self.evaluator.initial_cash * (1.0 + rf_daily) ** train_ind.shape[0]
            cash_baseline = np.cumsum(np.ones(T_test) * train_end_cash * rf_daily) + train_end_cash

            # benchmark_series
            benchmarks = OrderedDict()
            for bcode in self.constraints.benchmark_codes:
                if bcode == "risk_free":
                    rr_daily = rf_rate / 252.0
                    rf_series = np.cumsum(np.ones(T_test) * train_end_cash * rr_daily) + train_end_cash
                    benchmarks["risk_free"] = rf_series
                else:
                    b_close = self.wf_manager.get_benchmark_price(bcode, w, "test")
                    if b_close is not None and len(b_close) == T_test and not np.isnan(b_close[0]):
                        benchmarks[bcode] = b_close

            data.append((test_ind, test_price, cash_baseline, benchmarks))

        self._window_data_cache = data
        return data

    def _evaluate_batch_parallel(
        self,
        encodings: list["StrategyEncoding"],
    ) -> list[tuple | None]:
        """单线程直循环评估。"""
        wins = getattr(self, "_active_windows", [])
        results = []
        for enc in encodings:
            try:
                results.append(self._evaluate_strategy_wf(enc, wins))
            except Exception:
                results.append(None)
        return results

    # ════════════════════════════════════════════════════════
    # Phase 1: 粗筛
    # ════════════════════════════════════════════════════════

    def _random_strategy(self) -> StrategyEncoding:
        """生成随机策略（买入+卖出）"""
        n_buy = self.ds_cfg.num_buy_rules
        n_sell = self.ds_cfg.num_sell_rules
        n_buy_builders = len(self.ds_cfg.buy_builders)
        n_sell_builders = len(self.ds_cfg.sell_builders)

        buy_builders = [self._rng.randint(0, n_buy_builders - 1) for _ in range(n_buy)]
        buy_thresholds = [self._rng.randint(0, self.ds_cfg.threshold_levels - 1) for _ in range(n_buy)]
        buy_fracs = [self._rng.randint(0, len(self.ds_cfg.frac_levels) - 1) for _ in range(n_buy)]

        sell_builders = [self._rng.randint(0, n_sell_builders - 1) for _ in range(n_sell)]
        sell_thresholds = [self._rng.randint(0, self.ds_cfg.threshold_levels - 1) for _ in range(n_sell)]
        sell_fracs = [self._rng.randint(0, len(self.ds_cfg.sell_frac_levels) - 1) for _ in range(n_sell)]

        # 仓位目标参数
        pos_slope = self._rng.randint(0, self.ds_cfg.position_slope_levels - 1)
        pos_bias = self._rng.randint(0, self.ds_cfg.position_bias_levels - 1)

        return StrategyEncoding(
            buy_builders=buy_builders, buy_thresholds=buy_thresholds, buy_fracs=buy_fracs,
            sell_builders=sell_builders, sell_thresholds=sell_thresholds, sell_fracs=sell_fracs,
            position_slope=pos_slope, position_bias=pos_bias,
        )

    def _evaluate_strategy_wf(
        self,
        encoding: StrategyEncoding,
        windows,  # list[WindowSlice]
    ) -> tuple[list[WindowStats], float]:
        """对单个策略在所有 Walk-Forward 窗口评估

        Returns:
            (window_stats_list, wf_score)
        """
        buy_names, buy_thresh, buy_fracs = encoding.to_buy_params(self.ds_cfg)
        sell_names, sell_thresh, sell_fracs = encoding.to_sell_params(self.ds_cfg)
        all_stats: list[WindowStats] = []

        use_pt = self.ds_cfg.use_position_target
        if use_pt:
            pos_slope, pos_bias = encoding.to_position_params(self.ds_cfg)

        for w in windows:
            train_ind = self.wf_manager.build_matrices(w, "train")
            test_ind = self.wf_manager.build_matrices(w, "test")
            test_price = self.wf_manager.get_price_matrix(w, "test")
            T_test = test_ind.shape[0]

            if T_test == 0 or test_ind.shape[1] == 0:
                continue

            rf_rate = getattr(self.constraints, "risk_free_rate", 0.02)
            rf_daily = rf_rate / 252.0
            train_end_cash = self.evaluator.initial_cash * (1.0 + rf_daily) ** train_ind.shape[0]
            cash_baseline = np.cumsum(
                np.ones(T_test) * train_end_cash * rf_daily,
            ) + train_end_cash

            # 构造多基准序列
            from collections import OrderedDict
            benchmark_series = OrderedDict()
            rf_rate = getattr(self.constraints, "risk_free_rate", 0.02)
            for bcode in self.constraints.benchmark_codes:
                if bcode == "risk_free":
                    # 用无风险利率构造等比序列
                    rr_daily = rf_rate / 252.0
                    rf_series = np.cumsum(np.ones(T_test) * train_end_cash * rr_daily) + train_end_cash
                    benchmark_series["risk_free"] = rf_series
                else:
                    b_close = self.wf_manager.get_benchmark_price(bcode, w, "test")
                    if (b_close is not None and len(b_close) == T_test
                            and not np.isnan(b_close[0])):
                        benchmark_series[bcode] = b_close

            if use_pt:
                stats = self.evaluator.evaluate_position_target(
                    test_ind, test_price, cash_baseline,
                    buy_names, buy_thresh,
                    sell_names, sell_thresh,
                    position_slope=pos_slope, position_bias=pos_bias,
                    benchmark_series=benchmark_series if benchmark_series else None,
                )
            else:
                stats = self.evaluator.evaluate(
                    test_ind, test_price, cash_baseline,
                    buy_names, buy_thresh, buy_fracs,
                    sell_names, sell_thresh, sell_fracs,
                    benchmark_series=benchmark_series if benchmark_series else None,
                )
            all_stats.append(stats)

        wf_score = self._compute_wf_score(all_stats)
        return all_stats, wf_score

    def _compute_wf_score(self, stats_list: list[WindowStats]) -> float:
        """计算 Walk-Forward 加权得分"""
        if not stats_list:
            return -float("inf")

        returns = [s.test_excess_return for s in stats_list]
        weights = self.wf_cfg.window_weights[:len(returns)]

        # 归一化权重
        if sum(weights) > 0:
            weights = [w / sum(weights) for w in weights]
        else:
            weights = [1.0 / len(returns)] * len(returns)

        mean_return = sum(r * w for r, w in zip(returns, weights))
        std_return = float(np.std(returns)) if len(returns) >= 2 else 0.0
        score = mean_return - self.wf_cfg.stability_penalty * std_return
        return score

    def run_phase1(self, windows) -> list["ScoredStrategy"]:
        """Phase 1: 随机采样粗筛（并行批次）。

        策略: 先只检查最大回撤（防止爆仓），让遗传算法有机会工作。
        如果前 2000 个策略全部阵亡，自动放宽为仅检查回撤。
        仓位/一致性/交易密度约束在最终输出前验证。

        Returns:
            得分最高的 Top-K 策略列表
        """
        from math import ceil

        n_samples = self.cfg.phase1_random_samples
        n_keep = self.cfg.phase1_top_keep
        BATCH_SIZE = 500

        logger.info(
            "[Phase1] 随机采样 %d 个策略（批次 %d），保留 Top %d",
            n_samples, BATCH_SIZE, n_keep,
        )

        # 预提取窗口数据（并行 worker 共用）
        self._prepare_window_data(windows)
        self._active_windows = windows

        scored: list[ScoredStrategy] = []
        use_strict = True
        auto_switched = False
        total_evaluated = 0

        n_batches = ceil(n_samples / BATCH_SIZE)

        for batch_idx in range(n_batches):
            batch_sz = min(BATCH_SIZE, n_samples - total_evaluated)
            if batch_sz <= 0:
                break

            # 生成一批随机策略
            batch_encodings = [self._random_strategy() for _ in range(batch_sz)]

            # 并行评估
            batch_results = self._evaluate_batch_parallel(batch_encodings)

            # 约束检查 + 收集
            for j, encoding in enumerate(batch_encodings):
                total_evaluated += 1
                i = total_evaluated
                result = batch_results[j]
                if result is None:
                    if use_strict and i >= 2000 and len(scored) == 0 and not auto_switched:
                        logger.warning("[Phase1] 前 %d 个策略全部未通过严格约束，自动放宽为仅检查最大回撤", i)
                        use_strict = False
                        auto_switched = True
                    continue

                stats, wf_score = result

                if use_strict:
                    passes, violations = self.constraints.check_hard_constraints(stats, wf_score)
                else:
                    passes = all(
                        ws.max_drawdown_pct >= self.constraints.max_drawdown_pct
                        for ws in stats
                    )
                    violations = []

                if not passes:
                    if use_strict and i >= 2000 and len(scored) == 0 and not auto_switched:
                        logger.warning("[Phase1] 前 %d 个策略全部未通过严格约束，自动放宽为仅检查最大回撤", i)
                        use_strict = False
                        auto_switched = True
                    elif i > 2000 and i % 5000 == 0:
                        logger.debug("[Phase1] %d/%d: 未通过约束 (%s)", i, n_samples, "; ".join(violations[:3]) if violations else "仅回撤")
                    continue

                avg_sharpe = np.mean([s.sharpe_ratio for s in stats])
                penalty = self.constraints.compute_soft_penalty(avg_sharpe)
                adjusted_score = wf_score - penalty

                scored.append(ScoredStrategy(
                    encoding=encoding,
                    window_stats=stats,
                    wf_score=adjusted_score,
                    avg_excess_return=np.mean([s.test_excess_return for s in stats]),
                    avg_position=np.mean([s.avg_position_pct for s in stats]),
                    avg_sharpe=avg_sharpe,
                ))

            if (batch_idx + 1) % 4 == 0 or batch_idx == n_batches - 1:
                logger.info(
                    "[Phase1] 批次 %d/%d 完成, 已评估 %d, 有效策略 %d",
                    batch_idx + 1, n_batches, total_evaluated, len(scored),
                )

        # 按得分排序
        scored.sort(key=lambda x: x.wf_score, reverse=True)
        logger.info(
            "[Phase1] 完成: %d 个有效策略 / %d 总采样 (严格模式=%s)",
            len(scored), n_samples, "否" if auto_switched else "是",
        )

        return scored[:n_keep]

    # ════════════════════════════════════════════════════════
    # Phase 2: 遗传优化
    # ════════════════════════════════════════════════════════

    def _crossover(self, parent1: StrategyEncoding, parent2: StrategyEncoding) -> StrategyEncoding:
        """均匀交叉：每条规则（买入+卖出）随机从父1或父2继承"""
        child = parent1.clone()

        # 买入规则交叉
        for i in range(parent1.n_buy_rules):
            if self._rng.random() < 0.5:
                child.buy_builders[i] = parent2.buy_builders[i]
                child.buy_thresholds[i] = parent2.buy_thresholds[i]
                child.buy_fracs[i] = parent2.buy_fracs[i]

        # 卖出规则交叉
        for i in range(parent1.n_sell_rules):
            if self._rng.random() < 0.5:
                child.sell_builders[i] = parent2.sell_builders[i]
                child.sell_thresholds[i] = parent2.sell_thresholds[i]
                child.sell_fracs[i] = parent2.sell_fracs[i]

        # 仓位参数交叉
        if self._rng.random() < 0.5:
            child.position_slope = parent2.position_slope
        if self._rng.random() < 0.5:
            child.position_bias = parent2.position_bias

        return child

    def _mutate(self, encoding: StrategyEncoding) -> StrategyEncoding:
        """变异：随机改变某条规则的某个维度"""
        mutant = encoding.clone()
        n_buy_builders = len(self.ds_cfg.buy_builders)
        n_sell_builders = len(self.ds_cfg.sell_builders)

        # 买入规则变异
        for i in range(mutant.n_buy_rules):
            if self._rng.random() < self.cfg.mutation_builder_rate:
                mutant.buy_builders[i] = self._rng.randint(0, n_buy_builders - 1)

            if self._rng.random() < self.cfg.mutation_rate:
                step = self._rng.randint(-self.cfg.mutation_threshold_step,
                                         self.cfg.mutation_threshold_step)
                mutant.buy_thresholds[i] = max(0, min(
                    self.ds_cfg.threshold_levels - 1,
                    mutant.buy_thresholds[i] + step,
                ))

            if self._rng.random() < self.cfg.mutation_rate:
                step = self._rng.randint(-self.cfg.mutation_frac_step,
                                         self.cfg.mutation_frac_step)
                mutant.buy_fracs[i] = max(0, min(
                    len(self.ds_cfg.frac_levels) - 1,
                    mutant.buy_fracs[i] + step,
                ))

        # 卖出规则变异
        for i in range(mutant.n_sell_rules):
            if self._rng.random() < self.cfg.mutation_builder_rate:
                mutant.sell_builders[i] = self._rng.randint(0, n_sell_builders - 1)

            if self._rng.random() < self.cfg.mutation_rate:
                step = self._rng.randint(-self.cfg.mutation_threshold_step,
                                         self.cfg.mutation_threshold_step)
                mutant.sell_thresholds[i] = max(0, min(
                    self.ds_cfg.threshold_levels - 1,
                    mutant.sell_thresholds[i] + step,
                ))

            if self._rng.random() < self.cfg.mutation_rate:
                step = self._rng.randint(-self.cfg.mutation_frac_step,
                                         self.cfg.mutation_frac_step)
                mutant.sell_fracs[i] = max(0, min(
                    len(self.ds_cfg.sell_frac_levels) - 1,
                    mutant.sell_fracs[i] + step,
                ))

        # 仓位参数变异
        if self._rng.random() < self.cfg.mutation_rate:
            step = self._rng.randint(-2, 2)
            mutant.position_slope = max(0, min(
                self.ds_cfg.position_slope_levels - 1,
                mutant.position_slope + step,
            ))
        if self._rng.random() < self.cfg.mutation_rate:
            step = self._rng.randint(-2, 2)
            mutant.position_bias = max(0, min(
                self.ds_cfg.position_bias_levels - 1,
                mutant.position_bias + step,
            ))

        return mutant

    def run_phase2(
        self,
        population: list["ScoredStrategy"],
        windows,
        strict_constraints: bool = False,
    ) -> list["ScoredStrategy"]:
        """Phase 2: 遗传优化

        Args:
            population: Phase 1 产出的 Top-K 策略
            windows: Walk-Forward 窗口列表
            strict_constraints: 是否使用完整硬约束（默认放宽，只检查回撤）

        Returns:
            优化后的最终种群
        """
        n_gen = self.cfg.num_generations
        pop_size = self.cfg.population_size
        n_offspring = self.cfg.offspring_size

        # 确保种群大小不超过输入
        current_pop = population[:pop_size]

        for gen in range(n_gen):
            logger.info(
                "[Phase2] 第 %d/%d 代: %d 个策略, 最佳得分 %.2f",
                gen + 1, n_gen, len(current_pop),
                current_pop[0].wf_score if current_pop else -999,
            )

            # 生成后代编码（并行预生成）
            child_encodings = []
            for _ in range(n_offspring):
                t1 = self._rng.randint(0, len(current_pop) - 1)
                t2 = self._rng.randint(0, len(current_pop) - 1)
                parent1 = current_pop[min(t1, t2)].encoding
                t3 = self._rng.randint(0, len(current_pop) - 1)
                t4 = self._rng.randint(0, len(current_pop) - 1)
                parent2 = current_pop[min(t3, t4)].encoding
                if self._rng.random() < self.cfg.crossover_rate:
                    child_enc = self._crossover(parent1, parent2)
                else:
                    child_enc = parent1.clone()
                child_enc = self._mutate(child_enc)
                child_encodings.append(child_enc)

            # 并行评估后代
            offspring: list[ScoredStrategy] = []
            BATCH_SZ = 500
            for bi in range(0, len(child_encodings), BATCH_SZ):
                batch = child_encodings[bi:bi + BATCH_SZ]
                results = self._evaluate_batch_parallel(batch)
                for j, child_enc in enumerate(batch):
                    r = results[j]
                    if r is None:
                        continue
                    stats, wf_score = r
                    if strict_constraints:
                        passes, _ = self.constraints.check_hard_constraints(stats, wf_score)
                    else:
                        passes = all(
                            ws.max_drawdown_pct >= self.constraints.max_drawdown_pct
                            for ws in stats
                        )
                    if not passes:
                        continue
                    avg_sharpe = np.mean([s.sharpe_ratio for s in stats])
                    penalty = self.constraints.compute_soft_penalty(avg_sharpe)
                    offspring.append(ScoredStrategy(
                        encoding=child_enc,
                        window_stats=stats,
                        wf_score=wf_score - penalty,
                        avg_excess_return=np.mean([s.test_excess_return for s in stats]),
                        avg_position=np.mean([s.avg_position_pct for s in stats]),
                        avg_sharpe=avg_sharpe,
                    ))

            logger.debug("[Phase2] 后代评估完成: %d 个通过", len(offspring))

            # 合并并保留最优
            combined = current_pop + offspring
            combined.sort(key=lambda x: x.wf_score, reverse=True)
            current_pop = combined[:pop_size]

        logger.info(
            "[Phase2] 完成: 最终种群 %d, 最佳得分 %.2f, 均值超额 %.1f%%",
            len(current_pop), current_pop[0].wf_score if current_pop else -999,
            current_pop[0].avg_excess_return if current_pop else 0,
        )

        return current_pop


# ════════════════════════════════════════════════════════════
# 打分策略
# ════════════════════════════════════════════════════════════


@dataclass(order=True)
class ScoredStrategy:
    """已评估的策略 + 得分"""
    encoding: StrategyEncoding = field(compare=False)
    window_stats: list[WindowStats] = field(compare=False)
    wf_score: float = -float("inf")
    avg_excess_return: float = 0.0
    avg_position: float = 0.0
    avg_sharpe: float = 0.0
