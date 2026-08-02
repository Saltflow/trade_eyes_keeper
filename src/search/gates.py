"""Declarative candidate gates over ranking-window metrics only."""

from __future__ import annotations

from dataclasses import dataclass
import operator
from typing import Callable, Iterable

import numpy as np

from .contracts import GateDecision, stable_hash


GATE_MODES = {"hard", "penalty", "diagnostic"}
OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
    "eq": operator.eq,
}
METRICS = {
    "weighted_strategy_return",
    "positive_return_windows",
    "positive_return_ratio",
    "ranking_window_count",
    "mean_strongest_benchmark_excess",
    "strongest_benchmark_win_windows",
    "strongest_benchmark_win_ratio",
    "mean_majority_benchmark_excess",
    "majority_benchmark_win_windows",
    "majority_benchmark_win_ratio",
    "average_position_pct",
    "minimum_drawdown_pct",
    "strategy_return_range_pct",
    "strongest_excess_range_pct",
    "majority_excess_range_pct",
    "excess_return_std_pct",
    "mean_sharpe_ratio",
    "minimum_trade_count",
    "average_trade_count",
    "average_signal_event_count",
    "average_cash_rejected_order_count",
    "average_concentration_hhi",
    "objective_score",
}


@dataclass(frozen=True)
class GateRule:
    rule_id: str
    metric: str
    mode: str
    operator: str
    value: float | tuple[float, float]
    penalty: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "GateRule":
        rule_id = str(raw.get("id", "")).strip()
        metric = str(raw.get("metric", "")).strip()
        mode = str(raw.get("mode", "hard")).strip().lower()
        op = str(raw.get("operator", "ge")).strip().lower()
        if not rule_id:
            raise ValueError("gate rule id cannot be empty")
        if metric not in METRICS:
            raise ValueError(f"gate {rule_id}: unknown metric {metric!r}")
        if mode not in GATE_MODES:
            raise ValueError(f"gate {rule_id}: unknown mode {mode!r}")
        if op not in OPERATORS and op not in {"between", "outside"}:
            raise ValueError(f"gate {rule_id}: unknown operator {op!r}")
        if op in {"between", "outside"}:
            interval = raw.get("range")
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValueError(f"gate {rule_id}: {op} requires a two-value range")
            value: float | tuple[float, float] = (
                float(interval[0]),
                float(interval[1]),
            )
            if value[1] < value[0]:
                raise ValueError(f"gate {rule_id}: range is reversed")
        else:
            if "value" not in raw:
                raise ValueError(f"gate {rule_id}: comparison value is required")
            value = float(raw["value"])
        penalty = float(raw.get("penalty", 0.0))
        if mode == "penalty" and penalty < 0:
            raise ValueError(f"gate {rule_id}: penalty cannot be negative")
        return cls(rule_id, metric, mode, op, value, penalty)

    def passes(self, metrics: dict[str, object]) -> bool:
        try:
            observed = float(metrics[self.metric])
        except (KeyError, TypeError, ValueError):
            return False
        if not np.isfinite(observed):
            return False
        if self.operator == "between":
            low, high = self.value
            return low <= observed <= high
        if self.operator == "outside":
            low, high = self.value
            return observed < low or observed > high
        return bool(OPERATORS[self.operator](observed, float(self.value)))

    def to_contract(self) -> dict[str, object]:
        return {
            "id": self.rule_id,
            "metric": self.metric,
            "mode": self.mode,
            "operator": self.operator,
            "value": self.value,
            "penalty": self.penalty,
        }


