"""Frozen-policy CAPM-DCF value strategy.

This strategy does not optimise a company's fundamentals on the receiving
portfolio.  It consumes a causally joined panel produced from a broad-universe
policy and makes only executable portfolio decisions:

* buy immediately when the current price is at or below the risk-adjusted
  value entry price;
* otherwise wait until the daily close crosses down through that entry price;
* sell when the daily close reaches the current frozen-policy fair value.

The shared Backtester owns fill prices, lots, fees and minimum holding time.
"""

from __future__ import annotations

import numpy as np

from ..api import Params, ParamSpace, TradePlan, TradingStrategy
from ..capm_dcf_value_context import (
    CAPM_DCF_VALUE_FEATURE_NAMES,
    CAPM_DCF_VALUE_PANEL_CONTRACT,
    make_capm_dcf_value_context_from_config,
)
from ..fundamental_context import FundamentalStrategyMarketData
from ..registry import register_strategy


@register_strategy("capm_dcf_value")
class CapmDcfValueStrategy(TradingStrategy):
    """Long-only value realisation using a frozen broad CAPM-DCF policy."""

    name = "capm_dcf_value"
    label = "CAPM-DCF 价值策略"
    description = "低Beta放宽安全边际；低于目标立即买入、触及公允价值卖出"
    warmup_rows = 1
    manual_activation = True
    parameter_schema_id = "capm-dcf-value/1"
    window_state_scope = "full"
    fundamental_feature_dependencies = CAPM_DCF_VALUE_FEATURE_NAMES
    # Coverage is an interface declaration, not an activation promise.  Each
    # market independently supplies its dated statements, price benchmark,
    # risk-free curve and frozen broad-universe policy via the context factory.
    # Missing evidence fails closed before a TradePlan is generated.
    supported_markets = ("a_share", "hk", "us")

    def __init__(self) -> None:
        # The valuation policy itself is frozen outside the generic solver.
        # Only the standard, immutable cash execution tiers remain here.
        self._space = self.with_execution_dims([])

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    @staticmethod
    def _feature_index(panel: FundamentalStrategyMarketData) -> dict[str, int]:
        if panel.fundamental_feature_contract != CAPM_DCF_VALUE_PANEL_CONTRACT:
            raise ValueError("capm_dcf_value requires a CAPM-DCF value panel")
        indices = {
            name: index
            for index, name in enumerate(panel.fundamental_feature_names)
        }
        missing = [name for name in CAPM_DCF_VALUE_FEATURE_NAMES if name not in indices]
        if missing:
            raise ValueError("CAPM-DCF value panel is missing: " + ", ".join(missing))
        return indices

    def make_signals(
        self, params: Params, market_data: FundamentalStrategyMarketData
    ) -> TradePlan:
        if not isinstance(market_data, FundamentalStrategyMarketData):
            raise TypeError("capm_dcf_value requires a historical value panel")
        market_data.require_historical_walk_forward_eligibility()
        indices = self._feature_index(market_data)
        values = np.asarray(market_data.fundamental_features, dtype=np.float64)
        available = np.asarray(market_data.fundamental_availability_mask, dtype=bool)
        prices = np.asarray(market_data.prices, dtype=np.float64)
        if prices.shape != values.shape[:2]:
            raise ValueError("CAPM-DCF value panel prices are misaligned")
        fair = values[:, :, indices["value:fair_price"]]
        entry = values[:, :, indices["value:entry_price"]]
        required = np.stack(
            [available[:, :, indices[name]] for name in CAPM_DCF_VALUE_FEATURE_NAMES],
            axis=-1,
        ).all(axis=-1)
        valid = required & np.isfinite(prices) & (prices > 0)
        valid &= np.isfinite(fair) & (fair > 0) & np.isfinite(entry) & (entry > 0)
        eligible = market_data.eligibility_mask(self.warmup_rows)
        valid &= eligible

        at_or_below_entry = valid & (prices <= entry)
        above_fair = valid & (prices >= fair)
        source_dates = np.asarray(market_data.fundamental_as_of_dates, dtype=object)
        source_changed = np.zeros_like(valid, dtype=bool)
        if len(valid):
            source_changed[0] = valid[0]
        if len(valid) > 1:
            source_changed[1:] = valid[1:] & (
                ~valid[:-1] | (source_dates[1:] != source_dates[:-1])
            )
        crossed_entry = np.zeros_like(valid, dtype=bool)
        if len(valid) > 1:
            crossed_entry[1:] = (
                valid[1:]
                & valid[:-1]
                & (prices[:-1] > entry[:-1])
                & at_or_below_entry[1:]
            )
        # A new valuation already below its target is marketable immediately.
        # A stale valuation only creates a new order on a fresh downward cross,
        # avoiding repeated daily cash-cap orders while price stays depressed.
        entry_events = at_or_below_entry & (source_changed | crossed_entry)
        # Selling stays active while price is at/above fair value.  The shared
        # executor applies the minimum holding period and ignores it once flat.
        exit_events = above_fair
        entry_events[exit_events] = False

        buy_priority = np.where(
            entry_events,
            np.maximum(entry / np.maximum(prices, 1e-12) - 1.0, 0.0) + 1.0,
            -np.inf,
        )
        sell_priority = np.where(
            exit_events,
            np.maximum(prices / np.maximum(fair, 1e-12) - 1.0, 0.0) + 1.0,
            -np.inf,
        )
        execution = self.execution_params(params)
        return TradePlan(
            buy_signals=entry_events.copy(),
            sell_signals=exit_events.copy(),
            buy_priority=buy_priority.astype(np.float32),
            sell_priority=sell_priority.astype(np.float32),
            buy_cash_limit=float(execution["buy_cash_limit"]),
            sell_cash_limit=float(execution["sell_cash_limit"]),
            warmup_rows=self.warmup_rows,
            dates=list(market_data.dates),
            symbols=list(market_data.symbols),
            execution=dict(execution),
            strategy_metadata={
                "strategy_id": self.name,
                "strategy_label": self.label,
                "parameters": dict(params.values),
                "decision_contract": "marketable_value_entry_or_downward_cross",
                "exit_contract": "price_at_or_above_fair_value",
                "fundamental_context_contract": (
                    market_data.fundamental_feature_contract
                ),
                "valuation_snapshot_count": int(np.count_nonzero(source_changed)),
                "marketable_entry_event_count": int(
                    np.count_nonzero(entry_events & source_changed)
                ),
                "pullback_entry_event_count": int(
                    np.count_nonzero(entry_events & ~source_changed)
                ),
            },
            entry_events=entry_events.copy(),
            exit_events=exit_events.copy(),
        )

    def make_context_enricher(
        self,
        config: dict,
        *,
        market: str,
        symbols: tuple[str, ...],
    ):
        return make_capm_dcf_value_context_from_config(
            config, market=market, symbols=symbols
        )

    def to_human_readable(self, params: Params) -> str:
        execution = self.execution_params(params)
        return (
            "CAPM-DCF 价值策略（冻结大盘政策；低Beta放宽安全边际；"
            f"单次买入上限{float(execution['buy_cash_limit']):.0f}）"
        )
