"""Reference strategy that consumes the complete 22-feature contract."""

from __future__ import annotations

import numpy as np

from ..features import TECHNICAL_FEATURES
from ..registry import register_strategy
from ..api import (
    ParamDim,
    ParamSpace,
    Params,
    TradingStrategy,
    StrategyMarketData,
    TradePlan,
)


class PreparedTechnicalEnsemble:
    def __init__(self, strategy, market_data: StrategyMarketData):
        self.strategy = strategy
        self.market_data = market_data
        self.features, self.availability_mask = TECHNICAL_FEATURES.transform(
            market_data.indicator_matrix,
            str(getattr(market_data, "market", "a_share")),
        )

    def evaluate_batch(self, params: list[Params]) -> list[TradePlan]:
        if not params:
            return []
        weights = np.asarray(
            [
                [
                    float(item.values.get(f"weight_{name}", 0))
                    for name in TECHNICAL_FEATURES.names
                ]
                for item in params
            ],
            dtype=np.float32,
        )
        valid_features = np.where(self.availability_mask, self.features, 0.0)
        scores = np.tensordot(valid_features, weights.T, axes=([2], [0]))
        denominators = np.tensordot(
            self.availability_mask.astype(np.float32),
            weights.T,
            axes=([2], [0]),
        )
        scores = np.divide(
            scores,
            denominators,
            out=np.zeros_like(scores, dtype=np.float32),
            where=denominators > 0,
        )
        return [
            self.strategy._plan_from_score(item, self.market_data, scores[:, :, index])
            for index, item in enumerate(params)
        ]


@register_strategy("technical_ensemble")
class TechnicalEnsembleStrategy(TradingStrategy):
    name = "technical_ensemble"
    label = "22因子技术集成"
    description = "固定经济方向的22列技术因子集成与批量评分参考实现"
    warmup_rows = 252
    parameter_schema_id = "technical-ensemble/1"
    feature_dependencies = TECHNICAL_FEATURES.names
    fundamental_feature_dependencies: tuple[str, ...] = ()

    def __init__(self):
        dims = [
            ParamDim(f"weight_{name}", 5, 0.0, 1.0) for name in TECHNICAL_FEATURES.names
        ]
        dims.extend(
            [
                ParamDim("buy_threshold", 9, 0.0, 0.8),
                ParamDim("sell_threshold", 9, 0.0, 0.8),
            ]
        )
        self._space = self.with_execution_dims(dims)

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def prepare(self, market_data: StrategyMarketData) -> PreparedTechnicalEnsemble:
        return PreparedTechnicalEnsemble(self, market_data)

    def evaluate(self, params: Params, indicator_matrix: np.ndarray) -> np.ndarray:
        market_data = StrategyMarketData(indicator_matrix=indicator_matrix)
        plan = self.prepare(market_data).evaluate_batch([params])[0]
        return np.stack([plan.buy_priority, plan.sell_priority], axis=-1)

    def _score(self, params: Params, indicator_matrix: np.ndarray) -> np.ndarray:
        features, mask = TECHNICAL_FEATURES.transform(indicator_matrix)
        weights = np.asarray(
            [
                float(params.values.get(f"weight_{name}", 0))
                for name in TECHNICAL_FEATURES.names
            ],
            dtype=np.float32,
        )
        numerator = np.sum(np.where(mask, features, 0.0) * weights, axis=2)
        denominator = np.sum(mask.astype(np.float32) * weights, axis=2)
        return np.divide(
            numerator,
            denominator,
            out=np.zeros_like(numerator, dtype=np.float32),
            where=denominator > 0,
        )

    def _make_signal_arrays(self, params: Params, indicator_matrix: np.ndarray):
        score = self._score(params, indicator_matrix)
        buy_threshold = self.param_space.dims[-4].decode(
            int(params.values.get("buy_threshold", 0))
        )
        sell_threshold = self.param_space.dims[-3].decode(
            int(params.values.get("sell_threshold", 0))
        )
        condition = score >= buy_threshold
        buy = condition.copy()
        buy[:2] = False
        buy[2:] = condition[2:] & condition[1:-1] & condition[:-2]
        sell = score <= -sell_threshold
        return buy, sell

    def make_signals(
        self, params: Params, market_data: StrategyMarketData
    ) -> TradePlan:
        return self.prepare(market_data).evaluate_batch([params])[0]

    def _plan_from_score(
        self, params: Params, market_data: StrategyMarketData, score: np.ndarray
    ) -> TradePlan:
        buy_threshold = self.param_space.dims[-4].decode(
            int(params.values.get("buy_threshold", 0))
        )
        sell_threshold = self.param_space.dims[-3].decode(
            int(params.values.get("sell_threshold", 0))
        )
        condition = score >= buy_threshold
        buy = condition.copy()
        buy[:2] = False
        buy[2:] = condition[2:] & condition[1:-1] & condition[:-2]
        sell = score <= -sell_threshold
        valid = np.isfinite(market_data.indicator_matrix[:, :, 0])
        eligible = np.cumsum(valid, axis=0) >= self.warmup_rows
        buy &= eligible
        sell &= eligible
        buy[sell] = False
        execution = self.execution_params(params)
        return TradePlan(
            buy_signals=buy,
            sell_signals=sell,
            buy_priority=np.where(buy, score, -np.inf).astype(np.float32),
            sell_priority=np.where(sell, -score, -np.inf).astype(np.float32),
            buy_cash_limit=float(execution["buy_cash_limit"]),
            sell_cash_limit=float(execution["sell_cash_limit"]),
            warmup_rows=self.warmup_rows,
            dates=list(market_data.dates),
            symbols=list(market_data.symbols),
            execution=dict(execution),
            strategy_metadata={
                "strategy_id": self.name,
                "feature_contract_hash": TECHNICAL_FEATURES.hash,
                "fundamental_features": [],
                "parameters": dict(params.values),
            },
        )

    def to_human_readable(self, params: Params) -> str:
        active = [
            name
            for name in TECHNICAL_FEATURES.names
            if int(params.values.get(f"weight_{name}", 0)) > 0
        ]
        return f"22因子技术集成（启用{len(active)}项：{', '.join(active)}）"
