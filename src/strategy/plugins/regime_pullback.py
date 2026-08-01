"""Regime-aware pullback strategy with one-shot confirmed entry events."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ...backtest.engine import (
    HAS_NUMBA,
    IDX_ADX,
    IDX_ATR,
    IDX_BOLL_PCT_B,
    IDX_CLOSE,
    IDX_DEVIATION,
    IDX_MACD_HIST,
    IDX_MA200,
    IDX_MA200_SLOPE,
    IDX_MINUS_DI,
    IDX_PLUS_DI,
    IDX_RSI,
    IDX_RSI_PCT,
    jit,
)
from ..registry import register_strategy
from ..api import (
    ParamDim,
    Params,
    ParamSpace,
    TradingStrategy,
    StrategyMarketData,
    TradePlan,
)


def _weighted_score(
    factors: list[np.ndarray], weights: list[float]
) -> np.ndarray:
    total = float(sum(weight for weight in weights if weight > 0))
    if total <= 0:
        return np.zeros_like(factors[0], dtype=np.float32)
    result = np.zeros_like(factors[0], dtype=np.float32)
    for factor, weight in zip(factors, weights):
        if weight > 0:
            result += float(weight) * np.asarray(factor, dtype=np.float32)
    return result / total


def _calendar_ordinals(dates: list[str], rows: int) -> np.ndarray:
    if len(dates) == rows:
        parsed = pd.to_datetime(dates, errors="coerce")
        if not pd.isna(parsed).any():
            return parsed.values.astype("datetime64[D]").astype(np.int64)
    return np.arange(rows, dtype=np.int64)


if HAS_NUMBA:

    @jit(nopython=True, parallel=False, cache=True)
    def _state_machine_numba(
        eligible,
        regime,
        setup_ready,
        recovery_ready,
        setup_score,
        recovery_score,
        close,
        ma200,
        rsi,
        deviation,
        atr,
        dates,
        setup_threshold,
        recovery_threshold,
        exit_rsi,
        exit_deviation,
    ):
        rows, columns = close.shape
        entries = np.zeros((rows, columns), dtype=np.bool_)
        exits = np.zeros((rows, columns), dtype=np.bool_)
        force_exits = np.zeros((rows, columns), dtype=np.bool_)
        conviction = np.zeros((rows, columns), dtype=np.float32)
        logical_active = np.zeros((rows, columns), dtype=np.bool_)
        prepared = np.zeros(columns, dtype=np.bool_)
        locked = np.zeros(columns, dtype=np.bool_)
        active = np.zeros(columns, dtype=np.bool_)
        streak = np.zeros(columns, dtype=np.int16)
        entry_date = np.full(columns, -1000000000, dtype=np.int64)
        peak = np.full(columns, np.nan, dtype=np.float64)
        active_score = np.zeros(columns, dtype=np.float32)
        for row in range(rows):
            for column in range(columns):
                if not eligible[row, column]:
                    continue
                if not regime[row, column] or not recovery_ready[row, column]:
                    locked[column] = False
                if active[column]:
                    if not np.isnan(close[row, column]):
                        if np.isnan(peak[column]):
                            peak[column] = close[row, column]
                        else:
                            peak[column] = max(peak[column], close[row, column])
                    held_days = dates[row] - entry_date[column]
                    catastrophe = (
                        close[row, column] < ma200[row, column]
                        and not np.isnan(atr[row, column])
                        and atr[row, column] > 0
                        and peak[column] - close[row, column]
                        > 3.0 * atr[row, column]
                    )
                    repaired = (
                        rsi[row, column] >= exit_rsi
                        or deviation[row, column] >= exit_deviation
                    )
                    if catastrophe:
                        force_exits[row, column] = True
                        active[column] = False
                        prepared[column] = False
                        streak[column] = 0
                    elif held_days >= 30 and (
                        repaired or not regime[row, column]
                    ):
                        exits[row, column] = True
                        active[column] = False
                        prepared[column] = False
                        streak[column] = 0
                if not active[column]:
                    if setup_ready[row, column]:
                        prepared[column] = True
                    if prepared[column] and recovery_ready[row, column]:
                        streak[column] += 1
                    else:
                        streak[column] = 0
                    if streak[column] >= 3 and not locked[column]:
                        entries[row, column] = True
                        active[column] = True
                        locked[column] = True
                        prepared[column] = False
                        streak[column] = 0
                        entry_date[column] = dates[row]
                        peak[column] = close[row, column]
                        active_score[column] = max(
                            setup_score[row, column]
                            + recovery_score[row, column]
                            - setup_threshold
                            - recovery_threshold,
                            0.000001,
                        )
                if active[column]:
                    conviction[row, column] = active_score[column]
                    logical_active[row, column] = True
        return entries, exits, force_exits, conviction, logical_active

    @jit(nopython=True, parallel=False, cache=True, nogil=True)
    def _target_weights_numba(
        conviction,
        active,
        per_symbol_cap,
        total_exposure_cap,
    ):
        rows, columns = conviction.shape
        weights = np.zeros((rows, columns), dtype=np.float32)
        for row in range(rows):
            remaining = min(max(total_exposure_cap, 0.0), 1.0)
            while remaining > 0.000000000001:
                score_sum = 0.0
                open_count = 0
                for column in range(columns):
                    if (
                        active[row, column]
                        and conviction[row, column] > 0.0
                        and weights[row, column]
                        < per_symbol_cap - 0.000000000001
                    ):
                        score_sum += conviction[row, column]
                        open_count += 1
                if open_count == 0 or score_sum <= 0:
                    break
                used = 0.0
                for column in range(columns):
                    if (
                        not active[row, column]
                        or conviction[row, column] <= 0.0
                        or weights[row, column]
                        >= per_symbol_cap - 0.000000000001
                    ):
                        continue
                    proposal = (
                        remaining * conviction[row, column] / score_sum
                    )
                    room = per_symbol_cap - weights[row, column]
                    addition = min(proposal, room)
                    weights[row, column] += addition
                    used += addition
                if used <= 0.000000000001:
                    break
                remaining -= used
        return weights

    @jit(nopython=True, parallel=False, cache=True, nogil=True)
    def _signal_state_from_matrix_numba(
        matrix,
        dates,
        warmup_rows,
        adx_min,
        rsi_pullback_pct,
        deviation_pullback,
        boll_pullback,
        rsi_setup_weight,
        deviation_setup_weight,
        boll_setup_weight,
        setup_threshold,
        rsi_recovery_weight,
        price_recovery_weight,
        macd_recovery_weight,
        recovery_threshold,
        exit_rsi,
        exit_deviation,
    ):
        """Compute scores and state in one allocation-light optimizer kernel."""
        rows, columns = matrix.shape[:2]
        entries = np.zeros((rows, columns), dtype=np.bool_)
        exits = np.zeros((rows, columns), dtype=np.bool_)
        force_exits = np.zeros((rows, columns), dtype=np.bool_)
        conviction = np.zeros((rows, columns), dtype=np.float32)
        logical_active = np.zeros((rows, columns), dtype=np.bool_)
        prepared = np.zeros(columns, dtype=np.bool_)
        locked = np.zeros(columns, dtype=np.bool_)
        active = np.zeros(columns, dtype=np.bool_)
        streak = np.zeros(columns, dtype=np.int16)
        observed = np.zeros(columns, dtype=np.int32)
        entry_date = np.full(columns, -1000000000, dtype=np.int64)
        peak = np.full(columns, np.nan, dtype=np.float64)
        active_score = np.zeros(columns, dtype=np.float32)
        setup_weight_total = (
            max(rsi_setup_weight, 0.0)
            + max(deviation_setup_weight, 0.0)
            + max(boll_setup_weight, 0.0)
        )
        recovery_weight_total = (
            max(rsi_recovery_weight, 0.0)
            + max(price_recovery_weight, 0.0)
            + max(macd_recovery_weight, 0.0)
        )

        for row in range(rows):
            for column in range(columns):
                close = matrix[row, column, IDX_CLOSE]
                ma200 = matrix[row, column, IDX_MA200]
                valid = not np.isnan(close) and not np.isnan(ma200)
                if valid:
                    observed[column] += 1
                if observed[column] < warmup_rows:
                    continue

                ma200_slope = matrix[row, column, IDX_MA200_SLOPE]
                adx = matrix[row, column, IDX_ADX]
                plus_di = matrix[row, column, IDX_PLUS_DI]
                minus_di = matrix[row, column, IDX_MINUS_DI]
                regime = (
                    valid
                    and close > ma200
                    and ma200_slope > 0.0
                    and adx >= adx_min
                    and plus_di > minus_di
                )

                setup_score = 0.0
                if setup_weight_total > 0.0:
                    if (
                        rsi_setup_weight > 0.0
                        and matrix[row, column, IDX_RSI_PCT]
                        <= rsi_pullback_pct
                    ):
                        setup_score += rsi_setup_weight
                    if (
                        deviation_setup_weight > 0.0
                        and matrix[row, column, IDX_DEVIATION]
                        <= deviation_pullback
                    ):
                        setup_score += deviation_setup_weight
                    if (
                        boll_setup_weight > 0.0
                        and matrix[row, column, IDX_BOLL_PCT_B]
                        <= boll_pullback
                    ):
                        setup_score += boll_setup_weight
                    setup_score /= setup_weight_total

                recovery_score = 0.0
                if recovery_weight_total > 0.0 and row > 0:
                    if (
                        rsi_recovery_weight > 0.0
                        and matrix[row, column, IDX_RSI]
                        > matrix[row - 1, column, IDX_RSI]
                    ):
                        recovery_score += rsi_recovery_weight
                    if (
                        price_recovery_weight > 0.0
                        and close >= matrix[row - 1, column, IDX_CLOSE]
                    ):
                        recovery_score += price_recovery_weight
                    if (
                        macd_recovery_weight > 0.0
                        and matrix[row, column, IDX_MACD_HIST]
                        > matrix[row - 1, column, IDX_MACD_HIST]
                    ):
                        recovery_score += macd_recovery_weight
                    recovery_score /= recovery_weight_total

                setup_ready = regime and setup_score >= setup_threshold
                recovery_ready = (
                    regime and recovery_score >= recovery_threshold
                )
                if not regime or not recovery_ready:
                    locked[column] = False

                rsi = matrix[row, column, IDX_RSI]
                deviation = matrix[row, column, IDX_DEVIATION]
                atr = matrix[row, column, IDX_ATR]
                if active[column]:
                    if not np.isnan(close):
                        if np.isnan(peak[column]):
                            peak[column] = close
                        else:
                            peak[column] = max(peak[column], close)
                    held_days = dates[row] - entry_date[column]
                    catastrophe = (
                        close < ma200
                        and not np.isnan(atr)
                        and atr > 0.0
                        and peak[column] - close > 3.0 * atr
                    )
                    repaired = rsi >= exit_rsi or deviation >= exit_deviation
                    if catastrophe:
                        force_exits[row, column] = True
                        active[column] = False
                        prepared[column] = False
                        streak[column] = 0
                    elif held_days >= 30 and (repaired or not regime):
                        exits[row, column] = True
                        active[column] = False
                        prepared[column] = False
                        streak[column] = 0

                if not active[column]:
                    if setup_ready:
                        prepared[column] = True
                    if prepared[column] and recovery_ready:
                        streak[column] += 1
                    else:
                        streak[column] = 0
                    if streak[column] >= 3 and not locked[column]:
                        entries[row, column] = True
                        active[column] = True
                        locked[column] = True
                        prepared[column] = False
                        streak[column] = 0
                        entry_date[column] = dates[row]
                        peak[column] = close
                        active_score[column] = max(
                            setup_score
                            + recovery_score
                            - setup_threshold
                            - recovery_threshold,
                            0.000001,
                        )
                if active[column]:
                    conviction[row, column] = active_score[column]
                    logical_active[row, column] = True
        return entries, exits, force_exits, conviction, logical_active

else:

    def _state_machine_numba(*args, **kwargs):
        raise NotImplementedError("numba required")

    def _target_weights_numba(*args, **kwargs):
        raise NotImplementedError("numba required")

    def _signal_state_from_matrix_numba(*args, **kwargs):
        raise NotImplementedError("numba required")


@register_strategy("regime_pullback")
class RegimePullbackStrategy(TradingStrategy):
    """Buy a confirmed recovery after a pullback in a healthy long trend."""

    name = "regime_pullback"
    label = "趋势回撤确认策略"
    description = "MA200上升环境中等待回撤，再以三日恢复确认择时"
    warmup_rows = 252
    manual_activation = True
    parameter_schema_id = "regime_pullback/1"
    window_state_scope = "train"

    def __init__(self):
        self._space = ParamSpace(
            [
                ParamDim("adx_min", 4, 15.0, 30.0),
                ParamDim("rsi_pullback_pct", 5, 0.15, 0.35),
                ParamDim("deviation_pullback", 4, -0.08, -0.02),
                ParamDim("boll_pullback", 4, 0.10, 0.40),
                ParamDim("rsi_setup_weight", 3, 0.0, 1.0),
                ParamDim("deviation_setup_weight", 3, 0.0, 1.0),
                ParamDim("boll_setup_weight", 3, 0.0, 1.0),
                ParamDim("setup_threshold", 4, 0.25, 0.75),
                ParamDim("rsi_recovery_weight", 3, 0.0, 1.0),
                ParamDim("price_recovery_weight", 3, 0.0, 1.0),
                ParamDim("macd_recovery_weight", 3, 0.0, 1.0),
                ParamDim("recovery_threshold", 4, 0.25, 0.75),
                ParamDim("exit_rsi", 4, 55.0, 70.0),
                ParamDim("exit_deviation", 4, 0.00, 0.06),
                ParamDim("per_symbol_cap", 3, 0.15, 0.25),
                ParamDim("total_exposure_cap", 3, 0.60, 1.00),
            ]
        )

    @property
    def param_space(self) -> ParamSpace:
        return self._space

    def _decode(self, params: Params, name: str) -> float:
        dim = next(item for item in self.param_space.dims if item.name == name)
        return params.decode(dim)

    def execution_params(self, params: Params) -> dict[str, float | int | str]:
        snapshot = dict(getattr(params, "execution_snapshot", {}) or {})
        if snapshot.get("model") == "target_weight":
            return snapshot
        return {
            "model": "target_weight",
            "per_symbol_cap": self._decode(params, "per_symbol_cap"),
            "total_exposure_cap": self._decode(params, "total_exposure_cap"),
            "min_holding_calendar_days": 30,
            "buy_price_model": "max_high_t_minus_1_t_t_plus_1",
            "sell_price_model": "trigger_day_low",
            "catastrophe_atr_multiple": 3.0,
        }

    def evaluate(
        self, params: Params, indicator_matrix: np.ndarray
    ) -> np.ndarray:
        matrix = np.asarray(indicator_matrix, dtype=np.float32)
        rsi_pct = matrix[:, :, IDX_RSI_PCT]
        deviation = matrix[:, :, IDX_DEVIATION]
        boll = matrix[:, :, IDX_BOLL_PCT_B]
        setup = _weighted_score(
            [
                rsi_pct <= self._decode(params, "rsi_pullback_pct"),
                deviation <= self._decode(params, "deviation_pullback"),
                boll <= self._decode(params, "boll_pullback"),
            ],
            [
                self._decode(params, "rsi_setup_weight"),
                self._decode(params, "deviation_setup_weight"),
                self._decode(params, "boll_setup_weight"),
            ],
        )
        rsi = matrix[:, :, IDX_RSI]
        close = matrix[:, :, IDX_CLOSE]
        macd_hist = matrix[:, :, IDX_MACD_HIST]
        rsi_rising = np.zeros_like(close, dtype=bool)
        price_stable = np.zeros_like(close, dtype=bool)
        macd_improving = np.zeros_like(close, dtype=bool)
        rsi_rising[1:] = rsi[1:] > rsi[:-1]
        price_stable[1:] = close[1:] >= close[:-1]
        macd_improving[1:] = macd_hist[1:] > macd_hist[:-1]
        recovery = _weighted_score(
            [rsi_rising, price_stable, macd_improving],
            [
                self._decode(params, "rsi_recovery_weight"),
                self._decode(params, "price_recovery_weight"),
                self._decode(params, "macd_recovery_weight"),
            ],
        )
        return np.stack([setup, recovery], axis=-1).astype(np.float32)

    def _state_machine(
        self, params: Params, market_data: StrategyMarketData
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        matrix = np.ascontiguousarray(
            market_data.indicator_matrix, dtype=np.float32
        )
        dates = (
            np.asarray(market_data.date_ordinals, dtype=np.int64)
            if market_data.date_ordinals is not None
            else _calendar_ordinals(market_data.dates, matrix.shape[0])
        )
        return _signal_state_from_matrix_numba(
            matrix,
            np.ascontiguousarray(dates, dtype=np.int64),
            int(self.warmup_rows),
            float(self._decode(params, "adx_min")),
            float(self._decode(params, "rsi_pullback_pct")),
            float(self._decode(params, "deviation_pullback")),
            float(self._decode(params, "boll_pullback")),
            float(self._decode(params, "rsi_setup_weight")),
            float(self._decode(params, "deviation_setup_weight")),
            float(self._decode(params, "boll_setup_weight")),
            float(self._decode(params, "setup_threshold")),
            float(self._decode(params, "rsi_recovery_weight")),
            float(self._decode(params, "price_recovery_weight")),
            float(self._decode(params, "macd_recovery_weight")),
            float(self._decode(params, "recovery_threshold")),
            float(self._decode(params, "exit_rsi")),
            float(self._decode(params, "exit_deviation")),
        )

    def make_signals(
        self, params: Params, market_data: StrategyMarketData
    ) -> TradePlan:
        if not isinstance(market_data, StrategyMarketData):
            raise TypeError("make_signals requires StrategyMarketData")
        entries, exits, force_exits, conviction, active = self._state_machine(
            params, market_data
        )
        execution = self.execution_params(params)
        weights = _target_weights_numba(
            np.ascontiguousarray(conviction, dtype=np.float32),
            np.ascontiguousarray(active, dtype=np.bool_),
            float(execution["per_symbol_cap"]),
            float(execution["total_exposure_cap"]),
        )
        sell_signals = exits | force_exits
        date_ordinals = (
            np.asarray(market_data.date_ordinals, dtype=np.int64)
            if market_data.date_ordinals is not None
            else _calendar_ordinals(market_data.dates, len(entries))
        )
        return TradePlan(
            buy_signals=entries,
            sell_signals=sell_signals,
            buy_priority=np.where(entries, conviction, -np.inf).astype(np.float32),
            sell_priority=np.where(sell_signals, 1.0, -np.inf).astype(np.float32),
            buy_cash_limit=0.0,
            sell_cash_limit=0.0,
            warmup_rows=self.warmup_rows,
            dates=list(market_data.dates),
            symbols=list(market_data.symbols),
            execution=dict(execution),
            strategy_metadata={
                "strategy_id": self.name,
                "strategy_label": self.label,
                "parameter_schema": self.parameter_schema_id,
                "parameters": dict(params.values),
            },
            entry_events=entries,
            exit_events=exits,
            force_exit_signals=force_exits,
            conviction=conviction,
            target_weights=weights,
            risk_atr=np.asarray(
                market_data.indicator_matrix[:, :, IDX_ATR], dtype=np.float32
            ),
            date_ordinals=np.asarray(date_ordinals, dtype=np.int64),
        )

    def _make_signal_arrays(
        self, params: Params, indicator_matrix: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        rows, columns = indicator_matrix.shape[:2]
        market = StrategyMarketData(
            indicator_matrix=indicator_matrix,
            dates=[str(index) for index in range(rows)],
            symbols=[str(index) for index in range(columns)],
            prices=indicator_matrix[:, :, IDX_CLOSE],
        )
        entries, exits, force_exits, _, _ = self._state_machine(params, market)
        return entries, exits | force_exits

    def to_human_readable(self, params: Params) -> str:
        execution = self.execution_params(params)
        return (
            "趋势回撤确认策略: MA200上升且+DMI占优；"
            f"ADX≥{self._decode(params, 'adx_min'):.0f}；"
            "回撤后恢复连续3日触发一次；"
            f"单标的≤{float(execution['per_symbol_cap']):.0%}，"
            f"总仓位≤{float(execution['total_exposure_cap']):.0%}"
        )