@dataclass(frozen=True)
class CandidateGatePipeline:
    profile_id: str
    rules: tuple[GateRule, ...]
    activation_eligible: bool = True

    @classmethod
    def from_config(
        cls, raw_config: dict[str, object], profile_id: str
    ) -> "CandidateGatePipeline":
        profiles = raw_config.get("gate_profiles", {})
        if not isinstance(profiles, dict) or profile_id not in profiles:
            raise ValueError(f"unknown gate profile {profile_id!r}")
        raw_profile = profiles[profile_id]
        if not isinstance(raw_profile, dict):
            raise ValueError(f"gate profile {profile_id!r} must be a mapping")
        raw_rules = raw_profile.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError(f"gate profile {profile_id!r} rules must be a list")
        rules = tuple(GateRule.from_dict(rule) for rule in raw_rules)
        ids = [rule.rule_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"gate profile {profile_id!r} has duplicate rule ids")
        _validate_hard_rule_intersections(profile_id, rules)
        return cls(
            profile_id=profile_id,
            rules=rules,
            activation_eligible=bool(raw_profile.get("activation_eligible", False)),
        )

    @property
    def hash(self) -> str:
        return stable_hash(
            {
                "profile_id": self.profile_id,
                "activation_eligible": self.activation_eligible,
                "rules": [rule.to_contract() for rule in self.rules],
            }
        )

    def evaluate(self, metrics: dict[str, object]) -> GateDecision:
        feasible = True
        penalty = 0.0
        results = []
        failures = []
        for rule in self.rules:
            passed = rule.passes(metrics)
            result = {
                "rule_id": rule.rule_id,
                "metric": rule.metric,
                "mode": rule.mode,
                "operator": rule.operator,
                "expected": rule.value,
                "observed": metrics.get(rule.metric),
                "passed": passed,
            }
            results.append(result)
            if passed:
                continue
            if rule.mode == "hard":
                feasible = False
                failures.append(rule.rule_id)
            elif rule.mode == "penalty":
                penalty += rule.penalty
        return GateDecision(
            feasible=feasible,
            penalty=penalty,
            results=tuple(results),
            failure_reasons=tuple(failures),
        )


def majority_benchmark_excess(
    stat: object, control_benchmarks: Iterable[str]
) -> float:
    """Return excess over the median configured control (beat any two of three)."""
    benchmark_returns = dict(getattr(stat, "benchmark_returns", {}) or {})
    controls = tuple(dict.fromkeys(str(name) for name in control_benchmarks))
    if len(controls) != 3 or not all(name in benchmark_returns for name in controls):
        return float("nan")
    hurdle = float(
        np.median([float(benchmark_returns[name]) for name in controls])
    )
    return float(getattr(stat, "strategy_return", 0.0)) - hurdle


