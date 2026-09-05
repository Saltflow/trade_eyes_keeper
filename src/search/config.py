"""统一配置管理 — 一次 YAML 解析，全系统复用。

读取 config/optimizer_constraints.yaml，提供：
  - ExecutionConfig：执行参数（资金、费率、持仓日）
  - WalkForwardConfig / GeneticSearchConfig / DiscreteSearchConfig：搜索配置
  - StrategyConstraints：硬性/软性约束检查
  - WindowStats / BacktestConfig：回测结果载体 + 时间线配置
  - get_market_optimizer_config()：严格的单市场独立配置

生产搜参必须使用 ``get_market_optimizer_config``；旧的
``get_constraints`` 仅保留给低层兼容测试，不参与市场策略选择。

用法:
    from src.search.config import get_constraints
    cfg = get_constraints()
    exec_cfg = cfg.execution
"""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional as _Optional

import numpy as np
import pandas as _pd
import yaml
from pydantic import BaseModel as _BaseModel, Field as _Field

from .contracts import stable_hash

logger = logging.getLogger(__name__)

DEFAULT_PATH = (
    Path(__file__).parent.parent.parent / "config" / "optimizer_constraints.yaml"
)
DEFAULT_APPLICATION_PATH = (
    Path(__file__).parent.parent.parent / "config" / "config.yaml"
)
MARKET_GROUPS = ("a_share", "hk", "us")
MARKET_CONFIG_FIELDS = (
    "strategy",
    "solver_id",
    "gate_profile",
    "walk_forward_profile",
    "execution_profile",
    "benchmark_profile",
)

# ═══════════════════════════════════════════════════════════════
# 执行配置
# ═══════════════════════════════════════════════════════════════


