"""
优化器约束加载器

读取 config/optimizer_constraints.yaml，提供结构化约束访问。
所有约束可随时通过配置文件调整，无需修改代码。

用法:
    from src.analysis.optimizer_constraints import load_constraints
    constraints = load_constraints()
    result = constraints.check(strategy_stats)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# 默认配置路径
DEFAULT_PATH = Path(__file__).parent.parent.parent / "config" / "optimizer_constraints.yaml"


class WalkForwardConfig:
    """Walk-Forward 窗口配置"""

    def __init__(self, data: dict):
        self.train_months: int = data.get("train_months", 12)
        self.test_months: int = data.get("test_months", 9)
        self.step_months: int = data.get("step_months", 3)
        self.num_windows: int = data.get("num_windows", 6)
        self.window_weights: list[float] = data.get(
            "window_weights", [1.0] * self.num_windows,
        )
        self.stability_penalty: float = data.get("stability_penalty", 0.5)

    @property
    def total_months_needed(self) -> int:
        """推算最少需要的总月数（训练期 + 测试期 + 最后一个窗口的偏移）"""
        return self.train_months + self.test_months + (self.num_windows - 1) * self.step_months


class GeneticSearchConfig:
    """遗传搜索配置"""

    def __init__(self, data: dict):
        self.phase1_random_samples: int = data.get("phase1_random_samples", 10000)
        self.phase1_top_keep: int = data.get("phase1_top_keep", 1000)
        self.num_generations: int = data.get("num_generations", 3)
        self.population_size: int = data.get("population_size", 1000)
        self.offspring_size: int = data.get("offspring_size", 5000)
        self.crossover_rate: float = data.get("crossover_rate", 0.70)
        self.mutation_rate: float = data.get("mutation_rate", 0.30)
        self.mutation_builder_rate: float = data.get("mutation_builder_rate", 0.20)
        self.mutation_threshold_step: int = data.get("mutation_threshold_step", 2)
        self.mutation_frac_step: int = data.get("mutation_frac_step", 1)


class DiscreteSearchConfig:
    """离散搜索空间配置"""

    def __init__(self, data: dict):
        self.buy_builders: list[str] = data.get("buy_builders", [
            "deviation_cross", "rsi_signal", "bollinger_signal",
            "volume_spike", "deviation_absolute", "trend_follow", "none",
        ])
        self.threshold_levels: int = data.get("threshold_levels", 10)
        self.frac_levels: list[float] = data.get(
            "frac_levels", [0.10, 0.15, 0.20, 0.25, 0.30, 0.40],
        )
        self.num_buy_rules: int = data.get("num_buy_rules", 5)

        # 卖出规则
        self.sell_builders: list[str] = data.get("sell_builders", [
            "deviation_cross", "rsi_signal", "bollinger_signal",
            "deviation_absolute", "trend_follow", "none",
        ])
        self.sell_frac_levels: list[float] = data.get(
            "sell_frac_levels", [0.10, 0.20, 0.30, 0.40, 0.50],
        )
        self.num_sell_rules: int = data.get("num_sell_rules", 3)

        # 仓位目标模型
        pm = data.get("position_model", {})
        self.mode: str = data.get("mode", "frac")  # "frac" or "position_target"
        self.position_slope_levels: int = pm.get("slope_levels", 20)
        self.position_bias_levels: int = pm.get("bias_levels", 20)
        self.max_daily_adjust: float = pm.get("max_daily_adjust", 0.10)

    @property
    def use_position_target(self) -> bool:
        return self.mode == "position_target"

    @property
    def search_space_size(self) -> int:
        """估算搜索空间大小（总组合数）"""
        buy_singles = len(self.buy_builders) * self.threshold_levels * len(self.frac_levels)
        sell_singles = len(self.sell_builders) * self.threshold_levels * len(self.sell_frac_levels)
        return (buy_singles ** self.num_buy_rules) * (sell_singles ** self.num_sell_rules)


class StrategyConstraints:
    """策略约束检查器

    对 Walk-Forward 回测统计结果执行硬性/软性约束检查。
    """

    def __init__(self, raw_config: dict | None = None):
        if raw_config is None:
            raw_config = {}

        hc = raw_config.get("hard_constraints", {})
        self.min_avg_position_pct: float = hc.get("min_avg_position_pct", 20.0)
        self.max_drawdown_pct: float = hc.get("max_drawdown_pct", -25.0)
        self.max_return_std_pct: float = hc.get("max_return_std_pct", 15.0)
        self.min_trades_per_month: int = hc.get("min_trades_per_month", 1)
        self.max_trades_per_month: int = hc.get("max_trades_per_month", 6)

        sc = raw_config.get("soft_constraints", {})
        self.min_sharpe: float = sc.get("min_sharpe", 0.5)
        self.sharpe_penalty_weight: float = sc.get("sharpe_penalty_weight", 0.3)

        self.walk_forward = WalkForwardConfig(raw_config.get("walk_forward", {}))
        self.genetic_search = GeneticSearchConfig(raw_config.get("genetic_search", {}))
        self.discrete_search = DiscreteSearchConfig(raw_config.get("discrete_search", {}))

        # 业绩基准
        bc = raw_config.get("benchmarks", {})
        self.benchmark_codes: list[str] = []
        self.risk_free_rate: float = 0.02
        # 由调用方在创建后按 group 设置（a_share / non_a_share）
        self._raw_benchmarks = bc

    def set_group(self, group: str):
        """设置所属组别，从 benchmarks 配置中提取对应基准代码和利率。

        Args:
            group: "a_share" 或 "non_a_share"
        """
        self.benchmark_codes = list(self._raw_benchmarks.get(group, []))
        rates = self._raw_benchmarks.get("risk_free_rates", {})
        self.risk_free_rate = rates.get(group, 0.02)

    def check_hard_constraints(
        self,
        window_stats: list[WindowStats],
        walk_forward_score: float,
    ) -> tuple[bool, list[str]]:
        """检查硬性约束

        Args:
            window_stats: 各窗口的统计数据列表
            walk_forward_score: Walk-Forward 得分（保留以备将来使用）

        Returns:
            (passes, violations): 是否通过 + 违规项描述列表
        """
        violations: list[str] = []

        # 1. 平均仓位检查
        avg_position = np.mean([ws.avg_position_pct for ws in window_stats])
        if avg_position < self.min_avg_position_pct:
            violations.append(
                f"平均仓位 {avg_position:.1f}% < {self.min_avg_position_pct:.0f}%",
            )

        # 2. 最大回撤检查
        for i, ws in enumerate(window_stats):
            if ws.max_drawdown_pct < self.max_drawdown_pct:
                violations.append(
                    f"窗口{i+1}最大回撤 {ws.max_drawdown_pct:.1f}% < {self.max_drawdown_pct:.1f}%",
                )

        # 3. 训练-测试一致性检查
        test_returns = [ws.test_excess_return for ws in window_stats]
        if len(test_returns) >= 2:
            ret_std = float(np.std(test_returns))
            if ret_std > self.max_return_std_pct:
                violations.append(
                    f"测试期收益标准差 {ret_std:.1f}% > {self.max_return_std_pct:.1f}%",
                )

        # 4. 交易密度检查
        for i, ws in enumerate(window_stats):
            if ws.trades_per_month < self.min_trades_per_month:
                violations.append(
                    f"窗口{i+1} 月交易次数 {ws.trades_per_month:.1f} < {self.min_trades_per_month}",
                )
            if ws.trades_per_month > self.max_trades_per_month:
                violations.append(
                    f"窗口{i+1} 月交易次数 {ws.trades_per_month:.1f} > {self.max_trades_per_month}",
                )

        return len(violations) == 0, violations

    def compute_soft_penalty(self, sharpe_ratio: float) -> float:
        """计算软性约束惩罚分

        Returns:
            penalty >= 0.0, 越低越好
        """
        penalty = 0.0
        if sharpe_ratio < self.min_sharpe:
            penalty += (self.min_sharpe - sharpe_ratio) * self.sharpe_penalty_weight
        return penalty


class WindowStats:
    """单个 Walk-Forward 窗口的回测统计数据

    由向量化快速评估器或精确回测器填充。
    """

    def __init__(
        self,
        test_excess_return: float = 0.0,
        max_drawdown_pct: float = 0.0,
        avg_position_pct: float = 0.0,
        sharpe_ratio: float = 0.0,
        total_trades: int = 0,
        test_months: int = 9,
        benchmark_returns: dict[str, float] | None = None,
        strategy_return: float = 0.0,
        final_position_pct: float = 0.0,
        final_shares: "np.ndarray | None" = None,
        final_cash: float = 0.0,
        cost_basis: "np.ndarray | None" = None,
    ):
        self.test_excess_return = test_excess_return
        self.max_drawdown_pct = max_drawdown_pct
        self.avg_position_pct = avg_position_pct
        self.sharpe_ratio = sharpe_ratio
        self.total_trades = total_trades
        self.test_months = test_months
        self.benchmark_returns: dict[str, float] = benchmark_returns or {}
        self.strategy_return = strategy_return
        self.final_position_pct = final_position_pct
        self.final_shares = final_shares
        self.final_cash = final_cash
        self.cost_basis = cost_basis

    @property
    def trades_per_month(self) -> float:
        """月均交易次数"""
        if self.test_months <= 0:
            return 0.0
        return self.total_trades / self.test_months

    def excess_vs(self, bench_label: str) -> float:
        """对特定基准的超额收益"""
        if bench_label in self.benchmark_returns:
            return round(self.strategy_return - self.benchmark_returns[bench_label], 2)
        return self.test_excess_return


def load_constraints(path: Path | str | None = None) -> StrategyConstraints:
    """加载优化器约束配置

    Args:
        path: YAML 路径，默认 config/optimizer_constraints.yaml

    Returns:
        StrategyConstraints 实例
    """
    config_path = Path(path) if path else DEFAULT_PATH
    if not config_path.exists():
        logger.warning(
            "约束配置文件 %s 不存在，使用默认值", config_path,
        )
        return StrategyConstraints()

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return StrategyConstraints(raw)


# 惰性导入 numpy（避免模块级导入影响启动速度）
import numpy as np
