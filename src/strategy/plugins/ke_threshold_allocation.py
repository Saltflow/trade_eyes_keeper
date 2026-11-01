"""Fixed daily-report Ke thresholds; the shared executor owns all positions.

The input is dated ``implied_ke = 0.02 + 1 / PE_TTM`` for positive PE. This
plugin never substitutes a current valuation or bot price for missing history.
Repeated signals are standing requests, not fills or fabricated holdings.
"""

from __future__ import annotations

import numpy as np

from ..api import Params, ParamSpace, TradePlan, TradingStrategy
from ..fundamental_context import FundamentalStrategyMarketData
from ..registry import register_strategy

KE_PANEL_CONTRACT = "daily-implied-ke-history/1"
KE_FEATURE = "implied_ke"
BUY_KE = 0.09
SELL_KE = 0.05
THRESHOLD_ATOL = 1e-12


class PreparedKeThresholdAllocation:
    """Every batch member revalidates the same historical input contract."""

    def __init__(self, strategy, market_data):
        self.strategy, self.market_data = strategy, market_data

    def evaluate_batch(self, params):
        return [self.strategy.make_signals(item, self.market_data) for item in params]


@register_strategy("ke_threshold_allocation")
class KeThresholdAllocationStrategy(TradingStrategy):
    name = "ke_threshold_allocation"
    label = "日报Ke固定阈值分批配仓"
    description = (
        "历史日报Ke≥9%持续请求买入、≤5%持续请求卖出；实际等额分批配仓由共享执行器决定"
    )
    parameter_schema_id = "ke-threshold-allocation/fixed-1"
    window_state_scope = "full"
    warmup_rows = 1
    manual_activation = True
    feature_dependencies = ()
    fundamental_feature_dependencies = (KE_FEATURE,)
    supported_markets = ("a_share", "hk", "us")

    def __init__(self):
        self._space = ParamSpace([])

    @property
    def param_space(self):
        return self._space

    def execution_params(self, params: Params):
        if params.values:
            raise ValueError("Ke thresholds are fixed; there are no search parameters")
        if params._engine and params._engine != self.name:
            raise ValueError("Ke parameters belong to another strategy")
        execution = {
            "model": "target_weight",
            "allocation_mode": "ke_equal_batch",
            "per_symbol_cap": 0.20,
            "total_exposure_cap": 1.0,
            "max_new_entries": 5,
            "min_holding_calendar_days": 30,
            "buy_price_model": "max_high_t_minus_1_t_t_plus_1",
            "sell_price_model": "trigger_day_low",
        }
        if params.execution_snapshot and params.execution_snapshot != execution:
            raise ValueError("Ke execution snapshot differs from its fixed contract")
        return execution

    def make_signals(
        self, params: Params, market_data: FundamentalStrategyMarketData
    ) -> TradePlan:
        execution = self.execution_params(params)
        if not isinstance(market_data, FundamentalStrategyMarketData):
            raise TypeError(
                "ke_threshold_allocation requires a dated historical Ke panel"
            )
        market_data.require_historical_walk_forward_eligibility()
        market_data.validate_fundamental_panel()
        if market_data.fundamental_feature_contract != KE_PANEL_CONTRACT:
            raise ValueError("Ke input must use daily-implied-ke-history/1")
        names = tuple(market_data.fundamental_feature_names)
        if len(names) != len(set(names)) or KE_FEATURE not in names:
            raise ValueError("Ke panel requires a unique named implied_ke feature")
        if not self.supports_market(market_data.market):
            raise ValueError("unsupported Ke strategy market")
        rows, columns = len(market_data.dates), len(market_data.symbols)
        matrix = np.asarray(market_data.indicator_matrix)
        if matrix.ndim != 3 or matrix.shape[:2] != (rows, columns):
            raise ValueError("Ke indicator shape must match dates and symbols")
        if len(set(market_data.symbols)) != columns or any(
            not isinstance(symbol, str) or not symbol.strip()
            for symbol in market_data.symbols
        ):
            raise ValueError("Ke input requires distinct security identities")
        if market_data.prices is None:
            raise ValueError("Ke history requires observed market prices")
        prices = np.asarray(market_data.prices, dtype=np.float64)
        if prices.shape != (rows, columns):
            raise ValueError("Ke price shape must match dates and symbols")
        dates = np.asarray(market_data.dates, dtype="datetime64[D]")
        if np.isnat(dates).any() or (rows > 1 and np.any(dates[1:] <= dates[:-1])):
            raise ValueError("Ke dates must be finite and strictly increasing")
        ordinals = dates.astype(np.int64)
        if market_data.date_ordinals is not None:
            supplied = np.asarray(market_data.date_ordinals)
            if not np.issubdtype(supplied.dtype, np.integer) or not np.array_equal(
                supplied, ordinals
            ):
                raise ValueError("Ke date ordinals must match calendar dates")
        feature = names.index(KE_FEATURE)
        ke = np.asarray(market_data.fundamental_features, dtype=np.float64)[
            :, :, feature
        ]
        available = np.asarray(market_data.fundamental_availability_mask)[:, :, feature]
        valid = available & np.isfinite(ke) & np.isfinite(prices) & (prices > 0)
        valid &= market_data.eligibility_mask(self.warmup_rows)
        if market_data.tradable is not None:
            tradable = np.asarray(market_data.tradable)
            if tradable.shape != prices.shape or tradable.dtype != bool:
                raise ValueError("Ke tradability must be a matching boolean matrix")
            valid &= tradable
        buy = valid & (
            (ke >= BUY_KE) | np.isclose(ke, BUY_KE, atol=THRESHOLD_ATOL, rtol=0)
        )
        sell = valid & (
            (ke <= SELL_KE) | np.isclose(ke, SELL_KE, atol=THRESHOLD_ATOL, rtol=0)
        )
        return TradePlan(
            buy_signals=buy,
            sell_signals=sell,
            buy_priority=np.where(buy, ke, -np.inf),
            sell_priority=np.where(sell, -ke, -np.inf),
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
                "decision_contract": "daily_implied_ke_fixed_9pct_buy_5pct_sell",
                "ke_definition": "0.02 + 1 / PE_TTM; positive PE only",
                "ke_buy": BUY_KE,
                "ke_sell": SELL_KE,
                "threshold_equality_atol": THRESHOLD_ATOL,
                "fundamental_context_contract": KE_PANEL_CONTRACT,
                "event_semantics": "standing_requests_not_fills",
                "holdings_owner": "shared_executor",
                "historical_bot_price_override": "unavailable_not_backfilled",
            },
            entry_events=buy.copy(),
            exit_events=sell.copy(),
            force_exit_signals=np.zeros_like(buy),
            conviction=np.where(valid, ke, 0.0),
            target_weights=None,
            date_ordinals=ordinals,
        )

    def prepare(self, market_data):
        return PreparedKeThresholdAllocation(self, market_data)

    def to_human_readable(self, params: Params) -> str:
        self.execution_params(params)
        return (
            "日报Ke=2%+1/PE_TTM（正PE且有因果历史）；Ke≥9%买入、≤5%卖出；"
            "共享执行器按Ke排序每批最多5只、单只≤20%、总仓位≤100%；"
            "普通退出至少30自然日，不回填bot价格override，不搜参"
        )
