"""Signed, causal market pricing models for stable company factors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Callable

import numpy as np
from scipy.stats import rankdata

from .split_api import (
    CompanyExposureBatch,
    MarketPricingState,
    SplitPricingConfig,
)


@dataclass(frozen=True)
class FactorPriceHistory:
    """Per-quarter factor prices estimated only after labels are realized."""

    dates: tuple[date, ...]
    factor_names: tuple[str, ...]
    prices: np.ndarray
    uncertainty: np.ndarray

    def validate(self) -> "FactorPriceHistory":
        expected = (len(self.dates), len(self.factor_names))
        if self.prices.shape != expected or self.uncertainty.shape != expected:
            raise ValueError("factor price history has an invalid shape")
        if not np.all(np.isfinite(self.prices)):
            raise ValueError("factor price observations must be finite")
        if np.any(self.uncertainty < 0.0) or not np.all(
            np.isfinite(self.uncertainty)
        ):
            raise ValueError("factor price uncertainty is invalid")
        return self


def _centered_rank(values: np.ndarray) -> np.ndarray:
    if len(values) <= 1:
        return np.zeros(len(values), dtype=np.float64)
    ranks = rankdata(values, method="average")
    return (ranks - (len(values) + 1.0) / 2.0) / (
        (len(values) - 1.0) / 2.0
    )


def estimate_realized_factor_prices(
    exposures: CompanyExposureBatch,
    target: np.ndarray,
    ridge_alpha: float,
) -> FactorPriceHistory:
    """Estimate signed factor prices with a quarter-balanced rank target."""

    date_array = np.asarray(exposures.feature_dates, dtype=object)
    dates = []
    prices = []
    uncertainty = []
    for current_date in sorted(set(exposures.feature_dates)):
        selected = date_array == current_date
        x = exposures.ranking_exposures[selected]
        y = _centered_rank(np.asarray(target[selected], dtype=np.float64))
        if len(y) < max(3, x.shape[1] + 1):
            continue
        penalty = np.eye(x.shape[1], dtype=np.float64) * float(ridge_alpha)
        matrix = x.T @ x + penalty
        inverse = np.linalg.pinv(matrix)
        coefficient = inverse @ x.T @ y
        residual = y - x @ coefficient
        residual_variance = max(float(np.mean(residual**2)), 1e-8)
        standard_error = np.sqrt(
            np.maximum(np.diag(inverse) * residual_variance, 0.0)
        )
        dates.append(current_date)
        prices.append(coefficient)
        uncertainty.append(standard_error)
    if not dates:
        raise ValueError("no realized factor-price quarters are available")
    return FactorPriceHistory(
        dates=tuple(dates),
        factor_names=exposures.factor_names,
        prices=np.asarray(prices, dtype=np.float64),
        uncertainty=np.asarray(uncertainty, dtype=np.float64),
    ).validate()


class MarketPricingModel(ABC):
    """Plugin contract for forecasting the next signed factor-price state."""

    model_id: str

    @abstractmethod
    def forecast(
        self,
        history: FactorPriceHistory,
        as_of: date,
    ) -> MarketPricingState:
        raise NotImplementedError

    @staticmethod
    def _state(
        history: FactorPriceHistory,
        as_of: date,
        model_id: str,
        prices: np.ndarray,
        uncertainty: np.ndarray,
        metadata: dict | None = None,
    ) -> MarketPricingState:
        return MarketPricingState(
            as_of=as_of,
            realized_through=max(history.dates),
            model_id=model_id,
            factor_names=history.factor_names,
            factor_prices=np.asarray(prices, dtype=np.float64),
            uncertainty=np.asarray(uncertainty, dtype=np.float64),
            metadata=metadata or {},
        ).validate()


_MODEL_FACTORIES: dict[str, Callable[[SplitPricingConfig], MarketPricingModel]] = {}


def register_pricing_model(model_id: str):
    def decorator(factory):
        if model_id in _MODEL_FACTORIES:
            raise ValueError(f"duplicate market pricing model: {model_id}")
        _MODEL_FACTORIES[model_id] = factory
        return factory

    return decorator


def create_pricing_models(
    config: SplitPricingConfig,
) -> dict[str, MarketPricingModel]:
    return {
        model_id: factory(config)
        for model_id, factory in _MODEL_FACTORIES.items()
    }


@register_pricing_model("static_factor_price")
class StaticFactorPricingModel(MarketPricingModel):
    model_id = "static_factor_price"

    def __init__(self, config: SplitPricingConfig):
        self.config = config

    def forecast(
        self,
        history: FactorPriceHistory,
        as_of: date,
    ) -> MarketPricingState:
        prices = np.mean(history.prices, axis=0)
        if len(history.prices) <= 1:
            uncertainty = history.uncertainty[-1]
        else:
            uncertainty = np.std(
                history.prices,
                axis=0,
                ddof=1,
            ) / np.sqrt(len(history.prices))
        return self._state(
            history,
            as_of,
            self.model_id,
            prices,
            uncertainty,
            {"quarter_weighting": "equal"},
        )


@register_pricing_model("ewma_factor_price")
class EwmaFactorPricingModel(MarketPricingModel):
    model_id = "ewma_factor_price"

    def __init__(self, config: SplitPricingConfig):
        self.config = config

    def forecast(
        self,
        history: FactorPriceHistory,
        as_of: date,
    ) -> MarketPricingState:
        ages = np.arange(len(history.prices) - 1, -1, -1, dtype=np.float64)
        weights = np.power(
            0.5,
            ages / max(self.config.ewma_half_life_quarters, 1e-6),
        )
        weights /= weights.sum()
        prices = np.sum(history.prices * weights[:, None], axis=0)
        uncertainty = np.sqrt(np.sum(
            weights[:, None] * (history.prices - prices) ** 2,
            axis=0,
        ))
        return self._state(
            history,
            as_of,
            self.model_id,
            prices,
            uncertainty,
            {
                "half_life_quarters": (
                    self.config.ewma_half_life_quarters
                )
            },
        )


@register_pricing_model("kalman_factor_price")
class KalmanFactorPricingModel(MarketPricingModel):
    model_id = "kalman_factor_price"

    def __init__(self, config: SplitPricingConfig):
        self.config = config

    def forecast(
        self,
        history: FactorPriceHistory,
        as_of: date,
    ) -> MarketPricingState:
        observations = history.prices
        base_variance = np.maximum(
            np.var(observations, axis=0),
            self.config.kalman_min_variance,
        )
        process_variance = np.maximum(
            base_variance * self.config.kalman_process_variance_ratio,
            self.config.kalman_min_variance,
        )
        state = observations[0].copy()
        state_variance = np.maximum(
            history.uncertainty[0] ** 2,
            base_variance,
        )
        for observation, observed_error in zip(
            observations[1:],
            history.uncertainty[1:],
        ):
            predicted_variance = state_variance + process_variance
            observation_variance = np.maximum(
                observed_error**2,
                base_variance,
            )
            gain = predicted_variance / (
                predicted_variance + observation_variance
            )
            state = state + gain * (observation - state)
            state_variance = (1.0 - gain) * predicted_variance
        forecast_variance = state_variance + process_variance
        return self._state(
            history,
            as_of,
            self.model_id,
            state,
            np.sqrt(np.maximum(forecast_variance, 0.0)),
            {
                "process_variance_ratio": (
                    self.config.kalman_process_variance_ratio
                ),
                "signed_prices": True,
                "simplex_constraint": False,
            },
        )
