"""Justified-PB value strategy for A-share stocks (fixed rule).

Buy when the current PB is below the justified PB implied by a 10% cost of
equity and 2% perpetual growth; sell unconditionally when PB exceeds the
justified PB implied by a 6% cost of equity (same growth), otherwise hold.
Position sizing is one-shot: every qualifying symbol receives an equal 20%
slot (the shared engine scales slots when the portfolio would overshoot the
100% exposure cap).  The strategy reads a point-in-time quarterly panel and
never changes the production configuration.
"""

from __future__ import annotations

import numpy as np

from ..api import ParamSpace, Params, TradePlan, TradingStrategy
from ..fundamental_context import FundamentalStrategyMarketData
from ..justified_pb_value_context import (
    REQUIRED_FEATURES,
    make_justified_pb_value_context,
)
from ..registry import register_strategy

KE_BUY = 0.10  # buy-side cost of equity
KE_SELL = 0.06  # sell-side cost of equity
GROWTH = 0.02  # perpetual growth rate
PER_SYMBOL_CAP = 0.20
TOTAL_EXPOSURE_CAP = 1.0
WARMUP_ROWS = 1


@register_strategy("justified_pb_value")
class JustifiedPbValueStrategy(TradingStrategy):
    """Long-only justified-PB value rotation for the A-share market."""

    name = "justified_pb_value"
    label = "Justified-PB 价值策略"
    description = ("PB < (ROE-2%)/(10%-2%) 买入（每只≤20%，一次投完）；"
                   "PB > (ROE-2%)/(6%-2%) 无条件卖出；中间持有等待")
    warmup_rows = WARMUP_ROWS
    parameter_schema_id = "justified-pb-value/fixed-1"
    window_state_scope = "full"
    fundamental_feature_dependencies = REQUIRED_FEATURES
    supported_markets = ("a_share",)

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

    @staticmethod
    def _feature_index(market_data: FundamentalStrategyMarketData) -> dict[str, int]:
        names = tuple(str(name) for name in market_data.fundamental_feature_names)
        indices = {name: index for index, name in enumerate(names)}
        missing = [name for name in REQUIRED_FEATURES if name not in indices]
        if missing:
            raise ValueError(
                "justified_pb_value panel is missing: " + ", ".join(missing)
            )
        return indices

    def make_signals(
        self, params: Params, market_data: FundamentalStrategyMarketData
    ) -> TradePlan:
        if not isinstance(market_data, FundamentalStrategyMarketData):
            raise TypeError(
                "justified_pb_value requires a historical fundamental panel"
            )
        market_data.require_historical_walk_forward_eligibility()
        indices = self._feature_index(market_data)
        values = np.asarray(market_data.fundamental_features, dtype=np.float64)
        mask = np.asarray(market_data.fundamental_availability_mask, dtype=bool)
        prices = np.asarray(market_data.prices, dtype=np.float64)
        if values.ndim != 3 or values.shape[:2] != prices.shape:
            raise ValueError("justified_pb_value fundamental panel is misaligned")

        roe = values[:, :, indices["roe_ttm"]]
        book_yield = values[:, :, indices["book_yield"]]
        observed = mask[:, :, indices["roe_ttm"]] & mask[:, :, indices["book_yield"]]
        valid = (
            observed
            & np.isfinite(roe)
            & np.isfinite(book_yield)
            & (book_yield > 0.0)
            & (roe > GROWTH * 100.0)
            & np.isfinite(prices)
            & (prices > 0.0)
        )

        pb = np.full(book_yield.shape, np.nan, dtype=np.float64)
        np.divide(1.0, book_yield, out=pb, where=valid)
        # justified PB = (ROE% - g%) / (Ke - g) in percentage-point form
        j1 = (roe - GROWTH * 100.0) / ((KE_BUY - GROWTH) * 100.0)
        j2 = (roe - GROWTH * 100.0) / ((KE_SELL - GROWTH) * 100.0)

        eligible = market_data.eligibility_mask(self.warmup_rows)
        valid &= eligible
        buy_band = valid & (pb < j1)
        sell_band = valid & (pb > j2)
        buy_band &= ~sell_band

        source_dates = np.asarray(market_data.fundamental_as_of_dates, dtype=object)
        source_changed = np.zeros_like(valid)
        if valid.shape[0]:
            source_changed[0] = valid[0]
        if valid.shape[0] > 1:
            source_changed[1:] = valid[1:] & (
                ~valid[:-1] | (source_dates[1:] != source_dates[:-1])
            )
        crossed_down = np.zeros_like(valid)
        if valid.shape[0] > 1:
            crossed_down[1:] = (
                valid[1:] & valid[:-1] & ~buy_band[:-1] & buy_band[1:]
            )

        # One-shot entry: only a fresh quarterly valuation or a downward cross
        # into the buy band creates an order.  Selling stays active above the
        # sell threshold; the executor applies the 30-day holding lock.
        entry_events = (buy_band & (source_changed | crossed_down)).copy()
        entry_events[sell_band] = False
        exit_events = sell_band

        buy_priority = np.where(
            entry_events, np.maximum(j1 - pb, 0.0) + 1.0, -np.inf
        ).astype(np.float32)
        sell_priority = np.where(
            exit_events, np.maximum(pb - j2, 0.0) + 1.0, -np.inf
        ).astype(np.float32)
        target_weights = np.full(pb.shape, PER_SYMBOL_CAP, dtype=np.float32)
        conviction = np.where(entry_events, 1.0, 0.0).astype(np.float32)
        execution = self.execution_params(params)
        date_ordinals = (
            None
            if market_data.date_ordinals is None
            else np.asarray(market_data.date_ordinals, dtype=np.int64)
        )

        return TradePlan(
            buy_signals=entry_events,
            sell_signals=exit_events,
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
                "ke_buy": KE_BUY,
                "ke_sell": KE_SELL,
                "growth": GROWTH,
                "per_symbol_cap": PER_SYMBOL_CAP,
                "total_exposure_cap": TOTAL_EXPOSURE_CAP,
                "decision_contract": "justified_pb_entry_and_sell_bands",
                "fundamental_context_contract": (
                    market_data.fundamental_feature_contract
                ),
                "entry_event_count": int(np.count_nonzero(entry_events)),
                "exit_event_count": int(np.count_nonzero(exit_events)),
            },
            entry_events=entry_events,
            exit_events=exit_events,
            force_exit_signals=np.zeros_like(entry_events, dtype=bool),
            conviction=conviction,
            target_weights=target_weights,
            date_ordinals=date_ordinals,
        )

    def make_context_enricher(
        self, config: dict, *, market: str, symbols: tuple[str, ...]
    ):
        return make_justified_pb_value_context(
            config, market=market, symbols=tuple(symbols)
        )

    def to_human_readable(self, params: Params) -> str:
        return (
            "Justified-PB: 买入 PB < (ROE%-2)/8 (Ke=10%, g=2%)；"
            "卖出 PB > (ROE%-2)/4 (Ke=6%, g=2%)；每只≤20%，一次投完"
        )