@dataclass
class ExecutionConfig:
    """搜参/日回报测通用执行参数。"""

    # Compatibility field for old callers.  The unified cash-cap simulator
    # deliberately ignores it; per-trade tiers are now the only amount cap.
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
        legacy_train_months = data.get("train_months", 12)
        self.state_lookback_months: int = int(
            data.get("state_lookback_months", legacy_train_months)
        )
        # Compatibility alias for old artifacts/tests. This period initializes
        # indicators and signal state; it never contains Solver observations.
        self.train_months: int = self.state_lookback_months
        self.test_months: int = data.get("test_months", 9)
        self.step_months: int = data.get("step_months", 3)
        self.num_windows: int = data.get("num_windows", 6)
        self.window_weights: list[float] = data.get(
            "window_weights", [1.0] * self.num_windows
        )
        legacy_stability_penalty = data.get("stability_penalty", 0.5)
        self.window_range_penalty: float = float(
            data.get("window_range_penalty", legacy_stability_penalty)
        )
        # Compatibility alias. New contracts use window_range_penalty because
        # the penalty is the best-minus-worst window spread, not volatility.
        self.stability_penalty: float = self.window_range_penalty
        self.data_years: float = max(
            1.0,
            float(data.get("data_years", self.total_months_needed / 12)),
        )
        # The newest N windows are held out.  They are for the daily report
        # validation only and must never be used to rank, filter, mutate, or
        # otherwise select a search candidate.
        self.validation_windows: int = max(0, int(data.get("validation_windows", 0)))
        # A 9-month test window advanced every 3 months overlaps the next two
        # windows.  When enabled, those overlapping historical windows are
        # purged before ranking so a daily-report hold-out is truly unseen.
        self.purge_overlapping_windows: bool = bool(
            data.get("purge_overlapping_windows", False)
        )

    @property
    def ranking_window_count(self) -> int:
        """Number of historical windows that may participate in selection."""
        return max(0, self.num_windows - min(self.validation_windows, self.num_windows))

    @property
    def held_out_window_count(self) -> int:
        """Number of configured validation windows actually available."""
        return min(self.validation_windows, self.num_windows)

    @property
    def purge_overlap_window_count(self) -> int:
        """Number of candidate windows embargoed before the holdout."""
        if not self.purge_overlapping_windows or self.held_out_window_count <= 0:
            return 0
        overlapping_steps = int(
            np.ceil(self.test_months / max(self.step_months, 1))
        ) - 1
        return min(self.ranking_window_count, max(0, overlapping_steps))

    @property
    def independent_ranking_window_count(self) -> int:
        """Ranking windows remaining after the explicit overlap purge."""
        return max(0, self.ranking_window_count - self.purge_overlap_window_count)

    @property
    def holdout_window_months(self) -> int:
        """Total test-window months represented by the held-out windows."""
        return self.held_out_window_count * self.test_months

    @property
    def holdout_calendar_span_months(self) -> int:
        """Calendar span from first to last held-out test window."""
        if self.held_out_window_count <= 0:
            return 0
        return self.test_months + (self.held_out_window_count - 1) * self.step_months

    @property
    def ranking_budget_months(self) -> int:
        """Non-overlap accounting: total horizon minus holdout and purge."""
        return max(
            0,
            self.total_months_needed
            - self.holdout_window_months
            - self.purge_overlap_window_count * self.step_months,
        )

    def ranking_weights(self, count: int) -> list[float]:
        """Return one non-negative scoring weight for every ranking window.

        Historic configuration files sometimes contain fewer weights than
        windows.  Truncating with ``zip`` silently dropped newer ranking
        windows, so retain the original weighted-mean formula while extending
        the final configured weight to every remaining ranking window.
        """
        if count <= 0:
            return []
        weights = [max(0.0, float(weight)) for weight in self.window_weights]
        if not weights:
            return [1.0] * count
        if len(weights) < count:
            weights.extend([weights[-1]] * (count - len(weights)))
        return weights[:count]

    @property
    def search_history_months(self) -> int:
        """Calendar months through the first held-out test window."""
        return max(0, self.total_months_needed - self.test_months)

    @property
    def total_months_needed(self) -> int:
        return (
            self.state_lookback_months
            + self.test_months
            + (self.num_windows - 1) * self.step_months
        )

    @property
    def test_months_calendar_days(self) -> int:
        return 273 if self.test_months == 9 else max(self.test_months * 30, 1)


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
        self.random_seed: int | None = (
            int(data["random_seed"]) if data.get("random_seed") is not None else None
        )
        self.sensitivity_top_candidates: int = max(
            1, int(data.get("sensitivity_top_candidates", 500))
        )
        self.sensitivity_samples: int = max(1, int(data.get("sensitivity_samples", 10)))
        self.sensitivity_penalty_weight: float = max(
            0.0, float(data.get("sensitivity_penalty_weight", 1.0))
        )
        self.evaluation_workers: int = max(1, int(data.get("evaluation_workers", 4)))
        self.min_weighted_strategy_return: float = float(
            data.get("min_weighted_strategy_return", 0.0)
        )
        self.min_positive_return_windows: int = max(
            0, int(data.get("min_positive_return_windows", 0))
        )
        self.min_winning_benchmark_windows: int = max(
            0, int(data.get("min_winning_benchmark_windows", 0))
        )


class LocalSensitivityConfig:
    """Configurable one-level parameter-neighbour validation.

    Objective scores may legitimately be negative because the primary score
    subtracts a cross-window range penalty. Local robustness therefore
    measures neighbour feasibility and relative score deterioration; it must
    never require an absolute score greater than zero.
    """

    def __init__(self, data: dict | None = None):
        raw = data or {}
        self.enabled: bool = bool(raw.get("enabled", True))
        self.minimum_feasible_ratio: float = float(
            raw.get("minimum_feasible_ratio", 0.80)
        )
        if not 0.0 <= self.minimum_feasible_ratio <= 1.0:
            raise ValueError(
                "validation.local_sensitivity.minimum_feasible_ratio "
                "must be between 0 and 1"
            )
        self.activation_required: bool = bool(
            raw.get("activation_required", True)
        )

    def to_contract(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "minimum_feasible_ratio": self.minimum_feasible_ratio,
            "activation_required": self.activation_required,
        }


