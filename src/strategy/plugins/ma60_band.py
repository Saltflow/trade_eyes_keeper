"""Fixed MA60 hysteresis strategy used as a simple timing benchmark."""

from __future__ import annotations

import numpy as np

from ...backtest.engine import IDX_CLOSE, IDX_MA60
from ..api import ParamSpace, Params, StrategyMarketData, TradePlan, TradingStrategy
from ..registry import register_strategy


BUY_RATIO = 0.95
SELL_RATIO = 1.05
PER_SYMBOL_CAP = 0.25
TOTAL_EXPOSURE_CAP = 1.0


@register_strategy("ma60_band")
class Ma60BandStrategy(TradingStrategy):
    """Buy below MA60 -5% and sell above MA60 +5%."""

    name = "ma60_band"
    label = "MA60 +/-5% band"
    description = (
        "Use four fixed 25% capital slots; buy below 95% of MA60 and sell "
        "above 105% of MA60"
    )
    warmup_rows = 60
    parameter_schema_id = "ma60_band/fixed-1"
    window_state_scope = "train"
    feature_dependencies = ("close", "ma60")

    def __init__(self) -> None:
        self._space = ParamSpace([])

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def execution_params(self, params: Params) -> dict[str, float | int | str]:
        return {
            "model": "target_weight",
            "per_symbol_cap": PER_SYMBOL_CAP,
            "total_exposure_cap": TOTAL_EXPOSURE_CAP,
            "buy_price_model": "max_high_t_minus_1_t_t_plus_1",
            "sell_price_model": "trigger_day_low",
        }

    def make_signals(
        self, params: Params, market_data: StrategyMarketData
    ) -> TradePlan:
        if not isinstance(market_data, StrategyMarketData):
            raise TypeError("make_signals requires StrategyMarketData")

        matrix = np.asarray(market_data.indicator_matrix, dtype=np.float32)
        if matrix.ndim != 3 or matrix.shape[2] <= max(IDX_CLOSE, IDX_MA60):
            raise ValueError("ma60_band requires close and ma60 indicator columns")

        close = matrix[:, :, IDX_CLOSE].astype(np.float64)
        ma60 = matrix[:, :, IDX_MA60].astype(np.float64)
        valid = np.isfinite(close) & np.isfinite(ma60) & (ma60 > 0.0)
        eligible = valid & market_data.eligibility_mask(self.warmup_rows)

        price_to_ma60 = np.full(close.shape, np.nan, dtype=np.float64)
        np.divide(close, ma60, out=price_to_ma60, where=valid)
        entries = eligible & (price_to_ma60 < BUY_RATIO)
        exits = eligible & (price_to_ma60 > SELL_RATIO)
        entries &= ~exits

        buy_strength = np.maximum(BUY_RATIO - price_to_ma60, 0.0)
        sell_strength = np.maximum(price_to_ma60 - SELL_RATIO, 0.0)
        buy_priority = np.where(entries, buy_strength, -np.inf).astype(np.float32)
        sell_priority = np.where(exits, sell_strength, -np.inf).astype(np.float32)
        target_weights = np.full(close.shape, PER_SYMBOL_CAP, dtype=np.float32)
        conviction = np.where(entries, np.maximum(buy_strength, 1e-6), 0.0).astype(
            np.float32
        )
        execution = self.execution_params(params)
        date_ordinals = (
            None
            if market_data.date_ordinals is None
            else np.asarray(market_data.date_ordinals, dtype=np.int64)
        )

        return TradePlan(
            buy_signals=entries,
            sell_signals=exits,
            buy_priority=buy_priority,
            sell_priority=sell_priority,
            buy_cash_limit=0.0,
            sell_cash_limit=0.0,
            warmup_rows=self.warmup_rows,
            dates=list(market_data.dates),
            symbols=list(market_data.symbols),
            execution=execution,
            strategy_metadata={
                "strategy_id": self.name,
                "strategy_label": self.label,
                "parameter_schema": self.parameter_schema_id,
                "parameters": {},
                "buy_ratio": BUY_RATIO,
                "sell_ratio": SELL_RATIO,
                "slot_weight": PER_SYMBOL_CAP,
            },
            entry_events=entries,
            exit_events=exits,
            force_exit_signals=np.zeros_like(entries, dtype=bool),
            conviction=conviction,
            target_weights=target_weights,
            date_ordinals=date_ordinals,
        )

    def to_human_readable(self, params: Params) -> str:
        return (
            "MA60 band: buy when close < 95% of MA60; sell when close > "
            "105% of MA60; fixed 25% slot per instrument, 100% total cap"
        )
