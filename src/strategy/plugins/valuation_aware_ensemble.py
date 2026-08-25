"""Opt-in technical/valuation ensemble for causal research and search."""

from __future__ import annotations

import numpy as np

from ..api import (
    ParamDim,
    Params,
    ParamSpace,
    StrategyMarketData,
    TradePlan,
    TradingStrategy,
)
from ..features import TECHNICAL_FEATURES
from ..fundamental_context import FundamentalStrategyMarketData
from ..registry import register_strategy
from ..valuation_context_kernel import (
    VALUATION_CONTEXT_QUALITY_NAME,
    VALUATION_CONTEXT_SIGNAL_NAMES,
    score_valuation_context_series,
)
from .technical_ensemble import TechnicalEnsembleStrategy


@register_strategy("valuation_aware_ensemble")
class ValuationAwareEnsembleStrategy(TradingStrategy):
    """Blend the fixed-direction technical score with quality-gated valuation ranks.

    The plugin is deliberately opt-in.  It requires an explicit historical
    context enricher and therefore cannot accidentally consume the current
    valuation snapshot during a walk-forward search.
    """

    name = "valuation_aware_ensemble"
    label = "技术面 + 反向DCF共识"
    description = "22因子技术集成叠加质量门控的行业相对估值专家"
    warmup_rows = 252
    manual_activation = True
    parameter_schema_id = "valuation-aware-ensemble/1"
    window_state_scope = "train"
    feature_dependencies = TECHNICAL_FEATURES.names
    fundamental_feature_dependencies = (
        *VALUATION_CONTEXT_SIGNAL_NAMES,
        VALUATION_CONTEXT_QUALITY_NAME,
    )

    def __init__(self):
        self._technical = TechnicalEnsembleStrategy()
        self._space = ParamSpace(
            [
                *self._technical.param_space.dims,
                ParamDim("valuation_weight", 5, 0.0, 1.0),
            ]
        )

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def _decode(self, params: Params, name: str) -> float:
        dim = next(item for item in self.param_space.dims if item.name == name)
        return params.decode(dim)

    def execution_params(self, params: Params) -> dict[str, float | int | str]:
        return self._technical.execution_params(params)

    def make_signals(
        self, params: Params, market_data: StrategyMarketData
    ) -> TradePlan:
        if not isinstance(market_data, FundamentalStrategyMarketData):
            raise TypeError(
                "valuation_aware_ensemble requires a historical fundamental panel"
            )
        market_data.require_historical_walk_forward_eligibility()
        technical_score = self._technical._score(params, market_data.indicator_matrix)
        valuation_score, usable, quality = score_valuation_context_series(market_data)
        weight = self._decode(params, "valuation_weight")
        score = (1.0 - weight) * technical_score + weight * valuation_score
        plan = self._technical._plan_from_score(params, market_data, score)
        plan.strategy_metadata = {
            **plan.strategy_metadata,
            "strategy_id": self.name,
            "strategy_label": self.label,
            "valuation_context_contract": market_data.fundamental_feature_contract,
            "valuation_weight": weight,
            "valuation_usable_fraction": float(np.mean(usable)) if usable.size else 0.0,
            "valuation_quality_mean": float(np.mean(quality)) if quality.size else 0.0,
            "feature_contract_hash": TECHNICAL_FEATURES.hash,
            "fundamental_features": list(self.fundamental_feature_dependencies),
        }
        return plan

    def to_human_readable(self, params: Params) -> str:
        active = [
            name
            for name in TECHNICAL_FEATURES.names
            if int(params.values.get(f"weight_{name}", 0)) > 0
        ]
        weight = self._decode(params, "valuation_weight")
        return (
            f"技术面+反向DCF共识（启用{len(active)}项技术因子，"
            f"估值权重{weight:.0%}）"
        )