class UniverseRobustnessConfig:
    """Configurable leave-one-instrument-out finalist validation."""

    def __init__(self, data: dict | None = None):
        raw = data or {}
        self.enabled: bool = bool(raw.get("enabled", True))
        self.finalist_count: int = max(1, int(raw.get("finalist_count", 20)))
        self.minimum_passing_ratio: float = float(
            raw.get("minimum_passing_ratio", 0.80)
        )
        if not 0.0 <= self.minimum_passing_ratio <= 1.0:
            raise ValueError(
                "validation.universe_robustness.minimum_passing_ratio "
                "must be between 0 and 1"
            )
        self.small_universe_threshold: int = max(
            1, int(raw.get("small_universe_threshold", 5))
        )
        self.small_universe_allowed_failures: int = max(
            0, int(raw.get("small_universe_allowed_failures", 0))
        )
        self.minimum_mean_majority_excess: float = float(
            raw.get("minimum_mean_majority_excess", 0.0)
        )
        self.penalty_weight: float = max(
            0.0, float(raw.get("penalty_weight", 2.0))
        )
        self.activation_required: bool = bool(
            raw.get("activation_required", True)
        )
        self.require_order_invariance: bool = bool(
            raw.get("require_order_invariance", True)
        )

    def required_positive_variants(self, symbol_count: int) -> int:
        """Resolve ratio rounding while allowing explicit small-pool tolerance."""
        count = max(0, int(symbol_count))
        if count == 0:
            return 0
        ratio_required = max(
            1,
            int(np.ceil(count * self.minimum_passing_ratio)),
        )
        if count <= self.small_universe_threshold:
            allowed = min(self.small_universe_allowed_failures, count - 1)
            return min(ratio_required, max(1, count - allowed))
        return ratio_required

    def to_contract(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "finalist_count": self.finalist_count,
            "minimum_passing_ratio": self.minimum_passing_ratio,
            "small_universe_threshold": self.small_universe_threshold,
            "small_universe_allowed_failures": (
                self.small_universe_allowed_failures
            ),
            "minimum_mean_majority_excess": self.minimum_mean_majority_excess,
            "penalty_weight": self.penalty_weight,
            "activation_required": self.activation_required,
            "require_order_invariance": self.require_order_invariance,
        }


class SearchRuntimeConfig:
    """Solver-neutral orchestration and CPU scheduling configuration."""

    def __init__(self, data: dict, genetic: dict):
        self.solver_id: str = str(data.get("solver_id", "genetic"))
        self.gate_profile: str = str(data.get("gate_profile", "standard"))
        self.batch_size: int = int(data.get("batch_size", 256))
        if not 128 <= self.batch_size <= 512:
            raise ValueError("search.batch_size must be between 128 and 512")
        self.parallel_axis: str = str(data.get("parallel_axis", "candidate_window"))
        self.evaluation_backend: str = str(
            data.get("evaluation_backend", "process")
        )
        if self.evaluation_backend not in {"process", "scalar"}:
            raise ValueError(
                "search.evaluation_backend must be 'process' or 'scalar'"
            )
        configured_workers = os.environ.get("SEARCH_WORKERS", data.get("workers"))
        self.workers: int | None = (
            int(configured_workers) if configured_workers is not None else None
        )
        if self.workers is not None and self.workers < 1:
            raise ValueError("search.workers/SEARCH_WORKERS must be at least 1")
        self.checkpoint: bool = bool(data.get("checkpoint", True))
        self.candidate_retention_ratio: float = float(
            data.get("candidate_retention_ratio", 0.05)
        )
        if not 0.0 < self.candidate_retention_ratio <= 1.0:
            raise ValueError(
                "search.candidate_retention_ratio must be greater than 0 and at most 1"
            )
        self.run_retention_count: int = int(data.get("run_retention_count", 3))
        if not 1 <= self.run_retention_count <= 100:
            raise ValueError(
                "search.run_retention_count must be between 1 and 100"
            )
        configured = data.get("solvers", {}) or {}
        if not isinstance(configured, dict):
            raise ValueError("search.solvers must be a mapping")
        self._solver_configs = {
            str(key): dict(value or {}) for key, value in configured.items()
        }
        # Preserve every historical GA setting through the generic solver
        # configuration boundary.  Explicit ``search.solvers.genetic`` values
        # take precedence.
        self._genetic_defaults = dict(genetic)

    def solver_config(self, solver_id: str | None = None) -> dict:
        selected = str(solver_id or self.solver_id)
        if selected == "genetic":
            return {
                **self._genetic_defaults,
                **self._solver_configs.get(selected, {}),
            }
        return dict(self._solver_configs.get(selected, {}))


# ═══════════════════════════════════════════════════════════════
# 离散搜索空间配置
# ═══════════════════════════════════════════════════════════════


