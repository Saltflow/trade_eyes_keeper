"""共用遗传搜索 + 编排器 — 不关心具体策略，只操作 SearchStrategy 接口。"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import yaml

from .config import (
    StrategyConstraints, DiscreteSearchConfig, WindowStats, get_constraints,
)
from .search_interface import SearchStrategy, Params

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 策略编码
# ═══════════════════════════════════════════════════════════════

@dataclass
class StrategyEncoding:
    """策略的扁平编码 — 纯整数数组，适配 GA。"""
    genome: list[int]  # 扁平编码
    engine_name: str = ""
    params: Params | None = None  # 解码后的 Params（懒求值）

    def to_params(self, strategy: SearchStrategy) -> Params:
        if self.params is not None:
            return self.params
        vals = {}
        for i, d in enumerate(strategy.param_space.dims):
            vals[d.name] = self.genome[i] if i < len(self.genome) else 0
        self.params = Params(values=vals, _engine=strategy.name)
        return self.params


# ═══════════════════════════════════════════════════════════════
# GA 核心
# ═══════════════════════════════════════════════════════════════

def _random_encoding(strategy: SearchStrategy) -> StrategyEncoding:
    genome = []
    r = random
    for d in strategy.param_space.dims:
        genome.append(r.randint(0, max(d.levels - 1, 0)))
    return StrategyEncoding(genome=genome, engine_name=strategy.name)


def _crossover(p1: StrategyEncoding, p2: StrategyEncoding) -> StrategyEncoding:
    child = []
    for a, b in zip(p1.genome, p2.genome):
        child.append(a if random.random() < 0.5 else b)
    return StrategyEncoding(genome=child, engine_name=p1.engine_name)


def _mutate(enc: StrategyEncoding, strategy: SearchStrategy, rate: float = 0.15) -> StrategyEncoding:
    new_genome = list(enc.genome)
    for i, d in enumerate(strategy.param_space.dims):
        if i < len(new_genome) and random.random() < rate:
            new_genome[i] = random.randint(0, max(d.levels - 1, 0))
    return StrategyEncoding(genome=new_genome, engine_name=strategy.name)


# ═══════════════════════════════════════════════════════════════
# WF 窗口评估
# ═══════════════════════════════════════════════════════════════

def _evaluate_encoding_wf(
    encoding: StrategyEncoding,
    strategy: SearchStrategy,
    windows: list,
    ds_cfg: DiscreteSearchConfig,
    constraints: StrategyConstraints,
    evaluator,
    wf_manager,
):
    """在多个 WF 窗口上评估一组参数。"""
    all_stats = []
    params = encoding.to_params(strategy)

    for w in windows:
        test_ind = wf_manager.indicator_matrix[w.test_start:w.test_end]
        test_price = wf_manager.price_matrix[w.test_start:w.test_end]

        buy_score_signals = None
        sell_score_signals = None

        # 生成信号
        if strategy.name in ("percentile",):
            scores = strategy.evaluate(params, test_ind)
            buy_th = params.values.get("buy_score_thresh", 5)
            sell_th = params.values.get("sell_score_thresh", 5)
            buy_score_signals = scores[:, :, 0] > (buy_th / 10.0 + 0.1)
            sell_score_signals = scores[:, :, 1] > (sell_th / 10.0 + 0.1)
        elif strategy.name == "builder":
            from .strategies.builder.engine import (
                CONDITION_BUILDERS_FAST, BUILDER_COUNT, FRAC_LEVELS_BUILDER,
                THRESHOLD_LEVELS_BUILDER,
            )
            buy_names = list(CONDITION_BUILDERS_FAST.keys())[:BUILDER_COUNT]
            sell_names = list(CONDITION_BUILDERS_FAST.keys())[
                BUILDER_COUNT:BUILDER_COUNT + 6
            ]
            buy_builders = []
            buy_thresholds = []
            buy_fracs = []
            for i in range(5):
                n = params.values.get(f"buy_{i+1}_name", 0) % len(buy_names)
                buy_builders.append(buy_names[n])
                buy_thresholds.append(
                    params.values.get(f"buy_{i+1}_threshold", 5)
                    / (THRESHOLD_LEVELS_BUILDER - 1)
                )
                buy_fracs.append(
                    FRAC_LEVELS_BUILDER[
                        params.values.get(f"buy_{i+1}_frac", 0)
                        % len(FRAC_LEVELS_BUILDER)
                    ]
                )
            sell_builders = []
            sell_thresholds = []
            sell_fracs = []
            for i in range(3):
                n = params.values.get(f"sell_{i+1}_name", 0) % len(sell_names)
                sell_builders.append(sell_names[n])
                sell_thresholds.append(
                    params.values.get(f"sell_{i+1}_threshold", 5)
                    / (THRESHOLD_LEVELS_BUILDER - 1)
                )
                sell_fracs.append(
                    FRAC_LEVELS_BUILDER[
                        params.values.get(f"sell_{i+1}_frac", 0)
                        % len(FRAC_LEVELS_BUILDER)
                    ]
                )

            cash_bs = np.ones(test_ind.shape[0], dtype=np.float64) * (
                evaluator.initial_cash * (1 + constraints.risk_free_rate / 252)
            ).cumprod()

            stats = evaluator.evaluate(
                indicator_matrix=test_ind,
                price_matrix=test_price,
                cash_baseline=cash_bs,
                buy_builders=buy_builders,
                buy_thresholds=buy_thresholds,
                buy_fracs=buy_fracs,
                sell_builders=sell_builders,
                sell_thresholds=sell_thresholds,
                sell_fracs=sell_fracs,
            )
            all_stats.append(stats)
            continue

        elif strategy.name == "simplified":
            from .strategies.simplified.engine import (
                BUY_BUILDERS_SIMP, SELL_BUILDERS_SIMP,
                BUY_LIMIT_LEVELS, SELL_LIMIT_LEVELS, THRESHOLD_LEVELS_SIMP,
            )
            buy_builders = []
            buy_thresholds = []
            buy_limits = []
            for i in range(5):
                n = params.values.get(f"buy_{i+1}_name", 0) % len(BUY_BUILDERS_SIMP)
                buy_builders.append(BUY_BUILDERS_SIMP[n])
                buy_thresholds.append(
                    params.values.get(f"buy_{i+1}_threshold", 5)
                    / (THRESHOLD_LEVELS_SIMP - 1)
                )
                buy_limits.append(
                    BUY_LIMIT_LEVELS[
                        params.values.get(f"buy_{i+1}_limit", 1)
                        % len(BUY_LIMIT_LEVELS)
                    ]
                )
            sell_builders = []
            sell_thresholds = []
            sell_limits = []
            for i in range(3):
                n = params.values.get(f"sell_{i+1}_name", 0) % len(SELL_BUILDERS_SIMP)
                sell_builders.append(SELL_BUILDERS_SIMP[n])
                sell_thresholds.append(
                    params.values.get(f"sell_{i+1}_threshold", 5)
                    / (THRESHOLD_LEVELS_SIMP - 1)
                )
                sell_limits.append(
                    SELL_LIMIT_LEVELS[
                        params.values.get(f"sell_{i+1}_limit", 1)
                        % len(SELL_LIMIT_LEVELS)
                    ]
                )

            cash_bs = np.ones(test_ind.shape[0], dtype=np.float64) * (
                evaluator.initial_cash * (1 + constraints.risk_free_rate / 252)
            ).cumprod()

            stats = evaluator.evaluate(
                indicator_matrix=test_ind,
                price_matrix=test_price,
                cash_baseline=cash_bs,
                buy_builders=buy_builders,
                buy_thresholds=buy_thresholds,
                buy_limits=buy_limits,
                sell_builders=sell_builders,
                sell_thresholds=sell_thresholds,
                sell_limits=sell_limits,
            )
            all_stats.append(stats)
            continue

        # percentile / score-signal 路径
        cash_bs = np.ones(test_ind.shape[0], dtype=np.float64) * (
            evaluator.initial_cash * (1 + constraints.risk_free_rate / 252)
        ).cumprod()
        stats = evaluator.evaluate(
            indicator_matrix=test_ind,
            price_matrix=test_price,
            cash_baseline=cash_bs,
            buy_score_signals=buy_score_signals,
            sell_score_signals=sell_score_signals,
        )
        all_stats.append(stats)

    if not all_stats:
        return None

    # WF score
    wf_train = all_stats[: constraints.walk_forward.num_windows]
    train_returns = [s.excess_return for s in wf_train]
    if not train_returns:
        return None
    wf_score = float(np.mean(train_returns))

    # stability penalty
    if len(train_returns) >= 2 and constraints.walk_forward.stability_penalty > 0:
        wf_score -= float(np.std(train_returns)) * constraints.walk_forward.stability_penalty

    return all_stats, wf_score


# ═══════════════════════════════════════════════════════════════
# 遗传搜索
# ═══════════════════════════════════════════════════════════════

@dataclass
class ScoredEncoding:
    encoding: StrategyEncoding
    wf_stats: list[WindowStats]
    wf_score: float


class GeneticOptimizer:
    """通用遗传搜索 — 三阶段：随机采样 → 交叉变异 → 精确验证。"""

    def __init__(
        self,
        strategy: SearchStrategy,
        constraints: StrategyConstraints,
        wf_manager,
        evaluator,
    ):
        self.strategy = strategy
        self.constraints = constraints
        self.ds_cfg = constraints.discrete_search
        self.ga_cfg = constraints.genetic_search
        self.wf_manager = wf_manager
        self.evaluator = evaluator

    def run(self) -> list[ScoredEncoding]:
        windows = self.wf_manager.iter_windows()
        if not windows:
            return []

        # Phase 1: 随机采样
        logger.info("[Phase 1] Random sampling %d strategies", self.ga_cfg.phase1_random_samples)
        scored = []
        encodings = [
            _random_encoding(self.strategy)
            for _ in range(self.ga_cfg.phase1_random_samples)
        ]
        # 简单串行（可并行化）
        for enc in encodings:
            result = _evaluate_encoding_wf(
                enc, self.strategy, windows, self.ds_cfg,
                self.constraints, self.evaluator, self.wf_manager,
            )
            if result:
                stats, score = result
                passes, _ = self.constraints.check_hard_constraints(stats, score)
                if passes:
                    scored.append(ScoredEncoding(enc, stats, score))

        scored.sort(key=lambda x: x.wf_score, reverse=True)
        scored = scored[: self.ga_cfg.phase1_top_keep]
        logger.info("[Phase 1] Top after filter: %d", len(scored))

        # Phase 2: 遗传迭代
        for gen in range(self.ga_cfg.num_generations):
            pop = scored[: self.ga_cfg.population_size]
            if len(pop) < 2:
                break

            offspring: list[ScoredEncoding] = []
            for _ in range(self.ga_cfg.offspring_size):
                if random.random() < self.ga_cfg.crossover_rate and len(pop) >= 2:
                    p1, p2 = random.sample(pop, 2)
                    child = _crossover(p1.encoding, p2.encoding)
                else:
                    parent = random.choice(pop)
                    child = StrategyEncoding(
                        genome=list(parent.encoding.genome),
                        engine_name=parent.encoding.engine_name,
                    )

                if random.random() < self.ga_cfg.mutation_rate:
                    child = _mutate(child, self.strategy)

                result = _evaluate_encoding_wf(
                    child, self.strategy, windows, self.ds_cfg,
                    self.constraints, self.evaluator, self.wf_manager,
                )
                if result:
                    stats, score = result
                    passes, _ = self.constraints.check_hard_constraints(stats, score)
                    if passes:
                        offspring.append(ScoredEncoding(child, stats, score))

            combined = scored + offspring
            combined.sort(key=lambda x: x.wf_score, reverse=True)
            scored = combined[: self.ga_cfg.population_size]
            logger.info(
                "[Phase 2] Gen %d: best wf_score=%.2f, survivors=%d",
                gen + 1,
                scored[0].wf_score if scored else 0.0,
                len(scored),
            )

        return scored


# ═══════════════════════════════════════════════════════════════
# 编排器
# ═══════════════════════════════════════════════════════════════

def run_optimizer(
    strategy: SearchStrategy,
    stocks_data: dict,
    stock_codes: list[str],
    group: str = "a_share",
) -> tuple[list[ScoredEncoding], StrategyConstraints]:
    """运行完整搜参流程：数据 → 窗口 → GA → 排序结果。"""
    from .backtester import WalkForwardManager, FastEvaluator
    from .config import get_constraints

    constraints = get_constraints()
    constraints.set_group(group)
    exec_cfg = constraints.execution

    wf_manager = WalkForwardManager(stocks_data, constraints, stock_codes)
    evaluator = FastEvaluator(
        initial_cash=exec_cfg.initial_capital,
        monthly_buy_limit=exec_cfg.monthly_buy_limit,
        lot_size=exec_cfg.lot_sizes.get(group, 100),
        commission_rate=exec_cfg.commission_rate,
        min_holding_days=exec_cfg.min_holding_days,
    )

    opt = GeneticOptimizer(strategy, constraints, wf_manager, evaluator)
    results = opt.run()
    return results, constraints