def aggregate_ranking_metrics(
    ranking_stats: Iterable[object],
    objective_score: float,
    weights: Iterable[float] | None = None,
    control_benchmarks: Iterable[str] = (),
) -> dict[str, float | int]:
    """Create the only metric namespace accepted by gate profiles."""
    stats = list(ranking_stats)
    count = len(stats)
    returns = np.asarray(
        [float(getattr(stat, "strategy_return", 0.0)) for stat in stats],
        dtype=np.float64,
    )
    excess = np.asarray(
        [float(getattr(stat, "test_excess_return", 0.0)) for stat in stats],
        dtype=np.float64,
    )
    majority_excess = np.asarray(
        [majority_benchmark_excess(stat, control_benchmarks) for stat in stats],
        dtype=np.float64,
    )
    majority_complete = bool(count) and bool(np.all(np.isfinite(majority_excess)))
    configured_weights = list(weights or [])
    if count:
        if not configured_weights:
            configured_weights = [1.0] * count
        if len(configured_weights) < count:
            configured_weights.extend(
                [configured_weights[-1]] * (count - len(configured_weights))
            )
        normalized = np.asarray(configured_weights[:count], dtype=np.float64)
        normalized = np.maximum(normalized, 0.0)
        if normalized.sum() <= 0:
            normalized[:] = 1.0
        normalized /= normalized.sum()
    else:
        normalized = np.asarray([], dtype=np.float64)
    return {
        "weighted_strategy_return": (
            float(np.dot(returns, normalized)) if count else 0.0
        ),
        "positive_return_windows": int(np.sum(returns > 0.0)),
        "positive_return_ratio": float(np.mean(returns > 0.0)) if count else 0.0,
        "ranking_window_count": count,
        "mean_strongest_benchmark_excess": float(np.mean(excess)) if count else 0.0,
        "strongest_benchmark_win_windows": int(np.sum(excess > 0.0)),
        "strongest_benchmark_win_ratio": float(np.mean(excess > 0.0)) if count else 0.0,
        "mean_majority_benchmark_excess": (
            float(np.mean(majority_excess)) if majority_complete else -float("inf")
        ),
        "majority_benchmark_win_windows": (
            int(np.sum(majority_excess > 0.0)) if majority_complete else -1
        ),
        "majority_benchmark_win_ratio": (
            float(np.mean(majority_excess > 0.0))
            if majority_complete
            else -1.0
        ),
        "average_position_pct": (
            float(
                np.mean(
                    [float(getattr(stat, "avg_position_pct", 0.0)) for stat in stats]
                )
            )
            if count
            else 0.0
        ),
        "minimum_drawdown_pct": (
            float(
                np.min(
                    [float(getattr(stat, "max_drawdown_pct", 0.0)) for stat in stats]
                )
            )
            if count
            else 0.0
        ),
        "strategy_return_range_pct": (
            float(np.max(returns) - np.min(returns)) if count >= 2 else 0.0
        ),
        "strongest_excess_range_pct": (
            float(np.max(excess) - np.min(excess)) if count >= 2 else 0.0
        ),
        "majority_excess_range_pct": (
            float(np.max(majority_excess) - np.min(majority_excess))
            if count >= 2 and majority_complete
            else 0.0
        ),
        "excess_return_std_pct": float(np.std(excess)) if count >= 2 else 0.0,
        "mean_sharpe_ratio": (
            float(
                np.mean([float(getattr(stat, "sharpe_ratio", 0.0)) for stat in stats])
            )
            if count
            else 0.0
        ),
        "minimum_trade_count": int(
            min([int(getattr(stat, "total_trades", 0)) for stat in stats], default=0)
        ),
        "average_trade_count": (
            float(np.mean([int(getattr(stat, "total_trades", 0)) for stat in stats]))
            if count
            else 0.0
        ),
        "average_signal_event_count": (
            float(
                np.mean(
                    [int(getattr(stat, "signal_event_count", 0)) for stat in stats]
                )
            )
            if count
            else 0.0
        ),
        "average_cash_rejected_order_count": (
            float(
                np.mean(
                    [
                        int(getattr(stat, "cash_rejected_order_count", 0))
                        for stat in stats
                    ]
                )
            )
            if count
            else 0.0
        ),
        "average_concentration_hhi": (
            float(
                np.mean(
                    [float(getattr(stat, "concentration_hhi", 0.0)) for stat in stats]
                )
            )
            if count
            else 0.0
        ),
        "objective_score": float(objective_score),
    }


def _validate_hard_rule_intersections(
    profile_id: str, rules: tuple[GateRule, ...]
) -> None:
    """Reject impossible convex hard bounds at configuration load time."""
    by_metric: dict[str, list[GateRule]] = {}
    for rule in rules:
        if rule.mode == "hard" and rule.operator != "outside":
            by_metric.setdefault(rule.metric, []).append(rule)
    for metric, metric_rules in by_metric.items():
        lower = -float("inf")
        lower_inclusive = False
        upper = float("inf")
        upper_inclusive = False
        for rule in metric_rules:
            if rule.operator in {"gt", "ge"}:
                value = float(rule.value)
                inclusive = rule.operator == "ge"
                if value > lower or (value == lower and not inclusive):
                    lower = value
                    lower_inclusive = inclusive
            elif rule.operator in {"lt", "le"}:
                value = float(rule.value)
                inclusive = rule.operator == "le"
                if value < upper or (value == upper and not inclusive):
                    upper = value
                    upper_inclusive = inclusive
            elif rule.operator == "eq":
                value = float(rule.value)
                if value > lower:
                    lower = value
                    lower_inclusive = True
                elif value == lower:
                    lower_inclusive = lower_inclusive and True
                if value < upper:
                    upper = value
                    upper_inclusive = True
                elif value == upper:
                    upper_inclusive = upper_inclusive and True
            elif rule.operator == "between":
                interval_low, interval_high = rule.value
                if interval_low > lower:
                    lower = interval_low
                    lower_inclusive = True
                if interval_high < upper:
                    upper = interval_high
                    upper_inclusive = True
        impossible = lower > upper or (
            lower == upper and not (lower_inclusive and upper_inclusive)
        )
        if impossible:
            raise ValueError(
                f"gate profile {profile_id!r} has conflicting hard rules "
                f"for metric {metric!r}"
            )
