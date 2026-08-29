"""Shared fill-price policy for every strategy and tradable benchmark."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class CorporateActionSlice:
    """Window-local corporate actions applied before that day's orders.

    ``cash_dividends`` is cash received per share held at the start of the
    session.  ``share_multipliers`` is the post-action share count divided by
    the pre-action count (bonus shares and splits).  Keeping these arrays next
    to the execution prices makes scalar, batch and target-weight simulation
    consume the same contract.
    """

    cash_dividends: np.ndarray
    share_multipliers: np.ndarray

    def __post_init__(self) -> None:
        cash = np.asarray(self.cash_dividends, dtype=np.float64)
        multipliers = np.asarray(self.share_multipliers, dtype=np.float64)
        if cash.ndim == 1:
            cash = cash.reshape(-1, 1)
        if multipliers.ndim == 1:
            multipliers = multipliers.reshape(-1, 1)
        if cash.ndim != 2 or multipliers.ndim != 2:
            raise ValueError("corporate action arrays must be two-dimensional")
        if cash.shape != multipliers.shape:
            raise ValueError("corporate action arrays must have the same shape")
        if np.any(~np.isfinite(cash)) or np.any(cash < 0.0):
            raise ValueError("cash dividends must be finite and non-negative")
        if np.any(~np.isfinite(multipliers)) or np.any(multipliers <= 0.0):
            raise ValueError("share multipliers must be finite and positive")
        object.__setattr__(self, "cash_dividends", cash)
        object.__setattr__(self, "share_multipliers", multipliers)

    @classmethod
    def empty(cls, rows: int, columns: int) -> "CorporateActionSlice":
        shape = (max(0, int(rows)), max(0, int(columns)))
        return cls(
            cash_dividends=np.zeros(shape, dtype=np.float64),
            share_multipliers=np.ones(shape, dtype=np.float64),
        )

    def sliced(self, start: int, end: int) -> "CorporateActionSlice":
        return CorporateActionSlice(
            cash_dividends=self.cash_dividends[start:end].copy(),
            share_multipliers=self.share_multipliers[start:end].copy(),
        )

    def scaled(self, factor: float) -> "CorporateActionSlice":
        """Scale cash amounts with the execution currency, not share counts."""
        return CorporateActionSlice(
            cash_dividends=self.cash_dividends * float(factor),
            share_multipliers=self.share_multipliers.copy(),
        )


def build_corporate_action_schedule(
    actions: Iterable[object] | None,
    dates: Iterable[object],
    symbols: Iterable[str],
) -> CorporateActionSlice:
    """Convert dated ``CorporateAction`` objects into a strict matrix.

    The ex-date is applied on the first available trading row on or after the
    ex-date.  Unsupported rights issues or incomplete share-changing actions
    fail closed instead of being silently ignored.
    """
    date_values = np.asarray(
        [np.datetime64(str(value)[:10], "D") for value in dates],
        dtype="datetime64[D]",
    )
    symbol_values = [str(value) for value in symbols]
    result = CorporateActionSlice.empty(len(date_values), len(symbol_values))
    cash = result.cash_dividends.copy()
    multipliers = result.share_multipliers.copy()
    if not actions:
        return result

    symbol_index = {symbol: index for index, symbol in enumerate(symbol_values)}
    for action in actions:
        code = str(getattr(action, "code", ""))
        if code not in symbol_index:
            raise ValueError(f"corporate action code is outside the market universe: {code}")
        ex_date = getattr(action, "ex_date", None)
        if ex_date is None:
            raise ValueError(f"corporate action {code} has no ex-date")
        ex_day = np.datetime64(str(ex_date)[:10], "D")
        row = int(np.searchsorted(date_values, ex_day, side="left"))
        if row >= len(date_values):
            # Actions after the requested history do not affect this slice.
            continue
        if getattr(action, "rights_price", None) is not None:
            raise ValueError(
                f"rights issue is not supported by the execution contract: {code} {ex_date}"
            )
        cash_value = getattr(action, "cash_per_share", None)
        multiplier_value = getattr(action, "share_multiplier", None)
        raw_factor = getattr(action, "raw_adjustment_factor", None)
        if cash_value is None and multiplier_value is None:
            if raw_factor is not None and not np.isclose(float(raw_factor), 1.0):
                raise ValueError(
                    f"unresolved corporate action factor for {code} {ex_date}"
                )
            raise ValueError(
                f"corporate action has no cash or share effect: {code} {ex_date}"
            )
        if cash_value is not None:
            cash_value = float(cash_value)
            if not np.isfinite(cash_value) or cash_value < 0.0:
                raise ValueError(f"invalid cash dividend for {code} {ex_date}")
            cash[row, symbol_index[code]] += cash_value
        if multiplier_value is not None:
            multiplier_value = float(multiplier_value)
            if not np.isfinite(multiplier_value) or multiplier_value <= 0.0:
                raise ValueError(f"invalid share multiplier for {code} {ex_date}")
            multipliers[row, symbol_index[code]] *= multiplier_value
    return CorporateActionSlice(cash, multipliers)


def _price_matrix(
    values: np.ndarray | None,
    name: str,
    fallback: np.ndarray | None = None,
) -> np.ndarray:
    source = fallback if values is None else values
    if source is None:
        raise ValueError(f"{name} is required")
    matrix = np.asarray(source, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    if matrix.ndim != 2:
        raise ValueError(f"{name} must be a one- or two-dimensional array")
    return matrix


@dataclass(frozen=True)
class ExecutionPriceSlice:
    """Window-local valuation and fill matrices from one shared policy."""

    valuation_prices: np.ndarray
    buy_prices: np.ndarray
    sell_prices: np.ndarray
    tradable: np.ndarray
    corporate_actions: CorporateActionSlice | None = field(default=None)

    def __post_init__(self) -> None:
        shape = np.asarray(self.valuation_prices).shape
        if len(shape) == 1:
            shape = (shape[0], 1)
        for name in ("buy_prices", "sell_prices", "tradable"):
            value_shape = np.asarray(getattr(self, name)).shape
            if len(value_shape) == 1:
                value_shape = (value_shape[0], 1)
            if value_shape != shape:
                raise ValueError(f"{name} and valuation_prices must have the same shape")
        if self.corporate_actions is not None:
            if self.corporate_actions.cash_dividends.shape != shape:
                raise ValueError("corporate actions and prices must have the same shape")

    def scaled(self, factor: float) -> "ExecutionPriceSlice":
        scale = float(factor)
        return ExecutionPriceSlice(
            valuation_prices=self.valuation_prices * scale,
            buy_prices=self.buy_prices * scale,
            sell_prices=self.sell_prices * scale,
            tradable=self.tradable.copy(),
            corporate_actions=(
                self.corporate_actions.scaled(scale)
                if self.corporate_actions is not None
                else None
            ),
        )


@dataclass(frozen=True)
class FillPricePolicy:
    """Pessimistic execution policy, isolated from strategy decisions.

    A buy fills at the maximum high across t-1/t/t+1.  The next-session high
    is an execution-delay stress input only; it is never available to a
    strategy.  A sell fills at the signal-session low and NAV always uses the
    session close.  The final row of every evaluation slice is pending because
    its t+1 high is outside that evaluation period.
    """

    name: str = "three_session_high_day_low"

    def buy_prices(self, high_prices: np.ndarray) -> np.ndarray:
        highs = _price_matrix(high_prices, "high_prices")
        rows, columns = highs.shape
        result = np.full((rows, columns), np.nan, dtype=np.float64)
        for row in range(rows - 1):
            previous = highs[row - 1] if row else highs[row]
            current = highs[row]
            following = highs[row + 1]
            valid = (
                np.isfinite(previous)
                & np.isfinite(current)
                & np.isfinite(following)
                & (previous > 0)
                & (current > 0)
                & (following > 0)
            )
            result[row, valid] = np.maximum(
                np.maximum(previous[valid], current[valid]), following[valid]
            )
        return result

    def build(
        self,
        close_prices: np.ndarray,
        high_prices: np.ndarray | None = None,
        low_prices: np.ndarray | None = None,
        corporate_actions: CorporateActionSlice | None = None,
        *,
        start: int = 0,
        end: int | None = None,
    ) -> ExecutionPriceSlice:
        closes = _price_matrix(close_prices, "close_prices")
        highs = _price_matrix(high_prices, "high_prices", closes)
        lows = _price_matrix(low_prices, "low_prices", closes)
        if highs.shape != closes.shape or lows.shape != closes.shape:
            raise ValueError("close/high/low matrices must have the same shape")

        rows = len(closes)
        window_start = max(0, int(start))
        window_end = rows if end is None else min(rows, int(end))
        if window_end < window_start:
            raise ValueError("execution slice end must not precede start")

        valuation = closes[window_start:window_end].copy()
        buys = self.buy_prices(highs)[window_start:window_end].copy()
        sells = lows[window_start:window_end].copy()
        if len(buys):
            buys[-1] = np.nan
        tradable = np.isfinite(valuation) & (valuation > 0)
        if corporate_actions is None:
            action_slice = CorporateActionSlice.empty(rows, closes.shape[1]).sliced(
                window_start, window_end
            )
        else:
            if corporate_actions.cash_dividends.shape != closes.shape:
                raise ValueError("corporate actions and close prices must have the same shape")
            action_slice = corporate_actions.sliced(window_start, window_end)
        return ExecutionPriceSlice(
            valuation_prices=valuation,
            buy_prices=buys,
            sell_prices=sells,
            tradable=tradable,
            corporate_actions=action_slice,
        )


DEFAULT_FILL_PRICE_POLICY = FillPricePolicy()
