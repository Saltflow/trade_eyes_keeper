"""Builder signals must be invariant to data appended after a decision date."""

from __future__ import annotations

import numpy as np

from src.backtest.engine import IDX_CLOSE
from src.strategy.plugins.builder import _build_absolute_discount


def _indicators(close: list[float]) -> np.ndarray:
    result = np.zeros((len(close), 1, 22), dtype=np.float32)
    result[:, 0, IDX_CLOSE] = close
    return result


def test_absolute_discount_uses_only_historical_highs():
    prefix = _indicators([100.0, 90.0, 80.0, 70.0])
    extended = _indicators([100.0, 90.0, 80.0, 70.0, 500.0])

    prefix_condition, prefix_reset = _build_absolute_discount(prefix, 0.0)
    full_condition, full_reset = _build_absolute_discount(extended, 0.0)

    assert np.array_equal(prefix_condition, full_condition[: len(prefix)])
    assert np.array_equal(prefix_reset, full_reset[: len(prefix)])
    assert prefix_condition[:, 0].tolist() == [False, False, False, True]
