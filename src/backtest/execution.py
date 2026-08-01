"""Shared fill-price policy for every strategy and tradable benchmark."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


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

    def scaled(self, factor: float) -> "ExecutionPriceSlice":
        scale = float(factor)
        return ExecutionPriceSlice(
            valuation_prices=self.valuation_prices * scale,
            buy_prices=self.buy_prices * scale,
            sell_prices=self.sell_prices * scale,
            tradable=self.tradable.copy(),
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
        return ExecutionPriceSlice(
            valuation_prices=valuation,
            buy_prices=buys,
            sell_prices=sells,
            tradable=tradable,
        )


DEFAULT_FILL_PRICE_POLICY = FillPricePolicy()
