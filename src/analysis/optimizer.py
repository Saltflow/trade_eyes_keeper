"""共用遗传搜索 + 编排器 — 不关心具体策略，只操作 SearchStrategy 接口。"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import yaml
from datetime import datetime

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
    """在多个 WF 窗口上评估一组参数（统一路径，零策略分支）。"""
    all_stats = []
    params = encoding.to_params(strategy)

    for w in windows:
        test_ind = wf_manager.indicator_matrix[w.test_start:w.test_end]
        test_price = wf_manager.price_matrix[w.test_start:w.test_end]

        # 每个策略自己生成信号（optimizer 不关心策略内部逻辑）
        buy_signals, sell_signals = strategy.make_signals(params, test_ind)

        cash_bs = evaluator.initial_cash * (
            1 + constraints.risk_free_rate / 252
        ) ** np.arange(test_ind.shape[0], dtype=np.float64)
        stats = evaluator.evaluate(
            indicator_matrix=test_ind,
            price_matrix=test_price,
            cash_baseline=cash_bs,
            buy_score_signals=buy_signals,
            sell_score_signals=sell_signals,
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
    _constraints=None,
) -> tuple[list[ScoredEncoding], StrategyConstraints]:
    """运行完整搜参流程：数据 → 窗口 → GA → 排序结果。"""
    from .backtester import WalkForwardManager, FastEvaluator
    from .config import get_constraints

    if _constraints is not None:
        constraints = _constraints
    else:
        constraints = get_constraints()
        constraints.set_group(group)
    exec_cfg = constraints.execution

    wf_manager = WalkForwardManager(stocks_data, constraints, stock_codes)
    evaluator = FastEvaluator(exec_cfg, group)

    opt = GeneticOptimizer(strategy, constraints, wf_manager, evaluator)
    results = opt.run()
    if results:
        _save_optimizer_result(results, strategy, group)
    return results, constraints


def _save_optimizer_result(
    results,
    strategy,
    group: str,
) -> None:
    """保存最优参数到 data/optimizer/{group}_best_params.yaml"""
    top = results[0]
    params = top.encoding.to_params(strategy)
    data = {
        "timestamp": datetime.now().isoformat(),
        "group": group,
        "engine": strategy.name,
        "params": {**params.values, "_engine": strategy.name},
        "wf_score": top.wf_score,
    }
    from pathlib import Path

    out_dir = Path("data/optimizer")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{group}_best_params.yaml"
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    logger.info(f"最优参数已保存: {path}")