class DiscreteSearchConfig:
    def __init__(self, data: dict, cash_tiers: dict | None = None):
        self.buy_builders: list[str] = data.get(
            "buy_builders",
            [
                "deviation_cross",
                "rsi_signal",
                "bollinger_signal",
                "volume_spike",
                "deviation_absolute",
                "trend_follow",
                "none",
            ],
        )
        self.threshold_levels: int = data.get("threshold_levels", 10)
        self.num_buy_rules: int = data.get("num_buy_rules", 5)
        self.sell_builders: list[str] = data.get(
            "sell_builders",
            [
                "deviation_cross",
                "rsi_signal",
                "bollinger_signal",
                "deviation_absolute",
                "trend_follow",
                "none",
            ],
        )
        self.num_sell_rules: int = data.get("num_sell_rules", 3)
        ss = cash_tiers or data.get("simplified_search", {})
        self.buy_limit_levels: list[float] = ss.get(
            "buy_limit_levels", [5000.0, 10000.0, 20000.0, 30000.0, 50000.0]
        )
        self.sell_limit_levels: list[float] = ss.get(
            "sell_limit_levels", [5000.0, 10000.0, 20000.0, 30000.0, 50000.0]
        )

    @property
    def search_space_size(self) -> int:
        buy_singles = (
            len(self.buy_builders) * self.threshold_levels * len(self.buy_limit_levels)
        )
        sell_singles = (
            len(self.sell_builders)
            * self.threshold_levels
            * len(self.sell_limit_levels)
        )
        return (buy_singles**self.num_buy_rules) * (sell_singles**self.num_sell_rules)


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
        sc = raw_config.get("soft_constraints", {})
        self.min_sharpe: float = sc.get("min_sharpe", 0.5)
        self.sharpe_penalty_weight: float = sc.get("sharpe_penalty_weight", 0.3)
        self.walk_forward = WalkForwardConfig(raw_config.get("walk_forward", {}))
        self.search = SearchRuntimeConfig(
            raw_config.get("search", {}), raw_config.get("genetic_search", {})
        )
        self.genetic_search = GeneticSearchConfig(raw_config.get("genetic_search", {}))
        validation = raw_config.get("validation", {}) or {}
        self.local_sensitivity = LocalSensitivityConfig(
            validation.get("local_sensitivity", {})
        )
        self.universe_robustness = UniverseRobustnessConfig(
            validation.get("universe_robustness", {})
        )
        self.discrete_search = DiscreteSearchConfig(
            raw_config.get("discrete_search", {}),
            raw_config.get("simplified_search", {}),
        )
        bc = raw_config.get("benchmarks", {})
        self.benchmark_codes: list[str] = []
        self.risk_free_rate: float = 0.02
        self._raw_benchmarks = bc
        self.market_group: str | None = None
        self.market_config_hash: str = ""
        self.market_metadata: dict[str, object] = {}

    def set_group(self, group: str):
        self.benchmark_codes = list(self._raw_benchmarks.get(group, []))
        rates = self._raw_benchmarks.get("risk_free_rates", {})
        self.risk_free_rate = rates.get(group, 0.02)

    def benchmark_codes_for(self, group: str) -> list[str]:
        return list(self._raw_benchmarks.get(group, []))

    def primary_benchmark_for(self, group: str) -> str:
        primary = self._raw_benchmarks.get("primary", {}) or {}
        configured = str(primary.get(group, "")).strip()
        if configured:
            return configured
        return next(
            (code for code in self.benchmark_codes_for(group) if code != "risk_free"),
            "risk_free",
        )

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
        self,
        window_stats,
        walk_forward_score,
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
                    f"W{i+1} max dd {ws.max_drawdown_pct:.1f}% "
                    f"< {self.max_drawdown_pct:.1f}%"
                )
        test_returns = [ws.test_excess_return for ws in window_stats]
        if len(test_returns) >= 2:
            ret_std = float(np.std(test_returns))
            if ret_std > self.max_return_std_pct:
                violations.append(
                    f"test return std {ret_std:.1f}% > {self.max_return_std_pct:.1f}%"
                )
        return len(violations) == 0, violations

    def compute_soft_penalty(self, sharpe_ratio: float) -> float:
        if sharpe_ratio < self.min_sharpe:
            return (self.min_sharpe - sharpe_ratio) * self.sharpe_penalty_weight
        return 0.0


