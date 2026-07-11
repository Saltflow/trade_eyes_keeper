"""策略引擎插件接口。

StrategyEngine 定义了 StrategyOptimizerV2 + GeneticSearcher 与具体策略
实现之间的合约。策略是纯函数：evaluate(params, market_data) → (stats, score)。

两个实现：
- GlobalThresholdEngine：现有逻辑（buy_names/buy_thresh/buy_fracs）
- PercentileScoringEngine：分位评分（τ+权重，每只标的独立分位）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .genetic_searcher import StrategyEncoding
    from .fast_evaluator import FastEvaluator, WindowStats
    from .optimizer_constraints import StrategyConstraints, DiscreteSearchConfig
    import numpy as np


class StrategyEngine(ABC):
    """策略评估的纯函数接口。

    每个引擎实例管理一种参数空间 + 参数→评估的翻译逻辑。
    所有方法返回可直接用于搜索和报告的标准化格式。
    """

    # ── 编码操作（搜索器委托给引擎）──

    @abstractmethod
    def param_count(self) -> int:
        """扁平编码的总参数数（用于 to_flat/from_flat）。"""
        ...

    @abstractmethod
    def random_encoding(self, ds_cfg) -> "StrategyEncoding":
        """生成一组随机参数编码。"""
        ...

    @abstractmethod
    def evaluate_encoding(
        self,
        encoding: "StrategyEncoding",
        windows,  # list[WindowSlice]
        ds_cfg: "DiscreteSearchConfig",
        constraints: "StrategyConstraints",
        evaluator: "FastEvaluator",
        wf_manager,
    ) -> tuple[list["WindowStats"], float] | None:
        """评估一组参数编码 → (window_stats, wf_score)。

        Args:
            encoding: StrategyEncoding 参数编码
            windows: WalkForwardManager 窗口切片列表
            ds_cfg: 离散搜索配置
            constraints: 策略约束
            evaluator: FastEvaluator 实例
            wf_manager: WalkForwardManager 实例

        Returns:
            (window_stats_list, wf_score) 或 None（评估失败）
        """
        ...

    @abstractmethod
    def crossover_encoding(
        self, p1: "StrategyEncoding", p2: "StrategyEncoding",
    ) -> "StrategyEncoding":
        """交叉两个编码。"""
        ...

    @abstractmethod
    def mutate_encoding(
        self, encoding: "StrategyEncoding", ds_cfg,
    ) -> "StrategyEncoding":
        """变异一个编码。"""
        ...

    @abstractmethod
    def to_human_readable(self, encoding: "StrategyEncoding", ds_cfg) -> str:
        """将编码转为人类可读的描述（供报告使用）。"""
        ...
