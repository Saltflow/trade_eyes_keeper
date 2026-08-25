"""Rank-objective variant of the causal valuation-context gate."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.stats import spearmanr

from .causal_context_gate import (
    CAUSAL_CONTEXT_GATE_CONTRACT,
    CausalContextGate,
)

CAUSAL_RANK_CONTEXT_GATE_CONTRACT = "causal-rank-context-gate-1"


def _mean_rank_ic(
    predictions: np.ndarray,
    target: np.ndarray,
    dates: np.ndarray,
) -> float | None:
    values: list[float] = []
    for current_date in sorted(set(dates)):
        selected = dates == current_date
        if int(selected.sum()) < 3:
            continue
        if np.std(predictions[selected]) <= 1e-12 or np.std(target[selected]) <= 1e-12:
            continue
        value = spearmanr(predictions[selected], target[selected]).statistic
        if value is not None and np.isfinite(value):
            values.append(float(value))
    return float(np.mean(values)) if values else None


class CausalRankContextGate(CausalContextGate):
    """Freeze expert weights using only validation-period cross-sectional Rank IC."""

    def fit(
        self,
        base_values: np.ndarray,
        base_mask: np.ndarray,
        context_values: np.ndarray,
        context_mask: np.ndarray,
        target: np.ndarray,
        feature_dates: np.ndarray,
    ) -> CausalRankContextGate:
        base_values = np.asarray(base_values, dtype=np.float64)
        context_values = np.asarray(context_values, dtype=np.float64)
        base_mask = np.asarray(base_mask, dtype=bool)
        context_mask = np.asarray(context_mask, dtype=bool)
        target = np.asarray(target, dtype=np.float64)
        dates = np.asarray(feature_dates, dtype=object)
        if len(target) != len(base_values) or len(target) != len(context_values):
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
            base_coef = self._fit_for_gate(base_x[fit], target[fit])
            context_coef = self._fit_for_gate(context_x[fit], target[fit])
            validation_predictions = np.column_stack((
                self._predict(validation_values=base_x[validation], coefficient=base_coef),
                self._predict(validation_values=context_x[validation], coefficient=context_coef),
            ))
            scores = np.asarray([
                _mean_rank_ic(validation_predictions[:, index], target[validation], dates[validation])
                for index in range(2)
            ], dtype=np.float64)
            scores = np.where(np.isfinite(scores), scores, -1.0)
            self.gate_losses = -scores
            weights = np.exp(np.clip(scores / self.config.gate_temperature, -50.0, 50.0))
            weights /= max(float(weights.sum()), 1e-12)
            weights = np.maximum(weights, self.config.gate_floor)
            self.gate_weights = weights / weights.sum()
        self.gate_training_dates = tuple(item.isoformat() for item in validation_dates)
        self.base_coefficient = self._fit_for_gate(base_x, target)
        self.context_coefficient = self._fit_for_gate(context_x, target)
        return self

    def _fit_for_gate(self, values: np.ndarray, target: np.ndarray) -> np.ndarray:
        from .causal_context_gate import _fit_ridge

        return _fit_ridge(values, target, self.config.ridge_alpha)

    def _predict(self, validation_values: np.ndarray, coefficient: np.ndarray) -> np.ndarray:
        return validation_values @ coefficient

    def diagnostics(self) -> dict[str, Any]:
        base = super().diagnostics()
        base.update({
            "contract": CAUSAL_RANK_CONTEXT_GATE_CONTRACT,
            "gate_objective": "mean_validation_cross_sectional_rank_ic",
            "mse_gate_contract_not_used": CAUSAL_CONTEXT_GATE_CONTRACT,
        })
        return base
