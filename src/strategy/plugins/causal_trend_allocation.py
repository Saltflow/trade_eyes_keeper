"""Causal long-trend momentum selection with separate inverse-volatility sizing.

Only membership changes rebalance the portfolio. Calendar-month target
reaffirmations let an empty executor adopt an existing selection; an already
active executor does not trade solely because a month changed. Missing held
instruments keep their target budget and selection slot until observable again.
The plugin has no access to execution prices, optimizer results or trial data.
"""

from __future__ import annotations

import numpy as np
from numba import njit

from ..api import (
    ParamDim,
    ParamSpace,
    StrategyMarketData,
    TradePlan,
    TradingStrategy,
)
from ..registry import register_strategy

MOMENTUM_LOOKBACKS = (63, 126, 189, 252)
MOMENTUM_SKIP = 5
TREND_ROWS = 200
TREND_SLOPE_ROWS = 20
VOLATILITY_ROWS = 63
VOLATILITY_FLOOR = 0.10
CONFIRMATION_ROWS = 3
MINIMUM_HOLDING_DAYS = 30


@njit(cache=True, nogil=True)
def _membership_plan(
    momentum,
    ma,
    slope,
    volatility,
    close,
    ready,
    dates,
    months,
    order,
    entry_floor,
    selection_count,
    trend_buffer,
    symbol_cap,
    exposure_cap,
):
    rows, columns = close.shape
    entries = np.zeros((rows, columns), dtype=np.bool_)
    exits = np.zeros_like(entries)
    weights = np.zeros((rows, columns), dtype=np.float64)
    conviction = np.zeros((rows, columns), dtype=np.float32)
    active = np.zeros(columns, dtype=np.bool_)
    entry_streak = np.zeros(columns, dtype=np.int64)
    exit_streak = np.zeros(columns, dtype=np.int64)
    entered = np.zeros(columns, dtype=np.int64)
    reviewed_month = np.full(columns, -1000000000, dtype=np.int64)
    exit_pending = np.zeros(columns, dtype=np.bool_)
    risk = np.full(columns, VOLATILITY_FLOOR, dtype=np.float64)
    previous_weights = np.zeros(columns, dtype=np.float64)
    ranked = np.empty(columns, dtype=np.int64)
    ranks = np.empty(columns, dtype=np.int64)
    for row in range(rows):
        count = 0
        for column in order:
            ranks[column] = columns + 1
            if ready[row, column]:
                # Stable insertion preserves lexical identity on exact ties.
                pos = count
                while (
                    pos > 0 and momentum[row, column] > momentum[row, ranked[pos - 1]]
                ):
                    ranked[pos] = ranked[pos - 1]
                    pos -= 1
                ranked[pos] = column
                count += 1
                risk[column] = max(volatility[row, column], VOLATILITY_FLOOR)
        for index in range(count):
            ranks[ranked[index]] = index + 1
        changed = False
        for column in range(columns):
            if not ready[row, column]:
                entry_streak[column] = 0
                exit_streak[column] = 0
                continue
            entry_ok = (
                close[row, column] > ma[row, column] * (1.0 + trend_buffer)
                and slope[row, column] > 0.0
                and momentum[row, column] > entry_floor
                and ranks[column] <= selection_count
            )
            entry_streak[column] = entry_streak[column] + 1 if entry_ok else 0
            review = reviewed_month[column] != months[row]
            reviewed_month[column] = months[row]
            if exit_pending[column]:
                exits[row, column] = True
            if active[column] and review and entry_ok:
                entries[row, column] = True
            if active[column]:
                exit_bad = (
                    close[row, column] < ma[row, column] * (1.0 - trend_buffer)
                    or slope[row, column] <= 0.0
                    or momentum[row, column] <= 0.0
                    or ranks[column] > selection_count + 1
                )
                exit_streak[column] = exit_streak[column] + 1 if exit_bad else 0
                if (
                    exit_streak[column] >= CONFIRMATION_ROWS
                    and dates[row] - entered[column] >= MINIMUM_HOLDING_DAYS
                ):
                    exits[row, column] = True
                    entries[row, column] = False
                    exit_pending[column] = True
                    active[column] = False
                    entry_streak[column] = 0
                    changed = True
        active_count = int(active.sum())
        for index in range(count):
            column = ranked[index]
            if active_count >= selection_count:
                break
            if not active[column] and entry_streak[column] >= CONFIRMATION_ROWS:
                entries[row, column] = True
                exits[row, column] = False
                exit_pending[column] = False
                active[column] = True
                entered[column] = dates[row]
                exit_streak[column] = 0
                active_count += 1
                changed = True
        if changed:
            # An unobservable held member never releases its budget to a new
            # instrument. It is also still counted in active_count above.
            allocated = np.zeros(columns, dtype=np.float64)
            reserved = 0.0
            for column in range(columns):
                if active[column] and not ready[row, column]:
                    allocated[column] = previous_weights[column]
                    reserved += allocated[column]
            remaining = max(exposure_cap - reserved, 0.0)
            for _iteration in range(columns + 1):
                score_sum = 0.0
                for column in range(columns):
                    if (
                        active[column]
                        and ready[row, column]
                        and allocated[column] < symbol_cap - 1e-12
                    ):
                        score_sum += 1.0 / risk[column]
                if score_sum <= 0.0 or remaining <= 1e-12:
                    break
                used = 0.0
                for column in range(columns):
                    if (
                        active[column]
                        and ready[row, column]
                        and allocated[column] < symbol_cap - 1e-12
                    ):
                        added = min(
                            remaining / risk[column] / score_sum,
                            symbol_cap - allocated[column],
                        )
                        allocated[column] += added
                        used += added
                remaining -= used
            previous_weights = allocated
        for column in range(columns):
            weights[row, column] = previous_weights[column]
            if active[column]:
                conviction[row, column] = (
                    max(momentum[row, column], 0.000001)
                    if ready[row, column]
                    else 0.000001
                )
    return entries, exits, conviction, weights


