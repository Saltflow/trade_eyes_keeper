"""统一配置管理 — 一次 YAML 解析，全系统复用。

读取 config/optimizer_constraints.yaml，提供：
  - ExecutionConfig：执行参数（资金、费率、持仓日）
  - WalkForwardConfig / GeneticSearchConfig / DiscreteSearchConfig：搜索配置
  - StrategyConstraints：硬性/软性约束检查
  - WindowStats / BacktestConfig：回测结果载体 + 时间线配置
  - get_constraints()：模块级单例

用法:
    from src.analysis.config import get_constraints
    cfg = get_constraints()
    exec_cfg = cfg.execution
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional as _Optional

import numpy as np
import pandas as _pd
import yaml
from pydantic import BaseModel as _BaseModel, Field as _Field

logger = logging.getLogger(__name__)

DEFAULT_PATH = (
    Path(__file__).parent.parent.parent / "config" / "optimizer_constraints.yaml"
)

# ═══════════════════════════════════════════════════════════════
# 执行配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExecutionConfig:
    """搜参/日回报测通用执行参数。"""
    monthly_buy_limit: float = 15000.0
    initial_capital: float = 100000.0
    commission_rate: float = 0.005
    min_holding_days: int = 30
    lot_sizes: dict[str, int] = field(
        default_factory=lambda: {"a_share": 100, "hk": 100, "us": 1}
    )
    fx_rates: dict[str, float] = field(
        default_factory=lambda: {"a_share": 1.0, "hk": 0.9, "us": 7.0}
    )

# ═══════════════════════════════════════════════════════════════
# Walk-Forward 窗口配置
# ═══════════════════════════════════════════════════════════════

class WalkForwardConfig:
    """Walk-Forward 窗口配置"""
    def __init__(self, data: dict):
        self.train_months: int = data.get("train_months", 12)
        self.test_months: int = data.get("test_months", 9)
        self.step_months: int = data.get("step_months", 3)
        self.num_windows: int = data.get("num_windows", 6)
        self.window_weights: list[float] = data.get(
            "window_weights", [1.0] * self.num_windows
        )
        self.stability_penalty: float = data.get("stability_penalty", 0.5)
        self.validation_windows: int = data.get("validation_windows", 0)

    @property
    def total_months_needed(self) -> int:
        return (
            self.train_months + self.test_months
            + (self.num_windows - 1) * self.step_months
        )

    @property
    def test_months_calendar_days(self) -> int:
        return max(self.test_months * 30, 274)

# ═══════════════════════════════════════════════════════════════
# 遗传搜索配置
# ═══════════════════════════════════════════════════════════════

class GeneticSearchConfig:
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

# ═══════════════════════════════════════════════════════════════
# 离散搜索空间配置
# ═══════════════════════════════════════════════════════════════

class DiscreteSearchConfig:
    def __init__(self, data: dict):
        self.buy_builders: list[str] = data.get(
            "buy_builders",
            ["deviation_cross", "rsi_signal", "bollinger_signal",
             "volume_spike", "deviation_absolute", "trend_follow", "none"],
        )
        self.threshold_levels: int = data.get("threshold_levels", 10)
        self.frac_levels: list[float] = data.get(
            "frac_levels", [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]
        )
        self.num_buy_rules: int = data.get("num_buy_rules", 5)
        self.sell_builders: list[str] = data.get(
            "sell_builders",
            ["deviation_cross", "rsi_signal", "bollinger_signal",
             "deviation_absolute", "trend_follow", "none"],
        )
        self.sell_frac_levels: list[float] = data.get(
            "sell_frac_levels", [0.10, 0.20, 0.30, 0.40, 0.50]
        )
        self.num_sell_rules: int = data.get("num_sell_rules", 3)
        pm = data.get("position_model", {})
        self.mode: str = data.get("mode", "frac")
        self.position_slope_levels: int = pm.get("slope_levels", 20)
        self.position_bias_levels: int = pm.get("bias_levels", 20)
        self.max_daily_adjust: float = pm.get("max_daily_adjust", 0.10)
        ss = data.get("simplified_search", {})
        self.buy_limit_levels: list[float] = ss.get(
            "buy_limit_levels", [5000.0, 10000.0, 20000.0, 30000.0, 50000.0]
        )
        self.sell_limit_levels: list[float] = ss.get(
            "sell_limit_levels", [5000.0, 10000.0, 20000.0, 30000.0, 50000.0]
        )

    @property
    def use_position_target(self) -> bool:
        return self.mode == "position_target"

    @property
    def use_simplified(self) -> bool:
        return self.mode == "simplified"

    @property
    def search_space_size(self) -> int:
        if self.use_simplified:
            buy_singles = (
                len(self.buy_builders) * self.threshold_levels
                * len(self.buy_limit_levels)
            )
            sell_singles = (
                len(self.sell_builders) * self.threshold_levels
                * len(self.sell_limit_levels)
            )
        else:
            buy_singles = (
                len(self.buy_builders) * self.threshold_levels * len(self.frac_levels)
            )
            sell_singles = (
                len(self.sell_builders) * self.threshold_levels
                * len(self.sell_frac_levels)
            )
        return (buy_singles ** self.num_buy_rules) * (
            sell_singles ** self.num_sell_rules
        )

# ═══════════════════════════════════════════════════════════════
# 策略约束检查器
# ═══════════════════════════════════════════════════════════════

class StrategyConstraints:
    def __init__(self, raw_config: dict | None = None):
        if raw_config is None:
            raw_config = {}
        self._raw_config = raw_config
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
        self.discrete_search = DiscreteSearchConfig(
            raw_config.get("discrete_search", {})
        )
        bc = raw_config.get("benchmarks", {})
        self.benchmark_codes: list[str] = []
        self.risk_free_rate: float = 0.02
        self._raw_benchmarks = bc

    def set_group(self, group: str):
        self.benchmark_codes = list(self._raw_benchmarks.get(group, []))
        rates = self._raw_benchmarks.get("risk_free_rates", {})
        self.risk_free_rate = rates.get(group, 0.02)

    def benchmark_codes_for(self, group: str) -> list[str]:
        return list(self._raw_benchmarks.get(group, []))

    @property
    def execution(self) -> ExecutionConfig:
        ep = self._raw_config.get("execution_params", {}) or {}
        return ExecutionConfig(
            monthly_buy_limit=float(ep.get("monthly_buy_limit", 15000.0)),
            initial_capital=float(ep.get("initial_capital", 100000.0)),
            commission_rate=float(ep.get("commission_rate", 0.005)),
            min_holding_days=int(ep.get("min_holding_days", 30)),
            lot_sizes=dict(ep.get("lot_sizes", {}) or {}),
            fx_rates=dict(ep.get("fx_rates", {}) or {}),
        )

    def check_hard_constraints(
        self, window_stats, walk_forward_score,
    ) -> tuple[bool, list[str]]:
        violations: list[str] = []
        avg_position = np.mean([ws.avg_position_pct for ws in window_stats])
        if avg_position < self.min_avg_position_pct:
            violations.append(
                f"avg position {avg_position:.1f}% < {self.min_avg_position_pct:.0f}%"
            )
        for i, ws in enumerate(window_stats):
            if ws.max_drawdown_pct < self.max_drawdown_pct:
                violations.append(
                    f"W{i+1} max dd {ws.max_drawdown_pct:.1f}% < {self.max_drawdown_pct:.1f}%"
                )
        test_returns = [ws.test_excess_return for ws in window_stats]
        if len(test_returns) >= 2:
            ret_std = float(np.std(test_returns))
            if ret_std > self.max_return_std_pct:
                violations.append(
                    f"test return std {ret_std:.1f}% > {self.max_return_std_pct:.1f}%"
                )
        for i, ws in enumerate(window_stats):
            if ws.trades_per_month < self.min_trades_per_month:
                violations.append(
                    f"W{i+1} trades/month {ws.trades_per_month:.1f} < {self.min_trades_per_month}"
                )
            if ws.trades_per_month > self.max_trades_per_month:
                violations.append(
                    f"W{i+1} trades/month {ws.trades_per_month:.1f} > {self.max_trades_per_month}"
                )
        return len(violations) == 0, violations

    def compute_soft_penalty(self, sharpe_ratio: float) -> float:
        if sharpe_ratio < self.min_sharpe:
            return (self.min_sharpe - sharpe_ratio) * self.sharpe_penalty_weight
        return 0.0

# ═══════════════════════════════════════════════════════════════
# WindowStats
# ═══════════════════════════════════════════════════════════════

class WindowStats:
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
        final_shares: np.ndarray | None = None,
        final_cash: float = 0.0,
        cost_basis: np.ndarray | None = None,
        quarter_shares: np.ndarray | None = None,
        quarter_cash: np.ndarray | None = None,
        quarter_nav: np.ndarray | None = None,
        quarter_prices: np.ndarray | None = None,
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
        self.quarter_shares = quarter_shares
        self.quarter_cash = quarter_cash
        self.quarter_nav = quarter_nav
        self.quarter_prices = quarter_prices

    @property
    def trades_per_month(self) -> float:
        if self.test_months <= 0:
            return 0.0
        return self.total_trades / self.test_months

    @property
    def excess_return(self) -> float:
        return self.test_excess_return

    def excess_vs(self, bench_label: str) -> float:
        if bench_label in self.benchmark_returns:
            return round(
                self.strategy_return - self.benchmark_returns[bench_label], 2
            )
        return self.test_excess_return

# ═══════════════════════════════════════════════════════════════
# BacktestConfig（从 backtest_config.py 迁移）
# ═══════════════════════════════════════════════════════════════

class BacktestConfig(_BaseModel):
    observe_end_month: int = 6
    trade_end_month: int = 18
    rf_rate: float = 2.0
    capital_injections: dict[int, float] = _Field(default_factory=dict)
    initial_capital: float = 100000.0
    monthly_buy_limit: float = float("inf")
    monthly_sell_limit: float = float("inf")
    commission_rate: float = 0.005
    lot_size_override: _Optional[dict[str, int]] = None

    def get_phase(self, elapsed_months):
        if elapsed_months < self.observe_end_month:
            return "observe"
        if elapsed_months >= self.trade_end_month:
            return "hold"
        return "trade"

    def can_trade(self, elapsed_months):
        return self.get_phase(elapsed_months) == "trade"

    def get_injection(self, month):
        return self.capital_injections.get(month, 0.0)

    def get_lot_size(self, stock_code, default):
        if self.lot_size_override and stock_code in self.lot_size_override:
            return self.lot_size_override[stock_code]
        return default


def elapsed_months(date_str, ref_date_str):
    d = _pd.Timestamp(date_str)
    ref = _pd.Timestamp(ref_date_str)
    months = (d.year - ref.year) * 12 + (d.month - ref.month)
    months += (d.day - ref.day) / 30.0
    return round(months, 2)


def make_training_config():
    from collections import OrderedDict
    injections = OrderedDict()
    for m in range(6, 13):
        injections[m] = 20000.0
    return BacktestConfig(
        observe_end_month=6, trade_end_month=12,
        capital_injections=injections, initial_capital=100000.0,
        monthly_buy_limit=float("inf"), monthly_sell_limit=float("inf"),
        commission_rate=0.002,
    )


def make_default_optimizer_config():
    from collections import OrderedDict
    injections = OrderedDict()
    for m in range(6, 13):
        injections[m] = 20000.0
    return BacktestConfig(
        observe_end_month=6, trade_end_month=18,
        capital_injections=injections, initial_capital=100000.0,
        monthly_buy_limit=float("inf"), monthly_sell_limit=float("inf"),
        commission_rate=0.002,
    )

# ═══════════════════════════════════════════════════════════════
# 单例加载
# ═══════════════════════════════════════════════════════════════

def load_constraints(path: Path | str | None = None) -> StrategyConstraints:
    config_path = Path(path) if path else DEFAULT_PATH
    if not config_path.exists():
        logger.warning("constraints config %s not found, using defaults", config_path)
        return StrategyConstraints()
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return StrategyConstraints(raw)

_global_constraints: StrategyConstraints | None = None

def get_constraints(path: Path | str | None = None) -> StrategyConstraints:
    global _global_constraints
    if _global_constraints is None:
        _global_constraints = load_constraints(path)
    return _global_constraints

def reload_constraints(path: Path | str | None = None) -> StrategyConstraints:
    global _global_constraints
    _global_constraints = load_constraints(path)
    return _global_constraints

def get_execution_config(path: Path | str | None = None) -> ExecutionConfig:
    return get_constraints(path).execution

def reload_execution_config(path: Path | str | None = None) -> ExecutionConfig:
    reload_constraints(path)
    return get_constraints().execution
