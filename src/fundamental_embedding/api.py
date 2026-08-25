"""Stable contracts for fundamental-pricing embeddings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np


PRICING_FEATURE_NAMES = (
    "earnings_yield",
    "book_yield",
    "fcf_yield",
    "dividend_yield",
    "roe_ttm",
    "net_margin",
    "adjusted_margin",
    "fcf_margin",
    "cash_conversion",
    "capex_intensity",
    "revenue_yoy",
    "revenue_qoq",
    "net_income_yoy",
    "net_income_qoq",
    "revenue_cagr_3y",
    "net_income_cagr_3y",
    "revenue_growth_stability",
    "income_growth_stability",
    "financial_age_days",
)


EXPERT_FEATURES = {
    "value": (
        "earnings_yield",
        "book_yield",
        "fcf_yield",
        "dividend_yield",
    ),
    "cash_return": (
        "fcf_yield",
        "dividend_yield",
        "fcf_margin",
        "cash_conversion",
        "capex_intensity",
    ),
    "quality": (
        "roe_ttm",
        "net_margin",
        "adjusted_margin",
        "fcf_margin",
        "revenue_growth_stability",
        "income_growth_stability",
    ),
    "growth": (
        "revenue_yoy",
        "revenue_qoq",
        "net_income_yoy",
        "net_income_qoq",
        "revenue_cagr_3y",
        "net_income_cagr_3y",
    ),
}


@dataclass(frozen=True)
class FundamentalPricingDataset:
    """Flattened company-quarter observations with causal forward labels."""

    feature_dates: tuple[date, ...]
    label_end_dates: tuple[date, ...]
    symbols: tuple[str, ...]
    feature_names: tuple[str, ...]
    values: np.ndarray
    availability_mask: np.ndarray
    forward_returns: np.ndarray
    excess_returns: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "FundamentalPricingDataset":
        rows = len(self.symbols)
        if not (
            len(self.feature_dates)
            == len(self.label_end_dates)
            == len(self.forward_returns)
            == len(self.excess_returns)
            == rows
        ):
            raise ValueError("fundamental dataset row metadata is misaligned")
        expected = (rows, len(self.feature_names))
        if self.values.shape != expected or self.availability_mask.shape != expected:
            raise ValueError("fundamental feature matrix has an invalid shape")
        if self.availability_mask.dtype != bool:
            raise ValueError("availability_mask must be boolean")
        if any(end <= start for start, end in zip(
            self.feature_dates, self.label_end_dates
        )):
            raise ValueError("every forward label must end after its feature date")
        return self

    def rows_before(self, cutoff: date) -> np.ndarray:
        """Only labels fully realized before a prediction date are trainable."""
        return np.asarray([end < cutoff for end in self.label_end_dates], dtype=bool)

    def rows_on(self, feature_date: date) -> np.ndarray:
        return np.asarray(
            [item == feature_date for item in self.feature_dates], dtype=bool
        )


@dataclass(frozen=True)
class FundamentalPricingSnapshot:
    """Unlabelled point-in-time rows used to produce current embeddings."""

    feature_date: date
    symbols: tuple[str, ...]
    feature_names: tuple[str, ...]
    values: np.ndarray
    availability_mask: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "FundamentalPricingSnapshot":
        expected = (len(self.symbols), len(self.feature_names))
        if self.values.shape != expected or self.availability_mask.shape != expected:
            raise ValueError("fundamental snapshot matrix has an invalid shape")
        if self.availability_mask.dtype != bool:
            raise ValueError("availability_mask must be boolean")
        return self


@dataclass(frozen=True)
class MoEConfig:
    ridge_alpha: float = 8.0
    gate_temperature: float = 0.35
    gate_floor: float = 0.05
    gate_validation_fraction: float = 0.30
    gate_half_life_quarters: float = 4.0
    embedding_smoothing_alpha: float = 0.35
    minimum_train_rows: int = 48
    minimum_train_dates: int = 8
    winsor_limit: float = 4.0


@dataclass(frozen=True)
class EmbeddingEvaluation:
    contract: str
    dataset: dict[str, Any]
    config: dict[str, Any]
    metrics: dict[str, Any]
    baselines: dict[str, Any]
    stability: dict[str, Any]
    expert_diagnostics: dict[str, Any]
    feature_coverage: dict[str, Any]
    predictions: list[dict[str, Any]]
    latest_embeddings: list[dict[str, Any]]
    acceptance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "dataset": self.dataset,
            "config": self.config,
            "metrics": self.metrics,
            "baselines": self.baselines,
            "stability": self.stability,
            "expert_diagnostics": self.expert_diagnostics,
            "feature_coverage": self.feature_coverage,
            "predictions": self.predictions,
            "latest_embeddings": self.latest_embeddings,
            "acceptance": self.acceptance,
        }