def _rolling_mean(values, window):
    result = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) >= window:
        cumulative = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
        result[window - 1 :] = (cumulative[window:] - cumulative[:-window]) / window
    return result


class PreparedCausalTrendAllocation:
    """Cache causal price features; all parameter paths share one state kernel."""

    def __init__(self, strategy, market_data):
        if not isinstance(market_data, StrategyMarketData):
            raise TypeError("make_signals requires StrategyMarketData")
        matrix = np.asarray(market_data.indicator_matrix)
        if matrix.ndim != 3 or matrix.shape[2] < 1:
            raise ValueError("causal trend allocation requires qfq Close")
        self.strategy, self.market_data = strategy, market_data
        self.close = np.asarray(matrix[:, :, 0], dtype=np.float64).copy()
        rows, columns = self.close.shape
        if (
            len(market_data.symbols) != columns
            or len(set(market_data.symbols)) != columns
        ):
            raise ValueError("distinct security identities are required")
        if any(
            not isinstance(symbol, str) or not symbol.strip()
            for symbol in market_data.symbols
        ):
            raise ValueError("nonempty security identities are required")
        self.order = np.argsort(np.asarray(market_data.symbols), kind="stable").astype(
            np.int64
        )
        if len(market_data.dates) != rows:
            raise ValueError("calendar dates are required for the holding contract")
        parsed = np.asarray(market_data.dates, dtype="datetime64[D]")
        if np.isnat(parsed).any() or (rows > 1 and np.any(parsed[1:] <= parsed[:-1])):
            raise ValueError("calendar dates must be finite and strictly increasing")
        self.dates = parsed.astype(np.int64)
        self.months = parsed.astype("datetime64[M]").astype(np.int64)
        if market_data.date_ordinals is not None and not np.array_equal(
            self.dates, market_data.date_ordinals
        ):
            raise ValueError("date ordinals do not match calendar dates")
        observed = np.isfinite(self.close) & (self.close > 0)
        if market_data.tradable is not None:
            tradable = np.asarray(market_data.tradable, dtype=bool)
            if tradable.shape != self.close.shape:
                raise ValueError("tradability shape does not match qfq Close")
            observed &= tradable
        if market_data.prices is not None:
            prices = np.asarray(market_data.prices)
            if prices.shape != self.close.shape:
                raise ValueError(
                    "execution availability shape does not match qfq Close"
                )
            observed &= np.isfinite(prices) & (prices > 0)
        self.eligible = observed & market_data.eligibility_mask(strategy.warmup_rows)
        self.ma = np.full(self.close.shape, np.nan)
        self.slope = np.full(self.close.shape, np.nan)
        self.volatility = np.full(self.close.shape, np.nan)
        self.momentum = {
            window: np.full(self.close.shape, np.nan) for window in MOMENTUM_LOOKBACKS
        }
        for column in range(columns):
            indexes = np.flatnonzero(observed[:, column])
            prices = self.close[indexes, column]
            ma = _rolling_mean(prices, TREND_ROWS)
            self.ma[indexes, column] = ma
            if len(indexes) > TREND_SLOPE_ROWS:
                with np.errstate(invalid="ignore", divide="ignore"):
                    self.slope[indexes[TREND_SLOPE_ROWS:], column] = (
                        ma[TREND_SLOPE_ROWS:] / ma[:-TREND_SLOPE_ROWS] - 1.0
                    )
            if len(indexes) > VOLATILITY_ROWS:
                returns = np.diff(np.log(prices))
                mean = _rolling_mean(returns, VOLATILITY_ROWS)
                squares = _rolling_mean(returns * returns, VOLATILITY_ROWS)
                variance = (
                    np.maximum(squares - mean * mean, 0.0)
                    * VOLATILITY_ROWS
                    / (VOLATILITY_ROWS - 1)
                )
                self.volatility[indexes[1:], column] = np.sqrt(variance * 252.0)
            for window in MOMENTUM_LOOKBACKS:
                offset = window + MOMENTUM_SKIP
                if len(prices) > offset:
                    self.momentum[window][indexes[offset:], column] = (
                        prices[window:-MOMENTUM_SKIP] / prices[:-offset] - 1.0
                    )

    def evaluate_batch(self, params):
        plans = []
        for item in params:
            values = self.strategy._decoded(item)
            execution = self.strategy.execution_params(item)
            momentum = self.momentum[int(values["momentum_lookback"])]
            ready = (
                self.eligible
                & np.isfinite(momentum)
                & np.isfinite(self.ma)
                & np.isfinite(self.slope)
                & np.isfinite(self.volatility)
            )
            entries, exits, conviction, weights = _membership_plan(
                momentum,
                self.ma,
                self.slope,
                self.volatility,
                self.close,
                ready,
                self.dates,
                self.months,
                self.order,
                values["entry_return_floor"],
                int(values["selection_count"]),
                values["trend_buffer"],
                execution["per_symbol_cap"],
                execution["total_exposure_cap"],
            )
            plans.append(
                TradePlan(
                    buy_signals=entries,
                    sell_signals=exits,
                    buy_priority=np.where(entries, momentum, -np.inf).astype(
                        np.float32
                    ),
                    sell_priority=np.where(exits, 1.0, -np.inf).astype(np.float32),
                    buy_cash_limit=0.0,
                    sell_cash_limit=0.0,
                    warmup_rows=self.strategy.warmup_rows,
                    dates=list(self.market_data.dates),
                    symbols=list(self.market_data.symbols),
                    execution=dict(execution),
                    strategy_metadata={
                        "strategy_id": self.strategy.name,
                        "parameter_schema": self.strategy.parameter_schema_id,
                        "parameters": dict(item.values),
                        "signal_price_basis": "causal_qfq_close",
                        "allocation_basis": "inverse_63_observation_volatility_floor_10pct_annual",
                        "rebalance_policy": "membership_change_only",
                        "monthly_target_reaffirmation": "first_observable_tradable_day_of_calendar_month",
                        "event_semantics": "target_requests_not_fills",
                        "exit_request_policy": "standing_zero_target_until_new_entry",
                        "missing_held_budget": "reserved_previous_target_and_selection_slot",
                        "entry_confirmation_rows": CONFIRMATION_ROWS,
                        "exit_confirmation_rows": CONFIRMATION_ROWS,
                        "rank_exit_buffer": 1,
                        "momentum_skip_observations": MOMENTUM_SKIP,
                        "fundamental_features": [],
                    },
                    entry_events=entries,
                    exit_events=exits,
                    conviction=conviction,
                    force_exit_signals=np.zeros_like(entries),
                    target_weights=weights,
                    date_ordinals=self.dates.copy(),
                )
            )
        return plans


