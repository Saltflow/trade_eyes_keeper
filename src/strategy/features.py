"""Causal, cross-instrument technical feature contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from ..search.contracts import stable_hash


Transform = Callable[[np.ndarray, np.ndarray], np.ndarray]


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    index: int
    role: str
    redundancy_group: str
    direction: int
    lookback_rows: int
    applicable_markets: tuple[str, ...]
    transform_id: str
    transform: Transform

    def contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "index": self.index,
            "role": self.role,
            "redundancy_group": self.redundancy_group,
            "direction": self.direction,
            "lookback_rows": self.lookback_rows,
            "future_rows": 0,
            "applicable_markets": list(self.applicable_markets),
            "transform": self.transform_id,
        }


class FeatureRegistry:
    def __init__(self) -> None:
        self._features: dict[str, FeatureSpec] = {}

    def register(self, spec: FeatureSpec) -> None:
        if spec.name in self._features:
            raise ValueError(f"duplicate feature: {spec.name}")
        if spec.index in {item.index for item in self._features.values()}:
            raise ValueError(f"duplicate feature index: {spec.index}")
        if spec.direction not in {-1, 1}:
            raise ValueError(f"{spec.name}: direction must be -1 or 1")
        self._features[spec.name] = spec

    @property
    def features(self) -> tuple[FeatureSpec, ...]:
        return tuple(sorted(self._features.values(), key=lambda item: item.index))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self.features)

    @property
    def hash(self) -> str:
        return stable_hash([feature.contract() for feature in self.features])

    def transform(
        self, indicator_matrix: np.ndarray, market: str = "a_share"
    ) -> tuple[np.ndarray, np.ndarray]:
        raw = np.asarray(indicator_matrix, dtype=np.float32)
        if raw.ndim != 3 or raw.shape[2] < len(self.features):
            raise ValueError("indicator matrix does not contain the 22-column contract")
        close = raw[:, :, 0]
        output = np.full((*raw.shape[:2], len(self.features)), np.nan, dtype=np.float32)
        mask = np.zeros_like(output, dtype=bool)
        for target, feature in enumerate(self.features):
            if market not in feature.applicable_markets:
                continue
            values = np.asarray(
                feature.transform(raw[:, :, feature.index], close), dtype=np.float32
            )
            values *= float(feature.direction)
            output[:, :, target] = values
            mask[:, :, target] = np.isfinite(values)
        return output, mask


def _clip(value, limit=1.0):
    return np.clip(value, -limit, limit)


def _identity_unit(value, _close):
    return _clip(value)


def _log_return(value, _close):
    result = np.full_like(value, np.nan, dtype=np.float32)
    valid = (value[1:] > 0) & (value[:-1] > 0)
    result[0] = 0.0
    result[1:] = np.where(
        valid,
        _clip(np.log(value[1:] / value[:-1]) / 0.10),
        np.nan,
    )
    return result


def _relative_to_close(value, close):
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clip((close - value) / np.maximum(np.abs(value), 1e-6) / 0.20)


def _deviation(value, _close):
    return np.tanh(value / 0.10)


def _oscillator_100(value, _close):
    return _clip((value - 50.0) / 50.0)


def _macd_scale(value, close):
    scale = np.maximum(np.abs(close) * 0.02, 1e-6)
    return np.tanh(value / scale)


def _log_volume_ratio(value, _close):
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clip(np.log(np.maximum(value, 1e-6)) / np.log(4.0))


def _bollinger(value, _close):
    return _clip(2.0 * value - 1.0)


def _adx(value, _close):
    return _clip((value - 20.0) / 40.0)


def _atr(value, close):
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clip(value / np.maximum(np.abs(close), 1e-6) / 0.10)


def _percentile(value, _close):
    return _clip(2.0 * value - 1.0)


def _high_range(value, close):
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clip((value - close) / np.maximum(np.abs(close), 1e-6) / 0.10)


def _low_range(value, close):
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clip((close - value) / np.maximum(np.abs(close), 1e-6) / 0.10)


def _slope(value, _close):
    return np.tanh(value / 0.03)


def _directional_indicator(value, _close):
    return _clip((value - 25.0) / 25.0)


ALL_MARKETS = ("a_share", "hk", "us")


def _build_default_registry() -> FeatureRegistry:
    registry = FeatureRegistry()
    definitions = (
        ("close", "return", "price", 1, 1, "log_return/1", _log_return),
        (
            "ma60",
            "trend",
            "moving_average",
            1,
            60,
            "close_over_ma/1",
            _relative_to_close,
        ),
        (
            "deviation",
            "pullback",
            "moving_average",
            -1,
            60,
            "tanh_deviation/1",
            _deviation,
        ),
        ("rsi", "pullback", "oscillator", -1, 14, "centered_100/1", _oscillator_100),
        ("macd", "trend", "macd", 1, 35, "price_scaled/1", _macd_scale),
        ("macd_signal", "trend", "macd", 1, 44, "price_scaled/1", _macd_scale),
        ("macd_hist", "recovery", "macd", 1, 44, "price_scaled/1", _macd_scale),
        (
            "vol_ratio",
            "participation",
            "volume",
            1,
            20,
            "log_ratio/1",
            _log_volume_ratio,
        ),
        (
            "boll_pct_b",
            "location",
            "volatility_band",
            -1,
            20,
            "centered_unit/1",
            _bollinger,
        ),
        ("adx", "trend_quality", "directional", 1, 14, "adx_centered/1", _adx),
        ("atr", "risk", "volatility", -1, 14, "atr_over_close/1", _atr),
        (
            "adx_pct",
            "trend_quality",
            "percentile",
            1,
            252,
            "centered_percentile/1",
            _percentile,
        ),
        (
            "rsi_pct",
            "momentum",
            "percentile",
            -1,
            252,
            "centered_percentile/1",
            _percentile,
        ),
        (
            "deviation_pct",
            "pullback",
            "percentile",
            -1,
            252,
            "centered_percentile/1",
            _percentile,
        ),
        (
            "vol_ratio_pct",
            "participation",
            "percentile",
            1,
            252,
            "centered_percentile/1",
            _percentile,
        ),
        (
            "ma200_dev_pct",
            "trend",
            "percentile",
            1,
            252,
            "centered_percentile/1",
            _percentile,
        ),
        (
            "high",
            "price_action",
            "intraday_range",
            -1,
            1,
            "high_over_close/1",
            _high_range,
        ),
        ("low", "price_action", "intraday_range", 1, 1, "close_over_low/1", _low_range),
        (
            "ma200",
            "trend",
            "moving_average",
            1,
            200,
            "close_over_ma/1",
            _relative_to_close,
        ),
        ("ma200_slope", "trend", "moving_average", 1, 220, "tanh_slope/1", _slope),
        (
            "plus_di",
            "direction",
            "directional",
            1,
            14,
            "di_centered/1",
            _directional_indicator,
        ),
        (
            "minus_di",
            "direction",
            "directional",
            -1,
            14,
            "di_centered/1",
            _directional_indicator,
        ),
    )
    for index, definition in enumerate(definitions):
        name, role, group, direction, lookback, transform_id, transform = definition
        registry.register(
            FeatureSpec(
                name=name,
                index=index,
                role=role,
                redundancy_group=group,
                direction=direction,
                lookback_rows=lookback,
                applicable_markets=ALL_MARKETS,
                transform_id=transform_id,
                transform=transform,
            )
        )
    return registry


TECHNICAL_FEATURES = _build_default_registry()