@dataclass
class MarketOptimizerConfig:
    """Fully resolved optimizer contract for exactly one market.

    The application must resolve one of these objects before entering the
    optimizer.  ``constraints`` is intentionally unique to this market; it is
    never mutated by another market's evaluation.
    """

    group: str
    strategy_name: str
    solver_id: str
    gate_profile: str
    walk_forward_profile: str
    execution_profile: str
    benchmark_profile: str
    constraints: StrategyConstraints
    config_hash: str

    @property
    def strategy(self):
        from ..strategy import get_strategy

        return get_strategy(self.strategy_name)

    @property
    def search(self) -> SearchRuntimeConfig:
        return self.constraints.search

    @property
    def execution(self) -> ExecutionConfig:
        return self.constraints.execution

    def to_contract(self) -> dict[str, object]:
        return {
            "group": self.group,
            "strategy": self.strategy_name,
            "solver_id": self.solver_id,
            "gate_profile": self.gate_profile,
            "walk_forward_profile": self.walk_forward_profile,
            "execution_profile": self.execution_profile,
            "benchmark_profile": self.benchmark_profile,
            "config_hash": self.config_hash,
        }


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
        initial_asset: float = 0.0,
        final_asset: float = 0.0,
        final_position_pct: float = 0.0,
        final_shares: np.ndarray | None = None,
        final_prices: np.ndarray | None = None,
        final_cash: float = 0.0,
        cost_basis: np.ndarray | None = None,
        quarter_shares: np.ndarray | None = None,
        quarter_cash: np.ndarray | None = None,
        quarter_nav: np.ndarray | None = None,
        quarter_prices: np.ndarray | None = None,
        quarter_cost_basis: np.ndarray | None = None,
        pending_order_count: int = 0,
        signal_event_count: int = 0,
        cash_rejected_order_count: int = 0,
        concentration_hhi: float = 0.0,
        selected_basket_hold_return: float | None = None,
        timing_value_add: float | None = None,
        strongest_benchmark: str = "",
        benchmark_raw_returns: dict[str, float] | None = None,
    ):
        self.test_excess_return = test_excess_return
        self.max_drawdown_pct = max_drawdown_pct
        self.avg_position_pct = avg_position_pct
        self.sharpe_ratio = sharpe_ratio
        self.total_trades = total_trades
        self.test_months = test_months
        self.benchmark_returns: dict[str, float] = benchmark_returns or {}
        self.strategy_return = strategy_return
        self.initial_asset = initial_asset
        self.final_asset = final_asset
        self.final_position_pct = final_position_pct
        self.final_shares = final_shares
        self.final_prices = final_prices
        self.final_cash = final_cash
        self.cost_basis = cost_basis
        self.quarter_shares = quarter_shares
        self.quarter_cash = quarter_cash
        self.quarter_nav = quarter_nav
        self.quarter_prices = quarter_prices
        self.quarter_cost_basis = quarter_cost_basis
        self.pending_order_count = pending_order_count
        self.signal_event_count = signal_event_count
        self.cash_rejected_order_count = cash_rejected_order_count
        self.concentration_hhi = concentration_hhi
        self.selected_basket_hold_return = selected_basket_hold_return
        self.timing_value_add = timing_value_add
        self.strongest_benchmark = strongest_benchmark
        self.benchmark_raw_returns = benchmark_raw_returns or {}

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
            return round(self.strategy_return - self.benchmark_returns[bench_label], 2)
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
        observe_end_month=6,
        trade_end_month=12,
        capital_injections=injections,
        initial_capital=100000.0,
        monthly_buy_limit=float("inf"),
        monthly_sell_limit=float("inf"),
        commission_rate=0.002,
    )