@register_strategy("causal_trend_allocation")
class CausalTrendAllocationStrategy(TradingStrategy):
    name = "causal_trend_allocation"
    label = "因果趋势动量与独立风险分配"
    description = (
        "长趋势内选择历史动量，成员变化时按逆波动率分配，普通退出至少持有30自然日"
    )
    parameter_schema_id = "causal-trend-allocation/1"
    window_state_scope = "train"
    warmup_rows = max(MOMENTUM_LOOKBACKS) + MOMENTUM_SKIP + 1
    manual_activation = True
    feature_dependencies = ("close",)
    fundamental_feature_dependencies = ()

    def __init__(self):
        self._space = ParamSpace(
            [
                ParamDim("momentum_lookback", 4, 63.0, 252.0),
                ParamDim("entry_return_floor", 5, 0.0, 0.10),
                ParamDim("selection_count", 5, 1.0, 5.0),
                ParamDim("trend_buffer", 3, 0.0, 0.02),
                ParamDim("total_exposure_cap", 3, 0.60, 1.00),
                ParamDim("per_symbol_cap", 4, 0.25, 1.00),
            ]
        )

    @property
    def param_space(self):
        return self._space

    def _decoded(self, params):
        if params._engine and params._engine != self.name:
            raise ValueError("parameters belong to another strategy")
        if set(params.values) - {dim.name for dim in self._space.dims}:
            raise ValueError("unknown causal trend allocation parameter")
        result = {}
        for dim in self._space.dims:
            level = params.values.get(dim.name, 0)
            if (
                isinstance(level, bool)
                or not isinstance(level, (int, np.integer))
                or not 0 <= level < dim.levels
            ):
                raise ValueError("invalid parameter level: " + dim.name)
            result[dim.name] = dim.decode(int(level))
        return result

    def execution_params(self, params):
        values = self._decoded(params)
        execution = (
            dict(params.execution_snapshot)
            if params.execution_snapshot
            else {
                "model": "target_weight",
                "per_symbol_cap": values["per_symbol_cap"],
                "total_exposure_cap": values["total_exposure_cap"],
                "min_holding_calendar_days": MINIMUM_HOLDING_DAYS,
                "buy_price_model": "max_high_t_minus_1_t_t_plus_1",
                "sell_price_model": "trigger_day_low",
            }
        )
        if (
            execution.get("model") != "target_weight"
            or execution.get("min_holding_calendar_days") != MINIMUM_HOLDING_DAYS
        ):
            raise ValueError(
                "causal trend allocation requires the 30-calendar-day target-weight contract"
            )
        if (
            execution.get("buy_price_model") != "max_high_t_minus_1_t_t_plus_1"
            or execution.get("sell_price_model") != "trigger_day_low"
        ):
            raise ValueError(
                "causal trend allocation requires conservative fill prices"
            )
        for key in ("per_symbol_cap", "total_exposure_cap"):
            value = execution.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not np.isfinite(value)
                or not 0 < value <= 1
            ):
                raise ValueError("invalid long-only exposure cap: " + key)
            execution[key] = float(value)
        return execution

    def prepare(self, market_data):
        return PreparedCausalTrendAllocation(self, market_data)

    def make_signals(self, params, market_data):
        return self.prepare(market_data).evaluate_batch([params])[0]

    def to_human_readable(self, params):
        values, execution = self._decoded(params), self.execution_params(params)
        return (
            f"因果趋势动量：{int(values['momentum_lookback'])}期动量（跳过最近5期），"
            f"MA200上升且价格越过{values['trend_buffer']:.0%}缓冲；Top{int(values['selection_count'])}，三期确认；"
            "成员变化时逆63期波动率配仓、每月首个可交易日重申目标（已有持仓不因此交易），缺失持仓保留预算；"
            f"单标的≤{execution['per_symbol_cap']:.0%}、总仓位≤{execution['total_exposure_cap']:.0%}；普通退出至少30自然日"
        )
