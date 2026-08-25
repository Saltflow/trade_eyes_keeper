"""Deterministic scorer for the historical valuation context panel."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fundamental_context import FundamentalStrategyMarketData
from .valuation_context_panel import VALUATION_CONTEXT_PANEL_CONTRACT

VALUATION_SCORE_CONTRACT = "valuation-context-score-1"


@dataclass(frozen=True)
class ValuationContextScore:
    scores: np.ndarray
    usable_mask: np.ndarray
    observation_fraction: np.ndarray
    contract: str = VALUATION_SCORE_CONTRACT


class ValuationContextScorer:
    """Combine only the signed rank fields, with no fitted parameters."""

    _SIGNAL_NAMES = (
        "valuation:beta_cheap_rank",
        "valuation:capm_cost_cheap_rank",
        "valuation:implied_growth_cheap_rank",
        "valuation:growth_gap_cheap_rank",
        "valuation:fundamental_growth_rank",
        "industry_relative:implied_growth_cheap_rank",
        "industry_relative:growth_gap_cheap_rank",
    )
    _QUALITY_NAME = "valuation:observation_fraction"

    def score(
        self,
        panel: FundamentalStrategyMarketData,
        *,
        date_index: int = -1,
    ) -> ValuationContextScore:
        panel.validate_fundamental_panel()
        if panel.fundamental_feature_contract != VALUATION_CONTEXT_PANEL_CONTRACT:
            raise ValueError("panel is not a historical valuation context panel")
        names = {name: index for index, name in enumerate(panel.fundamental_feature_names)}
        missing = [name for name in (*self._SIGNAL_NAMES, self._QUALITY_NAME) if name not in names]
        if missing:
            raise ValueError("valuation context panel is missing: " + ", ".join(missing))
        values = np.asarray(panel.fundamental_features)[date_index]
        available = np.asarray(panel.fundamental_availability_mask)[date_index]
        signal_indices = [names[name] for name in self._SIGNAL_NAMES]
        signal_values = values[:, signal_indices]
        signal_mask = available[:, signal_indices]
        counts = signal_mask.sum(axis=1)
        raw = np.divide(
            np.where(signal_mask, signal_values, 0.0).sum(axis=1),
            counts,
            out=np.zeros(len(values)),
            where=counts > 0,
        )
        quality = np.clip(values[:, names[self._QUALITY_NAME]], 0.0, 1.0)
        usable = counts > 0
        # Quality only attenuates a signal; it never fabricates one.
        scores = raw * np.sqrt(quality)
        return ValuationContextScore(
            scores=scores,
            usable_mask=usable,
            observation_fraction=quality,
        )