def make_default_optimizer_config():
    from collections import OrderedDict

    injections = OrderedDict()
    for m in range(6, 13):
        injections[m] = 20000.0
    return BacktestConfig(
        observe_end_month=6,
        trade_end_month=18,
        capital_injections=injections,
        initial_capital=100000.0,
        monthly_buy_limit=float("inf"),
        monthly_sell_limit=float("inf"),
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


def _load_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        raise ValueError(f"configuration file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to load configuration {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return raw


def _profile_mapping(raw: dict, section: str, profile_id: str, group: str) -> dict:
    profiles = raw.get(section)
    if not isinstance(profiles, dict):
        raise ValueError(
            f"{group}: required profile registry {section!r} is missing"
        )
    value = profiles.get(profile_id)
    if not isinstance(value, dict):
        raise ValueError(
            f"{group}: unknown or invalid {section} profile {profile_id!r}"
        )
    return deepcopy(value)


def _profile_value(raw: dict, section: str, profile_id: str, group: str):
    profiles = raw.get(section)
    if not isinstance(profiles, dict) or profile_id not in profiles:
        raise ValueError(
            f"{group}: unknown or missing {section} profile {profile_id!r}"
        )
    value = profiles[profile_id]
    if not isinstance(value, (dict, list, tuple)):
        raise ValueError(
            f"{group}: invalid {section} profile {profile_id!r}"
        )
    return deepcopy(value)


def _application_config(application_config: dict | Path | str | None) -> dict:
    if isinstance(application_config, dict):
        return deepcopy(application_config)
    return _load_yaml_mapping(
        Path(application_config) if application_config else DEFAULT_APPLICATION_PATH
    )


def _resolved_market_raw(
    application_config: dict,
    group: str,
    constraints_raw: dict,
) -> tuple[dict, dict]:
    optimizer = application_config.get("optimizer")
    if not isinstance(optimizer, dict):
        raise ValueError("optimizer.markets is required")
    forbidden = sorted(
        key
        for key in ("engine", "strategy_by_group")
        if key in optimizer
    )
    if forbidden:
        raise ValueError(
            "global optimizer fallback fields are forbidden: "
            + ", ".join(forbidden)
        )
    markets = optimizer.get("markets")
    if not isinstance(markets, dict):
        raise ValueError("optimizer.markets must be a mapping")
    unknown = sorted(set(markets) - set(MARKET_GROUPS))
    missing = sorted(set(MARKET_GROUPS) - set(markets))
    if unknown:
        raise ValueError(f"optimizer.markets has unknown groups: {unknown}")
    if missing:
        raise ValueError(f"optimizer.markets is missing groups: {missing}")
    spec = markets.get(group)
    if not isinstance(spec, dict):
        raise ValueError(f"optimizer.markets.{group} must be a mapping")
    missing_fields = [key for key in MARKET_CONFIG_FIELDS if not spec.get(key)]
    if missing_fields:
        raise ValueError(
            f"{group}: missing required optimizer fields: {missing_fields}"
        )

    strategy_name = str(spec["strategy"]).strip().lower()
    solver_id = str(spec["solver_id"]).strip().lower()
    gate_profile = str(spec["gate_profile"]).strip()
    walk_forward_profile = str(spec["walk_forward_profile"]).strip()
    execution_profile = str(spec["execution_profile"]).strip()
    benchmark_profile = str(spec["benchmark_profile"]).strip()
    if not all(
        (strategy_name, solver_id, gate_profile, walk_forward_profile,
         execution_profile, benchmark_profile)
    ):
        raise ValueError(f"{group}: optimizer fields cannot be empty")

    from ..strategy import get_strategy
    from .gates import CandidateGatePipeline
    from .registry import create_solver
    strategy = get_strategy(strategy_name)
    if strategy is None:
        raise ValueError(f"{group}: unknown strategy {strategy_name!r}")
    if not strategy.supports_market(group):
        raise ValueError(
            f"{group}: strategy {strategy_name!r} does not support this market"
        )
    try:
        solver = create_solver(solver_id)
    except ValueError as exc:
        raise ValueError(f"{group}: {exc}") from exc

    raw = deepcopy(constraints_raw)
    raw["walk_forward"] = _profile_mapping(
        raw, "walk_forward_profiles", walk_forward_profile, group
    )
    raw["execution_params"] = _profile_mapping(
        raw, "execution_profiles", execution_profile, group
    )
    walk_forward = raw["walk_forward"]
    required_wf_fields = (
        "state_lookback_months",
        "test_months",
        "step_months",
        "num_windows",
        "validation_windows",
        "data_years",
    )
    missing_wf_fields = [
        key for key in required_wf_fields if key not in walk_forward
    ]
    if missing_wf_fields:
        raise ValueError(
            f"{group}: {walk_forward_profile} is missing Walk-Forward fields "
            f"{missing_wf_fields}"
        )
    try:
        positive_wf_fields = (
            "state_lookback_months",
            "test_months",
            "step_months",
            "num_windows",
            "data_years",
        )
        if any(float(walk_forward[key]) <= 0 for key in positive_wf_fields):
            raise ValueError("Walk-Forward lengths and data_years must be positive")
        if float(walk_forward["validation_windows"]) < 0:
            raise ValueError("validation_windows cannot be negative")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{group}: invalid Walk-Forward profile") from exc

    execution_profile_raw = raw["execution_params"]
    required_execution_fields = (
        "initial_capital",
        "commission_rate",
        "min_holding_days",
        "lot_sizes",
        "fx_rates",
    )
    missing_execution_fields = [
        key for key in required_execution_fields if key not in execution_profile_raw
    ]
    if missing_execution_fields:
        raise ValueError(
            f"{group}: {execution_profile} is missing execution fields "
            f"{missing_execution_fields}"
        )
    lot_sizes = execution_profile_raw["lot_sizes"]
    fx_rates = execution_profile_raw["fx_rates"]
    if not isinstance(lot_sizes, dict) or group not in lot_sizes:
        raise ValueError(
            f"{group}: execution profile must declare lot_sizes.{group}"
        )
    if not isinstance(fx_rates, dict) or group not in fx_rates:
        raise ValueError(
            f"{group}: execution profile must declare fx_rates.{group}"
        )
    try:
        if float(execution_profile_raw["initial_capital"]) <= 0:
            raise ValueError("initial_capital must be positive")
        if float(execution_profile_raw["commission_rate"]) < 0:
            raise ValueError("commission_rate cannot be negative")
        if int(execution_profile_raw["min_holding_days"]) < 0:
            raise ValueError("min_holding_days cannot be negative")
        if int(lot_sizes[group]) <= 0 or float(fx_rates[group]) <= 0:
            raise ValueError("market lot size and FX rate must be positive")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{group}: invalid execution profile") from exc
    benchmark_profile_value = _profile_value(
        raw, "benchmark_profiles", benchmark_profile, group
    )
    if isinstance(benchmark_profile_value, dict):
        benchmark_codes = benchmark_profile_value.get("codes")
        if not isinstance(benchmark_codes, list) or not benchmark_codes:
            raise ValueError(f"{group}: benchmark profile must contain codes")
        risk_free_rate = benchmark_profile_value.get("risk_free_rate")
    else:
        benchmark_codes = benchmark_profile_value
        risk_free_rate = None
    benchmarks = raw.get("benchmarks", {})
    if not isinstance(benchmarks, dict):
        benchmarks = {}
    benchmarks[group] = list(benchmark_codes)
    rates = dict(benchmarks.get("risk_free_rates", {}) or {})
    if risk_free_rate is not None:
        rates[group] = float(risk_free_rate)
    elif group not in rates:
        raise ValueError(f"{group}: benchmark profile has no risk-free rate")
    benchmarks["risk_free_rates"] = rates
    raw["benchmarks"] = benchmarks

    search = raw.get("search", {})
    if not isinstance(search, dict):
        raise ValueError("search must be a mapping")
    if "solver_id" in search or "gate_profile" in search:
        raise ValueError(
            "global search.solver_id/search.gate_profile are forbidden; "
            "declare them under every optimizer.markets entry"
        )
    search = deepcopy(search)
    search["solver_id"] = solver_id
    search["gate_profile"] = gate_profile
    solver_configs = search.get("solvers", {})
    if not isinstance(solver_configs, dict) or solver_id not in solver_configs:
        raise ValueError(f"{group}: no search configuration for Solver {solver_id!r}")
    market_search = spec.get("search", {})
    if market_search is not None and not isinstance(market_search, dict):
        raise ValueError(f"{group}: optimizer market search must be a mapping")
    if isinstance(market_search, dict):
        conflicting_search_keys = sorted(
            key for key in ("solver_id", "gate_profile") if key in market_search
        )
        if conflicting_search_keys:
            raise ValueError(
                f"{group}: search selection must use the market fields; "
                f"do not duplicate {conflicting_search_keys} under search"
            )
        search.update(deepcopy(market_search))
        search["solver_id"] = solver_id
        search["gate_profile"] = gate_profile
    solver_overrides = spec.get("solver_config", {}) or {}
    if not isinstance(solver_overrides, dict):
        raise ValueError(f"{group}: solver_config must be a mapping")
    solver_configs = deepcopy(solver_configs)
    solver_configs[solver_id] = {
        **dict(solver_configs[solver_id] or {}),
        **deepcopy(solver_overrides),
    }
    search["solvers"] = solver_configs
    raw["search"] = search

    CandidateGatePipeline.from_config(raw, gate_profile)
    schema = strategy.parameter_schema
    if any(item.active_if for item in schema.parameters) and not solver.capabilities.conditional_parameters:
        raise ValueError(
            f"{group}: Solver {solver_id!r} cannot handle conditional parameters"
        )
    # The actual SearchController performs the complete capability assertion;
    # this lightweight call catches the common incompatible contract here.
    if solver.capabilities.requires_gradients:
        raise ValueError(
            f"{group}: Solver {solver_id!r} requires unavailable gradients"
        )

    contract = {
        "group": group,
        "strategy": strategy_name,
        "solver_id": solver_id,
        "gate_profile": gate_profile,
        "walk_forward_profile": walk_forward_profile,
        "execution_profile": execution_profile,
        "benchmark_profile": benchmark_profile,
        "market_spec": spec,
        "constraints": raw,
    }
    return raw, contract


def load_market_optimizer_config(
    group: str,
    application_config: dict | Path | str | None = None,
    constraints_path: Path | str | None = None,
) -> MarketOptimizerConfig:
    """Resolve one strict, independent optimizer contract for ``group``."""
    if group not in MARKET_GROUPS:
        raise ValueError(f"unknown optimizer market group: {group}")
    app = _application_config(application_config)
    constraints_path = Path(constraints_path) if constraints_path else DEFAULT_PATH
    constraints_raw = _load_yaml_mapping(constraints_path)
    raw, contract = _resolved_market_raw(app, group, constraints_raw)
    constraints = StrategyConstraints(raw)
    constraints.benchmark_codes = list(raw["benchmarks"][group])
    constraints.risk_free_rate = float(
        raw["benchmarks"]["risk_free_rates"][group]
    )
    constraints.market_group = group
    constraints.market_config_hash = stable_hash(contract)
    constraints.market_metadata = {
        "market_group": group,
        "strategy_id": contract["strategy"],
        "solver_id": contract["solver_id"],
        "gate_profile": contract["gate_profile"],
        "walk_forward_profile": contract["walk_forward_profile"],
        "execution_profile": contract["execution_profile"],
        "benchmark_profile": contract["benchmark_profile"],
        "market_config_hash": constraints.market_config_hash,
    }
    return MarketOptimizerConfig(
        group=group,
        strategy_name=str(contract["strategy"]),
        solver_id=str(contract["solver_id"]),
        gate_profile=str(contract["gate_profile"]),
        walk_forward_profile=str(contract["walk_forward_profile"]),
        execution_profile=str(contract["execution_profile"]),
        benchmark_profile=str(contract["benchmark_profile"]),
        constraints=constraints,
        config_hash=constraints.market_config_hash,
    )


def get_market_optimizer_config(
    group: str,
    application_config: dict | Path | str | None = None,
    constraints_path: Path | str | None = None,
) -> MarketOptimizerConfig:
    return load_market_optimizer_config(group, application_config, constraints_path)


def get_market_optimizer_configs(
    application_config: dict | Path | str | None = None,
    constraints_path: Path | str | None = None,
    groups: tuple[str, ...] = MARKET_GROUPS,
) -> dict[str, MarketOptimizerConfig]:
    """Validate and resolve all requested markets without any fallback."""
    selected = tuple(dict.fromkeys(groups))
    invalid = sorted(set(selected) - set(MARKET_GROUPS))
    if invalid:
        raise ValueError(f"unknown optimizer market groups: {invalid}")
    app = _application_config(application_config)
    return {
        group: load_market_optimizer_config(
            group, app, constraints_path
        )
        for group in selected
    }


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
