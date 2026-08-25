"""Long-horizon, no-lookahead evaluation for intrinsic-value estimates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .intrinsic_value import (
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
    SubjectiveRiskAdjustment,
)


@dataclass(frozen=True)
class IntrinsicEvaluationConfig:
    horizons: tuple[int, ...] = (252, 504)
    minimum_cross_section: int = 12
    selection_fraction: float = 0.20
    minimum_confidence: float = 0.50


def _rank_ic(score: np.ndarray, target: np.ndarray) -> float | None:
    valid = np.isfinite(score) & np.isfinite(target)
    if valid.sum() < 3 or np.std(score[valid]) <= 1e-12:
        return None
    result = spearmanr(score[valid], target[valid]).statistic
    return float(result) if np.isfinite(result) else None


class IntrinsicValueWalkForwardEvaluator:
    """Evaluate margin-of-safety ranks over one- and two-year horizons."""

    def __init__(
        self,
        builder: PointInTimeValuationBuilder,
        value_config: IntrinsicValueConfig | None = None,
        evaluation_config: IntrinsicEvaluationConfig | None = None,
    ):
        self.builder = builder
        self.value_config = value_config or IntrinsicValueConfig()
        self.evaluation_config = (
            evaluation_config or IntrinsicEvaluationConfig()
        )
        self.engine = IntrinsicValueEngine(self.value_config)

    @staticmethod
    def _anchors(bundles: dict[str, Any]) -> list[date]:
        by_quarter: dict[tuple[int, int], list[date]] = {}
        for bundle in bundles.values():
            for timestamp in pd.to_datetime(bundle.prices["date"]):
                current = timestamp.date()
                key = (current.year, (current.month - 1) // 3 + 1)
                by_quarter.setdefault(key, []).append(current)
        return sorted(max(items) for items in by_quarter.values())

    @staticmethod
    def _forward_label(
        bundle: Any,
        market_date: date,
        horizon: int,
    ) -> dict[str, Any] | None:
        dates = pd.to_datetime(bundle.prices["date"]).dt.date.to_numpy()
        index = int(np.searchsorted(dates, market_date, side="left"))
        end_index = index + horizon
        if index < 0 or end_index >= len(bundle.prices):
            return None
        start = float(bundle.prices.iloc[index]["qfq_close"])
        end = float(bundle.prices.iloc[end_index]["qfq_close"])
        path = pd.to_numeric(
            bundle.prices.iloc[index : end_index + 1]["qfq_close"],
            errors="coerce",
        ).to_numpy(dtype=np.float64)
        if (
            not np.isfinite(start)
            or not np.isfinite(end)
            or start <= 0
            or end <= 0
            or not np.isfinite(path).any()
        ):
            return None
        return {
            "label_end_date": pd.Timestamp(
                bundle.prices.iloc[end_index]["date"]
            ).date(),
            "forward_return": end / start - 1.0,
            "maximum_wealth_multiple": float(np.nanmax(path) / start),
        }

    def _metrics(
        self, rows: list[dict[str, Any]], horizon: int
    ) -> dict[str, Any]:
        selected = [item for item in rows if item["horizon"] == horizon]
        score_names = (
            "margin_of_safety",
            "fair_value_gap",
            "earnings_yield",
            "book_yield",
            "dividend_yield",
        )
        by_date: dict[str, list[dict[str, Any]]] = {}
        for item in selected:
            by_date.setdefault(item["evaluation_date"], []).append(item)
        quarterly: dict[str, dict[str, float | None]] = {}
        spreads: dict[str, list[float]] = {name: [] for name in score_names}
        ics: dict[str, list[float]] = {name: [] for name in score_names}
        for evaluation_date, items in sorted(by_date.items()):
            if len(items) < self.evaluation_config.minimum_cross_section:
                continue
            target = np.asarray(
                [item["forward_return"] for item in items], dtype=np.float64
            )
            current: dict[str, float | None] = {}
            for name in score_names:
                scores = np.asarray(
                    [
                        np.nan if item[name] is None else item[name]
                        for item in items
                    ],
                    dtype=np.float64,
                )
                value = _rank_ic(scores, target)
                current[name] = value
                if value is not None:
                    ics[name].append(value)
                valid = np.isfinite(scores) & np.isfinite(target)
                count = int(valid.sum())
                bucket = max(
                    1,
                    int(np.floor(
                        count * self.evaluation_config.selection_fraction
                    )),
                )
                if count >= max(4, bucket * 2):
                    order = np.argsort(scores[valid])
                    valid_target = target[valid]
                    spread = float(
                        np.mean(valid_target[order[-bucket:]])
                        - np.mean(valid_target[order[:bucket]])
                    )
                    spreads[name].append(spread)
            quarterly[evaluation_date] = current
        buy_rows = [
            item
            for item in selected
            if item["margin_of_safety"] is not None
            and item["margin_of_safety"] >= 0.0
            and item["confidence"] >= self.evaluation_config.minimum_confidence
        ]
        return {
            "horizon_trading_days": horizon,
            "row_count": len(selected),
            "quarter_count": len(by_date),
            "scores": {
                name: {
                    "mean_rank_ic": (
                        float(np.mean(ics[name])) if ics[name] else None
                    ),
                    "rank_ic_std": (
                        float(np.std(ics[name])) if ics[name] else None
                    ),
                    "positive_quarter_rate": (
                        float(np.mean(np.asarray(ics[name]) > 0))
                        if ics[name]
                        else None
                    ),
                    "mean_top_bottom_spread": (
                        float(np.mean(spreads[name]))
                        if spreads[name]
                        else None
                    ),
                }
                for name in score_names
            },
            "buy_signals": {
                "count": len(buy_rows),
                "mean_forward_return": (
                    float(np.mean([
                        item["forward_return"] for item in buy_rows
                    ]))
                    if buy_rows
                    else None
                ),
                "positive_return_rate": (
                    float(np.mean([
                        item["forward_return"] > 0 for item in buy_rows
                    ]))
                    if buy_rows
                    else None
                ),
                "fair_value_reached_rate": (
                    float(np.mean([
                        item["fair_value_reached"] for item in buy_rows
                    ]))
                    if buy_rows
                    else None
                ),
            },
            "quarterly_rank_ic": quarterly,
        }

    def run(
        self,
        *,
        symbols: Iterable[str] | None = None,
        risks: dict[str, SubjectiveRiskAdjustment] | None = None,
    ) -> dict[str, Any]:
        selected = self.builder.available_symbols(symbols)
        bundles = {
            symbol: bundle
            for symbol in selected
            if (bundle := self.builder.market_store.read(symbol)) is not None
        }
        rows: list[dict[str, Any]] = []
        leakage_violations = 0
        for anchor in self._anchors(bundles):
            for symbol, bundle in bundles.items():
                snapshot = self.builder.snapshot(
                    symbol, anchor, self.value_config
                )
                if snapshot is None:
                    continue
                estimate = self.engine.estimate(
                    snapshot, (risks or {}).get(symbol)
                )
                if estimate.buy_price is None:
                    continue
                if estimate.market_date > anchor:
                    leakage_violations += 1
                    continue
                for horizon in self.evaluation_config.horizons:
                    label = self._forward_label(
                        bundle, estimate.market_date, horizon
                    )
                    if label is None:
                        continue
                    wealth_high = (
                        estimate.current_price
                        * label["maximum_wealth_multiple"]
                    )
                    rows.append({
                        "symbol": symbol,
                        "evaluation_date": anchor.isoformat(),
                        "market_date": estimate.market_date.isoformat(),
                        "label_end_date": label[
                            "label_end_date"
                        ].isoformat(),
                        "horizon": horizon,
                        "current_price": estimate.current_price,
                        "fair_value": estimate.fair_value,
                        "buy_price": estimate.buy_price,
                        "margin_of_safety": estimate.margin_of_safety,
                        "fair_value_gap": estimate.fair_value_gap,
                        "confidence": estimate.confidence,
                        "forward_return": label["forward_return"],
                        "fair_value_reached": (
                            wealth_high >= float(estimate.fair_value)
                        ),
                        "dominant_expert": max(
                            estimate.gate, key=estimate.gate.get
                        ),
                        "gate": estimate.gate,
                        "market_implied_growth": (
                            estimate.market_implied_growth
                        ),
                        "required_return_policy": (
                            estimate.required_return_policy
                        ),
                        "reverse_dcf": estimate.reverse_dcf,
                        "expert_assumptions": {
                            item.expert_id: {
                                "weight": estimate.gate.get(item.expert_id, 0.0),
                                "low": item.low,
                                "base": item.base,
                                "high": item.high,
                                **item.assumptions,
                            }
                            for item in estimate.experts
                            if item.available
                        },
                        "earnings_yield": (
                            snapshot.earnings_per_share
                            / snapshot.current_price
                            if snapshot.earnings_per_share is not None
                            and snapshot.earnings_per_share > 0
                            else None
                        ),
                        "book_yield": (
                            snapshot.book_value_per_share
                            / snapshot.current_price
                            if snapshot.book_value_per_share is not None
                            and snapshot.book_value_per_share > 0
                            else None
                        ),
                        "dividend_yield": (
                            snapshot.dividend_per_share
                            / snapshot.current_price
                            if snapshot.dividend_per_share is not None
                            and snapshot.dividend_per_share > 0
                            else None
                        ),
                    })
        metrics = {
            str(horizon): self._metrics(rows, horizon)
            for horizon in self.evaluation_config.horizons
        }
        return {
            "contract": "intrinsic-value-walk-forward-1",
            "value_config": asdict(self.value_config),
            "evaluation_config": asdict(self.evaluation_config),
            "dataset": {
                "root": str(self.builder.root.resolve()),
                "market": self.builder.market,
                "symbol_count": len(selected),
                "row_count": len(rows),
                "leakage_violations": leakage_violations,
            },
            "metrics": metrics,
            "rows": rows,
            "acceptance": {
                "no_lookahead": leakage_violations == 0,
                "market_price_not_used_in_cash_flows_or_growth": True,
                "reverse_dcf_is_diagnostic_only": True,
                "subjective_risk_is_explicit": True,
                "production_ready": False,
            },
        }


@dataclass(frozen=True)
class PriceIntervalConfig:
    """Objective and search grid for the next-report price interval."""

    target_coverage: float = 0.80
    width_penalty: float = 0.75
    coverage_shortfall_penalty: float = 2.0
    minimum_dcf_incremental_objective: float = 0.01
    beta_floor: float = 0.35
    minimum_path_days: int = 10
    minimum_calibration_episodes: int = 30
    validation_fraction: float = 0.30
    maximum_center_log_shift: float = 0.70
    valuation_weights: tuple[float, ...] = (
        0.0,
        0.10,
        0.25,
        0.50,
        0.75,
        1.0,
    )
    base_half_widths: tuple[float, ...] = (
        0.02,
        0.04,
        0.06,
        0.08,
        0.12,
        0.16,
        0.22,
    )
    beta_half_widths: tuple[float, ...] = (
        0.0,
        0.02,
        0.04,
        0.06,
        0.08,
        0.12,
    )
    value_uncertainty_weights: tuple[float, ...] = (
        0.0,
        0.05,
        0.10,
        0.20,
    )


@dataclass(frozen=True)
class PriceIntervalParameters:
    valuation_weight: float
    base_half_width: float
    beta_half_width: float
    value_uncertainty_weight: float


@dataclass(frozen=True)
class PriceIntervalForecast:
    symbol: str
    evaluation_date: date
    market_date: date
    current_price: float
    price_lower: float
    price_upper: float
    price_center: float
    expected_coverage_probability: float | None
    relative_width: float
    beta: float
    beta_adjusted_width: float
    objective_score: float | None
    fair_value_low: float
    fair_value: float
    fair_value_high: float
    cost_of_equity: float | None
    required_return: float | None
    required_return_policy: dict[str, Any]
    reverse_dcf: dict[str, dict[str, Any]]
    parameters: PriceIntervalParameters
    risk: SubjectiveRiskAdjustment

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evaluation_date"] = self.evaluation_date.isoformat()
        result["market_date"] = self.market_date.isoformat()
        if self.risk.effective_from is not None:
            result["risk"]["effective_from"] = (
                self.risk.effective_from.isoformat()
            )
        if self.risk.expires_at is not None:
            result["risk"]["expires_at"] = self.risk.expires_at.isoformat()
        result["valid_until"] = "next_new_financial_report"
        return result


class IntrinsicValueIntervalEvaluator:
    """Calibrate narrow price intervals valid until the next report.

    Candidate parameters are selected only on completed report-to-report
    episodes.  A final chronological suffix is held out.  Current forecasts
    are then calibrated on every completed episode, while the reported OOS
    comparison remains frozen.
    """

    def __init__(
        self,
        builder: PointInTimeValuationBuilder,
        value_config: IntrinsicValueConfig | None = None,
        interval_config: PriceIntervalConfig | None = None,
    ):
        self.builder = builder
        self.value_config = value_config or IntrinsicValueConfig()
        self.interval_config = interval_config or PriceIntervalConfig()
        self.engine = IntrinsicValueEngine(self.value_config)

    @staticmethod
    def _report_events(builder: PointInTimeValuationBuilder, symbol: str):
        earliest_by_period: dict[date, date] = {}
        for statement in builder.fundamental_store.read_all(symbol):
            if statement.published_at is None:
                continue
            current = earliest_by_period.get(statement.period_end)
            if current is None or statement.published_at < current:
                earliest_by_period[statement.period_end] = statement.published_at
        return sorted(
            (
                {"period_end": period_end, "published_at": published_at}
                for period_end, published_at in earliest_by_period.items()
            ),
            key=lambda item: (item["published_at"], item["period_end"]),
        )

    @staticmethod
    def _price_path(
        bundle: Any,
        market_date: date,
        next_report_date: date,
        current_price: float,
    ) -> np.ndarray | None:
        dates = pd.to_datetime(bundle.prices["date"]).dt.date.to_numpy()
        start_index = int(
            np.searchsorted(dates, market_date, side="right") - 1
        )
        if start_index < 0:
            return None
        start_qfq = float(bundle.prices.iloc[start_index]["qfq_close"])
        if not np.isfinite(start_qfq) or start_qfq <= 0:
            return None
        selected = (
            (dates > market_date)
            & (dates < next_report_date)
        )
        qfq = pd.to_numeric(
            bundle.prices.loc[selected, "qfq_close"], errors="coerce"
        ).to_numpy(dtype=np.float64)
        path = current_price * qfq / start_qfq
        path = path[np.isfinite(path) & (path > 0)]
        return path if len(path) else None

    def _beta(self, snapshot: Any) -> float:
        capital = snapshot.capital_cost
        if capital is not None:
            for candidate in (capital.adjusted_beta, capital.raw_beta):
                if candidate is not None and np.isfinite(candidate):
                    return float(candidate)
        return float(self.value_config.beta_assumption)

    @staticmethod
    def _historical_risk(
        risk: SubjectiveRiskAdjustment | None,
        evaluation_date: date,
    ) -> SubjectiveRiskAdjustment:
        if risk is None:
            return SubjectiveRiskAdjustment()
        if risk.effective_from is None:
            return SubjectiveRiskAdjustment(
                reason="undated_subjective_risk_excluded_from_history"
            )
        return risk if risk.active_on(evaluation_date) else SubjectiveRiskAdjustment()

    def _episode(
        self,
        symbol: str,
        bundle: Any,
        report_date: date,
        next_report_date: date,
        risk: SubjectiveRiskAdjustment | None,
    ) -> dict[str, Any] | None:
        snapshot = self.builder.snapshot(
            symbol, report_date, self.value_config
        )
        if snapshot is None:
            return None
        selected_risk = self._historical_risk(risk, report_date)
        estimate = self.engine.estimate(snapshot, selected_risk)
        fair_values = (
            estimate.fair_value_low,
            estimate.fair_value,
            estimate.fair_value_high,
        )
        if any(
            value is None or not np.isfinite(value) or value <= 0
            for value in fair_values
        ):
            return None
        path = self._price_path(
            bundle,
            estimate.market_date,
            next_report_date,
            estimate.current_price,
        )
        if path is None or len(path) < self.interval_config.minimum_path_days:
            return None
        capital = snapshot.capital_cost
        return {
            "symbol": symbol,
            "evaluation_date": report_date,
            "market_date": estimate.market_date,
            "next_report_date": next_report_date,
            "current_price": estimate.current_price,
            "fair_value_low": float(estimate.fair_value_low),
            "fair_value": float(estimate.fair_value),
            "fair_value_high": float(estimate.fair_value_high),
            "beta": self._beta(snapshot),
            "cost_of_equity": (
                float(capital.cost_of_equity) if capital is not None else None
            ),
            "required_return_policy": estimate.required_return_policy,
            "reverse_dcf": estimate.reverse_dcf,
            "risk": selected_risk,
            "beta_is_fallback": (
                capital is None
                or capital.adjusted_beta is None
            ),
            "path": path,
        }

    def _episodes(
        self,
        symbols: Iterable[str] | None,
        risks: dict[str, SubjectiveRiskAdjustment],
    ) -> list[dict[str, Any]]:
        episodes = []
        for symbol in self.builder.available_symbols(symbols):
            bundle = self.builder.market_store.read(symbol)
            if bundle is None:
                continue
            events = self._report_events(self.builder, symbol)
            for current, following in zip(events, events[1:]):
                if following["published_at"] <= current["published_at"]:
                    continue
                episode = self._episode(
                    symbol,
                    bundle,
                    current["published_at"],
                    following["published_at"],
                    risks.get(symbol),
                )
                if episode is not None:
                    episodes.append(episode)
        return sorted(
            episodes,
            key=lambda item: (
                item["evaluation_date"],
                item["symbol"],
            ),
        )

    def _forecast_values(
        self,
        episode: dict[str, Any],
        parameters: PriceIntervalParameters,
    ) -> tuple[float, float, float, float, float]:
        current = float(episode["current_price"])
        fair = float(episode["fair_value"])
        fair_low = float(episode["fair_value_low"])
        fair_high = float(episode["fair_value_high"])
        beta = float(episode["beta"])
        risk = episode["risk"]
        log_gap = float(
            np.clip(
                np.log(fair / current),
                -self.interval_config.maximum_center_log_shift,
                self.interval_config.maximum_center_log_shift,
            )
        )
        center = current * np.exp(parameters.valuation_weight * log_gap)
        center *= 1.0 - risk.expected_price_haircut()
        value_uncertainty = float(
            np.clip((fair_high - fair_low) / (2.0 * fair), 0.0, 1.0)
        )
        beta_size = max(abs(beta), self.interval_config.beta_floor)
        half_width = (
            parameters.base_half_width
            + parameters.beta_half_width * beta_size
            + parameters.value_uncertainty_weight * value_uncertainty
        )
        downside_uncertainty = risk.event_uncertainty()
        lower = max(
            0.01,
            center * (1.0 - min(half_width + downside_uncertainty, 0.95)),
        )
        upper = center * (1.0 + half_width)
        relative_width = (upper - lower) / current
        beta_adjusted_width = relative_width / beta_size
        return center, lower, upper, relative_width, beta_adjusted_width

    def _episode_result(
        self,
        episode: dict[str, Any],
        parameters: PriceIntervalParameters,
    ) -> dict[str, Any]:
        center, lower, upper, width, beta_width = self._forecast_values(
            episode, parameters
        )
        path = episode["path"]
        contained = (path >= lower) & (path <= upper)
        coverage = float(np.mean(contained))
        shortfall = max(
            self.interval_config.target_coverage - coverage,
            0.0,
        )
        objective = (
            coverage
            - self.interval_config.width_penalty * beta_width
            - self.interval_config.coverage_shortfall_penalty * shortfall**2
        )
        return {
            "symbol": episode["symbol"],
            "evaluation_date": episode["evaluation_date"].isoformat(),
            "market_date": episode["market_date"].isoformat(),
            "next_report_date": episode["next_report_date"].isoformat(),
            "current_price": episode["current_price"],
            "price_center": center,
            "price_lower": lower,
            "price_upper": upper,
            "relative_width": width,
            "beta": episode["beta"],
            "beta_adjusted_width": beta_width,
            "daily_coverage": coverage,
            "covered_days": int(contained.sum()),
            "path_days": int(len(path)),
            "full_path_covered": bool(contained.all()),
            "objective_score": float(objective),
            "path_min": float(np.min(path)),
            "path_max": float(np.max(path)),
            "fair_value": episode["fair_value"],
            "cost_of_equity": episode["cost_of_equity"],
            "required_return_policy": episode.get(
                "required_return_policy", {}
            ),
            "reverse_dcf": episode.get("reverse_dcf", {}),
            "beta_is_fallback": episode.get("beta_is_fallback", True),
            "risk_expected_haircut": (
                episode["risk"].expected_price_haircut()
            ),
            "risk_event_uncertainty": episode["risk"].event_uncertainty(),
            "risk_reason": episode["risk"].reason,
        }

    def _metrics(
        self,
        episodes: list[dict[str, Any]],
        parameters: PriceIntervalParameters,
        *,
        include_rows: bool = False,
    ) -> dict[str, Any]:
        rows = [self._episode_result(item, parameters) for item in episodes]
        if not rows:
            return {
                "episode_count": 0,
                "daily_observation_count": 0,
                "mean_daily_coverage": None,
                "pooled_daily_coverage": None,
                "full_path_coverage": None,
                "mean_relative_width": None,
                "mean_beta_adjusted_width": None,
                "mean_objective": None,
                "rows": [] if include_rows else None,
            }
        covered = sum(item["covered_days"] for item in rows)
        days = sum(item["path_days"] for item in rows)
        result = {
            "episode_count": len(rows),
            "daily_observation_count": days,
            "mean_daily_coverage": float(np.mean([
                item["daily_coverage"] for item in rows
            ])),
            "pooled_daily_coverage": covered / days,
            "full_path_coverage": float(np.mean([
                item["full_path_covered"] for item in rows
            ])),
            "mean_relative_width": float(np.mean([
                item["relative_width"] for item in rows
            ])),
            "mean_beta_adjusted_width": float(np.mean([
                item["beta_adjusted_width"] for item in rows
            ])),
            "mean_objective": float(np.mean([
                item["objective_score"] for item in rows
            ])),
        }
        if include_rows:
            result["rows"] = rows
        return result

    def _parameter_grid(self, *, baseline: bool):
        valuation_weights = (
            (0.0,) if baseline else self.interval_config.valuation_weights
        )
        uncertainty_weights = (
            (0.0,)
            if baseline
            else self.interval_config.value_uncertainty_weights
        )
        for valuation_weight in valuation_weights:
            for base_half_width in self.interval_config.base_half_widths:
                for beta_half_width in self.interval_config.beta_half_widths:
                    for uncertainty_weight in uncertainty_weights:
                        yield PriceIntervalParameters(
                            valuation_weight=valuation_weight,
                            base_half_width=base_half_width,
                            beta_half_width=beta_half_width,
                            value_uncertainty_weight=uncertainty_weight,
                        )

    def _select_parameters(
        self,
        episodes: list[dict[str, Any]],
        *,
        baseline: bool,
    ) -> tuple[PriceIntervalParameters, dict[str, Any]]:
        best_parameters = None
        best_metrics = None
        best_key = None
        for parameters in self._parameter_grid(baseline=baseline):
            metrics = self._metrics(episodes, parameters)
            key = (
                metrics["mean_objective"],
                metrics["mean_daily_coverage"],
                -metrics["mean_relative_width"],
                -parameters.valuation_weight,
            )
            if best_key is None or key > best_key:
                best_key = key
                best_parameters = parameters
                best_metrics = metrics
        if best_parameters is None or best_metrics is None:
            raise ValueError("no completed episodes for interval calibration")
        return best_parameters, best_metrics

    @staticmethod
    def _split_episodes(
        episodes: list[dict[str, Any]], validation_fraction: float
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        dates = sorted({item["evaluation_date"] for item in episodes})
        if len(dates) < 2:
            return episodes, [], None
        validation_count = max(1, int(np.ceil(len(dates) * validation_fraction)))
        validation_dates = set(dates[-validation_count:])
        calibration = [
            item for item in episodes
            if item["evaluation_date"] not in validation_dates
        ]
        validation = [
            item for item in episodes
            if item["evaluation_date"] in validation_dates
        ]
        cutoff = min(validation_dates).isoformat()
        return calibration, validation, cutoff

    def _latest_forecasts(
        self,
        parameters: PriceIntervalParameters,
        expected_coverage: float | None,
        symbols: Iterable[str] | None,
        risks: dict[str, SubjectiveRiskAdjustment],
    ) -> list[dict[str, Any]]:
        anchor = self.builder.latest_date(symbols)
        forecasts = []
        for symbol in self.builder.available_symbols(symbols):
            snapshot = self.builder.snapshot(
                symbol, anchor, self.value_config
            )
            if snapshot is None:
                continue
            estimate = self.engine.estimate(snapshot, risks.get(symbol))
            if any(
                value is None or not np.isfinite(value) or value <= 0
                for value in (
                    estimate.fair_value_low,
                    estimate.fair_value,
                    estimate.fair_value_high,
                )
            ):
                continue
            episode = {
                "current_price": estimate.current_price,
                "fair_value_low": float(estimate.fair_value_low),
                "fair_value": float(estimate.fair_value),
                "fair_value_high": float(estimate.fair_value_high),
                "beta": self._beta(snapshot),
                "risk": estimate.risk,
            }
            center, lower, upper, width, beta_width = self._forecast_values(
                episode, parameters
            )
            capital = snapshot.capital_cost
            applied_return = estimate.required_return_policy.get(
                "applied_required_return", {}
            )
            forecast = PriceIntervalForecast(
                symbol=symbol,
                evaluation_date=anchor,
                market_date=estimate.market_date,
                current_price=estimate.current_price,
                price_lower=lower,
                price_upper=upper,
                price_center=center,
                expected_coverage_probability=expected_coverage,
                relative_width=width,
                beta=episode["beta"],
                beta_adjusted_width=beta_width,
                objective_score=None,
                fair_value_low=float(estimate.fair_value_low),
                fair_value=float(estimate.fair_value),
                fair_value_high=float(estimate.fair_value_high),
                cost_of_equity=(
                    float(capital.cost_of_equity)
                    if capital is not None
                    else None
                ),
                required_return=(
                    float(applied_return["base"])
                    if applied_return.get("base") is not None
                    else None
                ),
                required_return_policy=estimate.required_return_policy,
                reverse_dcf=estimate.reverse_dcf,
                parameters=parameters,
                risk=estimate.risk,
            )
            forecasts.append(forecast.to_dict())
        return forecasts

    def run(
        self,
        *,
        symbols: Iterable[str] | None = None,
        risks: dict[str, SubjectiveRiskAdjustment] | None = None,
    ) -> dict[str, Any]:
        selected_risks = risks or {}
        episodes = self._episodes(symbols, selected_risks)
        if len(episodes) < self.interval_config.minimum_calibration_episodes:
            raise ValueError(
                "insufficient completed report-to-report episodes: "
                f"{len(episodes)} < "
                f"{self.interval_config.minimum_calibration_episodes}"
            )
        calibration, validation, validation_start = self._split_episodes(
            episodes, self.interval_config.validation_fraction
        )
        dcf_parameters, calibration_dcf = self._select_parameters(
            calibration, baseline=False
        )
        baseline_parameters, calibration_baseline = self._select_parameters(
            calibration, baseline=True
        )
        validation_dcf = self._metrics(
            validation, dcf_parameters, include_rows=True
        )
        validation_baseline = self._metrics(
            validation, baseline_parameters, include_rows=True
        )
        deployment_parameters, all_history_dcf = self._select_parameters(
            episodes, baseline=False
        )
        expected_coverage = validation_dcf["mean_daily_coverage"]
        forecasts = self._latest_forecasts(
            deployment_parameters,
            expected_coverage,
            symbols,
            selected_risks,
        )
        dcf_increment = (
            validation_dcf["mean_objective"]
            - validation_baseline["mean_objective"]
            if validation_dcf["mean_objective"] is not None
            and validation_baseline["mean_objective"] is not None
            else None
        )
        return {
            "contract": "next-report-price-interval-1",
            "objective": (
                "maximize mean_daily_coverage - width_penalty * "
                "relative_width / max(abs(beta), beta_floor) - "
                "coverage_shortfall_penalty * max(target-coverage, 0)^2"
            ),
            "value_config": asdict(self.value_config),
            "interval_config": asdict(self.interval_config),
            "dataset": {
                "root": str(self.builder.root.resolve()),
                "market": self.builder.market,
                "completed_episode_count": len(episodes),
                "estimated_beta_episode_count": sum(
                    not item.get("beta_is_fallback", True)
                    for item in episodes
                ),
                "beta_summary": {
                    "minimum": float(np.min([
                        item["beta"] for item in episodes
                    ])),
                    "median": float(np.median([
                        item["beta"] for item in episodes
                    ])),
                    "maximum": float(np.max([
                        item["beta"] for item in episodes
                    ])),
                },
                "calibration_episode_count": len(calibration),
                "validation_episode_count": len(validation),
                "validation_start": validation_start,
            },
            "calibration": {
                "dcf_parameters": asdict(dcf_parameters),
                "dcf_metrics": calibration_dcf,
                "baseline_parameters": asdict(baseline_parameters),
                "baseline_metrics": calibration_baseline,
            },
            "validation": {
                "dcf_parameters": asdict(dcf_parameters),
                "dcf_metrics": validation_dcf,
                "baseline_parameters": asdict(baseline_parameters),
                "baseline_metrics": validation_baseline,
                "dcf_incremental_objective": dcf_increment,
            },
            "deployment": {
                "parameters": asdict(deployment_parameters),
                "all_history_metrics": all_history_dcf,
                "latest_forecasts": forecasts,
            },
            "acceptance": {
                "no_lookahead": True,
                "held_out_validation": bool(validation),
                "subjective_risk_requires_effective_from_for_history": True,
                "equity_cash_flow_only": (
                    self.value_config.equity_cash_flow_only
                ),
                "investor_required_return_is_explicit": True,
                "market_capm_is_diagnostic_and_floor_only": (
                    self.value_config.market_cost_of_equity_floor
                ),
                "reverse_dcf_is_diagnostic_only": True,
                "dcf_contributes_to_selected_interval": (
                    dcf_parameters.valuation_weight > 0.0
                    or dcf_parameters.value_uncertainty_weight > 0.0
                ),
                "dcf_beats_beta_only_baseline": (
                    dcf_increment is not None
                    and dcf_increment
                    >= self.interval_config.minimum_dcf_incremental_objective
                    and (
                        dcf_parameters.valuation_weight > 0.0
                        or dcf_parameters.value_uncertainty_weight > 0.0
                    )
                ),
                "validation_coverage_meets_target": (
                    validation_dcf["mean_daily_coverage"] is not None
                    and validation_dcf["mean_daily_coverage"]
                    >= self.interval_config.target_coverage
                ),
                "production_ready": False,
            },
        }
