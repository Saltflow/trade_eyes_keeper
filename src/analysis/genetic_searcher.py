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
    """策略的离散编码（买入+卖出）

    买入规则: (builder_idx, threshold_level, frac_level) × num_buy_rules
    卖出规则: (builder_idx, threshold_level, frac_level) × num_sell_rules
    """

    # 买入
    buy_builders: list[int]
    buy_thresholds: list[int]
    buy_fracs: list[int]

    # 卖出
    sell_builders: list[int] = field(default_factory=list)
    sell_thresholds: list[int] = field(default_factory=list)
    sell_fracs: list[int] = field(default_factory=list)

    @property
    def n_buy_rules(self) -> int:
        return len(self.buy_builders)

    @property
    def n_sell_rules(self) -> int:
        return len(self.sell_builders)

    def to_flat(self) -> list[int]:
        """扁平化为一维列表"""
        result = []
        for i in range(self.n_buy_rules):
            result.extend([self.buy_builders[i], self.buy_thresholds[i], self.buy_fracs[i]])
        for i in range(self.n_sell_rules):
            result.extend([self.sell_builders[i], self.sell_thresholds[i], self.sell_fracs[i]])
        return result

    @classmethod
    def from_flat(
        cls, flat: list[int], n_buy: int = 5, n_sell: int = 3,
    ) -> "StrategyEncoding":
        """从一维列表恢复"""
        p = 0
        buy_builders = [flat[p + i * 3] for i in range(n_buy)]
        buy_thresholds = [flat[p + i * 3 + 1] for i in range(n_buy)]
        buy_fracs = [flat[p + i * 3 + 2] for i in range(n_buy)]
        p = n_buy * 3
        sell_builders = [flat[p + i * 3] for i in range(n_sell)]
        sell_thresholds = [flat[p + i * 3 + 1] for i in range(n_sell)]
        sell_fracs = [flat[p + i * 3 + 2] for i in range(n_sell)]
        return cls(
            buy_builders=buy_builders, buy_thresholds=buy_thresholds, buy_fracs=buy_fracs,
            sell_builders=sell_builders, sell_thresholds=sell_thresholds, sell_fracs=sell_fracs,
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

    def clone(self) -> "StrategyEncoding":
        return StrategyEncoding(
            buy_builders=list(self.buy_builders),
            buy_thresholds=list(self.buy_thresholds),
            buy_fracs=list(self.buy_fracs),
            sell_builders=list(self.sell_builders),
            sell_thresholds=list(self.sell_thresholds),
            sell_fracs=list(self.sell_fracs),
        )


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

        return StrategyEncoding(
            buy_builders=buy_builders, buy_thresholds=buy_thresholds, buy_fracs=buy_fracs,
            sell_builders=sell_builders, sell_thresholds=sell_thresholds, sell_fracs=sell_fracs,
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

        for w in windows:
            train_ind = self.wf_manager.build_matrices(w, "train")
            test_ind = self.wf_manager.build_matrices(w, "test")
            test_price = self.wf_manager.get_price_matrix(w, "test")
            T_test = test_ind.shape[0]

            if T_test == 0 or test_ind.shape[1] == 0:
                continue

            rf_daily = 0.02 / 252.0
            train_end_cash = self.evaluator.initial_cash * (1.0 + rf_daily) ** train_ind.shape[0]
            cash_baseline = np.cumsum(
                np.ones(T_test) * train_end_cash * rf_daily,
            ) + train_end_cash

            stats = self.evaluator.evaluate(
                test_ind, test_price, cash_baseline,
                buy_names, buy_thresh, buy_fracs,
                sell_names, sell_thresh, sell_fracs,
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
        """Phase 1: 随机采样粗筛

        策略: 先只检查最大回撤（防止爆仓），让遗传算法有机会工作。
        如果前 2000 个策略全部阵亡，自动放宽为仅检查回撤。
        仓位/一致性/交易密度约束在最终输出前验证。

        Returns:
            得分最高的 Top-K 策略列表
        """
        n_samples = self.cfg.phase1_random_samples
        n_keep = self.cfg.phase1_top_keep

        logger.info(
            "[Phase1] 随机采样 %d 个策略，保留 Top %d",
            n_samples, n_keep,
        )

        scored: list[ScoredStrategy] = []
        # 自适应约束模式: strict → relaxed
        use_strict = True
        auto_switched = False

        for i in range(n_samples):
            encoding = self._random_strategy()
            stats, wf_score = self._evaluate_strategy_wf(encoding, windows)

            # 自适应约束检查
            if use_strict:
                passes, violations = self.constraints.check_hard_constraints(stats, wf_score)
            else:
                # 放宽模式: 只检查最大回撤
                passes = all(
                    ws.max_drawdown_pct >= self.constraints.max_drawdown_pct
                    for ws in stats
                )
                violations = []

            if not passes:
                # 前 2000 个全部阵亡 → 自动放宽
                if use_strict and i >= 2000 and len(scored) == 0 and not auto_switched:
                    logger.warning(
                        "[Phase1] 前 %d 个策略全部未通过严格约束，"
                        "自动放宽为仅检查最大回撤",
                        i,
                    )
                    use_strict = False
                    auto_switched = True
                elif i % 5000 == 0:
                    logger.debug(
                        "[Phase1] %d/%d: 未通过约束 (%s)",
                        i + 1, n_samples,
                        "; ".join(violations[:3]) if violations else "仅回撤",
                    )
                continue

            # 软性约束惩罚
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

            if (i + 1) % 2000 == 0:
                logger.info(
                    "[Phase1] %d/%d 完成, 有效策略 %d, 最佳得分 %.2f",
                    i + 1, n_samples, len(scored), scored[0].wf_score if scored else 0,
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

            # 生成后代
            offspring: list[ScoredStrategy] = []

            for _ in range(n_offspring):
                # 锦标赛选择父代
                t1 = self._rng.randint(0, len(current_pop) - 1)
                t2 = self._rng.randint(0, len(current_pop) - 1)
                parent1 = current_pop[min(t1, t2)].encoding

                t3 = self._rng.randint(0, len(current_pop) - 1)
                t4 = self._rng.randint(0, len(current_pop) - 1)
                parent2 = current_pop[min(t3, t4)].encoding

                # 交叉
                if self._rng.random() < self.cfg.crossover_rate:
                    child_enc = self._crossover(parent1, parent2)
                else:
                    child_enc = parent1.clone()

                # 变异
                child_enc = self._mutate(child_enc)

                # 评估后代
                stats, wf_score = self._evaluate_strategy_wf(child_enc, windows)

                # 约束检查（遗传阶段默认放宽，只检查回撤）
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

                if len(offspring) % 1000 == 0:
                    logger.debug("[Phase2] 已生成 %d/%d 后代", len(offspring), n_offspring)

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
