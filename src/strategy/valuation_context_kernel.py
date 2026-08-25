"""Vectorized scoring kernel for the historical valuation context contract."""

from __future__ import annotations

import numpy as np

from .fundamental_context import FundamentalStrategyMarketData
from .valuation_context_panel import VALUATION_CONTEXT_PANEL_CONTRACT

VALUATION_CONTEXT_SIGNAL_NAMES = (
    "valuation:beta_cheap_rank",
    "valuation:capm_cost_cheap_rank",
    "valuation:implied_growth_cheap_rank",
    "valuation:growth_gap_cheap_rank",
    "valuation:fundamental_growth_rank",
    "industry_relative:implied_growth_cheap_rank",
    "industry_relative:growth_gap_cheap_rank",
)
VALUATION_CONTEXT_QUALITY_NAME = "valuation:observation_fraction"


def score_valuation_context_series(
    panel: FundamentalStrategyMarketData,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(scores, usable_mask, observation_fraction)`` for all dates.

    This is the columnar equivalent of ``ValuationContextScorer.score``.  It
    intentionally uses the same signed rank fields and quality attenuation,
    but avoids a Python loop over dates so a strategy can evaluate a candidate
    efficiently inside the normal search service.
    """

    panel.validate_fundamental_panel()
    if panel.fundamental_feature_contract != VALUATION_CONTEXT_PANEL_CONTRACT:
        raise ValueError("panel is not a historical valuation context panel")
    names = {name: index for index, name in enumerate(panel.fundamental_feature_names)}
    required = (*VALUATION_CONTEXT_SIGNAL_NAMES, VALUATION_CONTEXT_QUALITY_NAME)
    missing = [name for name in required if name not in names]
    if missing:
        raise ValueError("valuation context panel is missing: " + ", ".join(missing))
    values = np.asarray(panel.fundamental_features, dtype=np.float64)
    available = np.asarray(panel.fundamental_availability_mask, dtype=bool)
    signal_indices = [names[name] for name in VALUATION_CONTEXT_SIGNAL_NAMES]
    signal_values = values[:, :, signal_indices]
    signal_mask = available[:, :, signal_indices]
    counts = signal_mask.sum(axis=2)
    raw = np.divide(
        np.where(signal_mask, signal_values, 0.0).sum(axis=2),
        counts,
        out=np.zeros(values.shape[:2], dtype=np.float64),
        where=counts > 0,
    )
    quality = np.clip(values[:, :, names[VALUATION_CONTEXT_QUALITY_NAME]], 0.0, 1.0)
    usable = counts > 0
    return raw * np.sqrt(quality), usable, quality
