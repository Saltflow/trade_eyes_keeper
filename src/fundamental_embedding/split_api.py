"""Contracts for separated company exposures and market pricing states."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np


FACTOR_FEATURE_DIRECTIONS = {
    "value": {
        "earnings_yield": 1.0,
        "book_yield": 1.0,
        "fcf_yield": 1.0,
        "dividend_yield": 1.0,
    },
    "cash_return": {
        "fcf_yield": 1.0,
        "dividend_yield": 1.0,
        "fcf_margin": 1.0,
        "cash_conversion": 1.0,
        "capex_intensity": -1.0,
    },
    "quality": {
        "roe_ttm": 1.0,
        "net_margin": 1.0,
        "adjusted_margin": 1.0,
        "fcf_margin": 1.0,
        "revenue_growth_stability": 1.0,
        "income_growth_stability": 1.0,
    },
    "growth": {
        "revenue_yoy": 1.0,
        "revenue_qoq": 1.0,
        "net_income_yoy": 1.0,
        "net_income_qoq": 1.0,
        "revenue_cagr_3y": 1.0,
        "net_income_cagr_3y": 1.0,
    },
}

FACTOR_NAMES = tuple(FACTOR_FEATURE_DIRECTIONS)
APPROVED_PRICING_FEATURES = tuple(dict.fromkeys(
    feature
    for mapping in FACTOR_FEATURE_DIRECTIONS.values()
    for feature in mapping
))


@dataclass(frozen=True)
class SplitPricingConfig:
    """Configuration for the separated exposure/pricing experiment."""

    factor_ridge_alpha: float = 4.0
    raw_ridge_alpha: float = 8.0
    winsor_limit: float = 4.0
    exposure_smoothing_alpha: float = 0.35
    stale_after_days: float = 180.0
    freshness_half_life_days: float = 730.0
    ewma_half_life_quarters: float = 4.0
    kalman_process_variance_ratio: float = 0.15
    kalman_min_variance: float = 1e-4
    minimum_train_rows: int = 48
    minimum_train_dates: int = 8
    candidate_model_id: str = "kalman_factor_price"
    stability_penalty: float = 0.25
    turnover_penalty: float = 0.05
    production_minimum_symbols: int = 100
    production_minimum_rank_ic: float = 0.03
    production_minimum_delta_rank_ic: float = 0.01
    production_minimum_win_rate: float = 0.55
    mandatory_baselines: tuple[str, ...] = (
        "zero_score",
        "uniform_factor_price",
        "static_factor_price",
        "ewma_factor_price",
        "single_rank_ridge",
        "single_return_ridge",
        "quality_growth_static",
        "legacy_recent_mse_gate",
    )


@dataclass(frozen=True)
class CompanyExposureBatch:
    """Stable-semantics company factors, separate from market pricing."""

    feature_dates: tuple[date, ...]
    symbols: tuple[str, ...]
    factor_names: tuple[str, ...]
    raw_exposures: np.ndarray
    ranking_exposures: np.ndarray
    availability_confidence: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "CompanyExposureBatch":
        expected = (len(self.symbols), len(self.factor_names))
        if not (
            len(self.feature_dates) == len(self.symbols)
            and self.raw_exposures.shape == expected
            and self.ranking_exposures.shape == expected
            and self.availability_confidence.shape == expected
        ):
            raise ValueError("company exposure batch is misaligned")
        if not np.all(np.isfinite(self.raw_exposures)):
            raise ValueError("raw company exposures must be finite")
        if not np.all(np.isfinite(self.ranking_exposures)):
            raise ValueError("ranking company exposures must be finite")
        if np.any(self.availability_confidence < 0.0) or np.any(
            self.availability_confidence > 1.0
        ):
            raise ValueError("exposure confidence must be in [0, 1]")
        return self


@dataclass(frozen=True)
class MarketPricingState:
    """Signed market prices for stable company factors."""

    as_of: date
    realized_through: date
    model_id: str
    factor_names: tuple[str, ...]
    factor_prices: np.ndarray
    uncertainty: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "MarketPricingState":
        expected = (len(self.factor_names),)
        if (
            self.factor_prices.shape != expected
            or self.uncertainty.shape != expected
        ):
            raise ValueError("market pricing state has an invalid shape")
        if self.realized_through >= self.as_of:
            raise ValueError("market pricing state used an unrealized period")
        if not np.all(np.isfinite(self.factor_prices)):
            raise ValueError("factor prices must be finite")
        if np.any(self.uncertainty < 0.0) or not np.all(
            np.isfinite(self.uncertainty)
        ):
            raise ValueError("factor price uncertainty must be finite and nonnegative")
        return self


@dataclass(frozen=True)
class SplitPricingEvaluation:
    """Walk-forward ranking report with mandatory paired baselines."""

    contract: str
    dataset: dict[str, Any]
    config: dict[str, Any]
    candidate_model_id: str
    metrics: dict[str, Any]
    baselines: dict[str, Any]
    paired_comparison: dict[str, Any]
    stability: dict[str, Any]
    exposure_coverage: dict[str, Any]
    factor_price_states: list[dict[str, Any]]
    predictions: list[dict[str, Any]]
    latest_company_exposures: list[dict[str, Any]]
    acceptance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "dataset": self.dataset,
            "config": self.config,
            "candidate_model_id": self.candidate_model_id,
            "metrics": self.metrics,
            "baselines": self.baselines,
            "paired_comparison": self.paired_comparison,
            "stability": self.stability,
            "exposure_coverage": self.exposure_coverage,
            "factor_price_states": self.factor_price_states,
            "predictions": self.predictions,
            "latest_company_exposures": self.latest_company_exposures,
            "acceptance": self.acceptance,
        }
