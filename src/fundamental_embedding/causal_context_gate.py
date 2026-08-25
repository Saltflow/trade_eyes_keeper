"""Training-only gate between company fundamentals and valuation context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .exposure import RobustFeatureTransformer

CAUSAL_CONTEXT_GATE_CONTRACT = "causal-fundamental-context-gate-1"


@dataclass(frozen=True)
class CausalContextGateConfig:
    ridge_alpha: float = 8.0
    gate_temperature: float = 0.35
    gate_floor: float = 0.05
    validation_fraction: float = 0.25
    minimum_validation_dates: int = 2

    def validate(self) -> CausalContextGateConfig:
        if self.ridge_alpha <= 0:
            raise ValueError("ridge_alpha must be positive")
        if self.gate_temperature <= 0:
            raise ValueError("gate_temperature must be positive")
        if not 0.0 < self.gate_floor < 0.5:
            raise ValueError("gate_floor must be between zero and one half")
        if not 0.0 < self.validation_fraction < 0.5:
            raise ValueError("validation_fraction must be in (0, 0.5)")
        if self.minimum_validation_dates < 1:
            raise ValueError("minimum_validation_dates must be positive")
        return self


def _fit_ridge(values: np.ndarray, target: np.ndarray, alpha: float) -> np.ndarray:
    penalty = np.eye(values.shape[1], dtype=np.float64) * float(alpha)
    return np.linalg.pinv(values.T @ values + penalty) @ values.T @ target


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values)
    weights = np.exp(np.clip(shifted, -50.0, 50.0))
    total = float(weights.sum())
    return weights / total if total > 0 else np.full(len(values), 1.0 / len(values))


class CausalContextGate:
    """Two-expert gate whose weight is frozen before the OOS date."""

    expert_names = ("fundamental", "valuation_context")

    def __init__(self, config: CausalContextGateConfig | None = None):
        self.config = (config or CausalContextGateConfig()).validate()
        self.base_transformer: RobustFeatureTransformer | None = None
        self.context_transformer: RobustFeatureTransformer | None = None
        self.base_coefficient: np.ndarray | None = None
        self.context_coefficient: np.ndarray | None = None
        self.gate_weights = np.full(2, 0.5, dtype=np.float64)
        self.gate_losses = np.full(2, np.nan, dtype=np.float64)
        self.gate_training_dates: tuple[str, ...] = ()

    @staticmethod
    def _predict(values: np.ndarray, coefficient: np.ndarray) -> np.ndarray:
        return values @ coefficient

    def _prepare(
        self,
        base_values: np.ndarray,
        base_mask: np.ndarray,
        context_values: np.ndarray,
        context_mask: np.ndarray,
        base_fit: np.ndarray,
        context_fit: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        self.base_transformer = RobustFeatureTransformer(
            tuple(f"base:{i}" for i in range(base_values.shape[1]))
        ).fit(base_values[base_fit], base_mask[base_fit])
        self.context_transformer = RobustFeatureTransformer(
            tuple(f"context:{i}" for i in range(context_values.shape[1]))
        ).fit(context_values[context_fit], context_mask[context_fit])
        return (
            self.base_transformer.transform(base_values, base_mask),
            self.context_transformer.transform(context_values, context_mask),
            base_fit,
            context_fit,
        )

    def fit(
        self,
        base_values: np.ndarray,
        base_mask: np.ndarray,
        context_values: np.ndarray,
        context_mask: np.ndarray,
        target: np.ndarray,
        feature_dates: np.ndarray,
    ) -> CausalContextGate:
        base_values = np.asarray(base_values, dtype=np.float64)
        context_values = np.asarray(context_values, dtype=np.float64)
        base_mask = np.asarray(base_mask, dtype=bool)
        context_mask = np.asarray(context_mask, dtype=bool)
        target = np.asarray(target, dtype=np.float64)
        dates = np.asarray(feature_dates, dtype=object)
        if len(target) != len(base_values) or context_values.shape[0] != len(target):
            raise ValueError("expert training rows are misaligned")
        unique_dates = sorted(set(dates))
        validation_count = max(
            self.config.minimum_validation_dates,
            int(np.ceil(len(unique_dates) * self.config.validation_fraction)),
        )
        if len(unique_dates) - validation_count < 2:
            validation_count = 0
        validation_dates = tuple(unique_dates[-validation_count:]) if validation_count else ()
        validation = np.isin(dates, validation_dates)
        fit = ~validation
        base_x, context_x, _, _ = self._prepare(
            base_values, base_mask, context_values, context_mask, fit, fit
        )
        if validation_count:
            base_coef = _fit_ridge(base_x[fit], target[fit], self.config.ridge_alpha)
            context_coef = _fit_ridge(context_x[fit], target[fit], self.config.ridge_alpha)
            validation_predictions = np.column_stack((
                self._predict(base_x[validation], base_coef),
                self._predict(context_x[validation], context_coef),
            ))
            losses = np.mean(np.square(validation_predictions - target[validation, None]), axis=0)
            variance = max(float(np.var(target[validation])), 1e-8)
            self.gate_losses = losses / variance
            weights = _softmax(-self.gate_losses / self.config.gate_temperature)
            weights = np.maximum(weights, self.config.gate_floor)
            self.gate_weights = weights / weights.sum()
        self.gate_training_dates = tuple(item.isoformat() for item in validation_dates)
        # Freeze gate weights, then refit both experts on every realized label.
        self.base_coefficient = _fit_ridge(base_x, target, self.config.ridge_alpha)
        self.context_coefficient = _fit_ridge(context_x, target, self.config.ridge_alpha)
        return self

    def predict(
        self,
        base_values: np.ndarray,
        base_mask: np.ndarray,
        context_values: np.ndarray,
        context_mask: np.ndarray,
    ) -> dict[str, np.ndarray]:
        if self.base_transformer is None or self.context_transformer is None:
            raise RuntimeError("gate is not fitted")
        if self.base_coefficient is None or self.context_coefficient is None:
            raise RuntimeError("gate coefficients are not fitted")
        base_x = self.base_transformer.transform(base_values, base_mask)
        context_x = self.context_transformer.transform(context_values, context_mask)
        base = self._predict(base_x, self.base_coefficient)
        context = self._predict(context_x, self.context_coefficient)
        return {
            "fundamental": base,
            "valuation_context": context,
            "gated": self.gate_weights[0] * base + self.gate_weights[1] * context,
            "gate_weights": np.repeat(self.gate_weights[None, :], len(base), axis=0),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "contract": CAUSAL_CONTEXT_GATE_CONTRACT,
            "configuration": asdict(self.config),
            "expert_names": self.expert_names,
            "gate_weights": self.gate_weights.tolist(),
            "gate_losses": self.gate_losses.tolist(),
            "gate_validation_dates": list(self.gate_training_dates),
            "gate_uses_only_training_labels": True,
        }
