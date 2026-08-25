"""Stable-semantics company exposure construction."""

from __future__ import annotations

from datetime import date
from typing import Iterable

import numpy as np
from scipy.stats import rankdata

from .split_api import (
    FACTOR_FEATURE_DIRECTIONS,
    FACTOR_NAMES,
    CompanyExposureBatch,
    SplitPricingConfig,
)


class RobustFeatureTransformer:
    """Causal robust scaling with explicit availability preservation."""

    def __init__(self, feature_names: Iterable[str], winsor_limit: float = 4.0):
        self.feature_names = tuple(feature_names)
        self.winsor_limit = float(winsor_limit)
        self.median = np.zeros(len(self.feature_names), dtype=np.float64)
        self.scale = np.ones(len(self.feature_names), dtype=np.float64)
        self._fitted = False

    def fit(
        self,
        values: np.ndarray,
        availability_mask: np.ndarray,
    ) -> "RobustFeatureTransformer":
        for index in range(values.shape[1]):
            observed = values[availability_mask[:, index], index]
            observed = observed[np.isfinite(observed)]
            if len(observed) == 0:
                continue
            self.median[index] = float(np.median(observed))
            q25, q75 = np.quantile(observed, [0.25, 0.75])
            robust = float(q75 - q25)
            if robust <= 1e-12:
                mad = float(
                    np.median(np.abs(observed - self.median[index]))
                )
                robust = mad * 1.4826
            if robust > 1e-12:
                self.scale[index] = robust
        self._fitted = True
        return self

    def transform(
        self,
        values: np.ndarray,
        availability_mask: np.ndarray,
    ) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("feature transformer is not fitted")
        filled = np.where(availability_mask, values, self.median)
        transformed = (filled - self.median) / self.scale
        return np.clip(
            transformed,
            -self.winsor_limit,
            self.winsor_limit,
        )


class CompanyExposureEncoder:
    """Map raw fundamentals to fixed-meaning company factor exposures."""

    def __init__(
        self,
        feature_names: Iterable[str],
        config: SplitPricingConfig | None = None,
    ):
        self.feature_names = tuple(feature_names)
        self.config = config or SplitPricingConfig()
        feature_index = {
            name: index for index, name in enumerate(self.feature_names)
        }
        missing = [
            feature
            for mapping in FACTOR_FEATURE_DIRECTIONS.values()
            for feature in mapping
            if feature not in feature_index
        ]
        if missing:
            raise ValueError(
                "company exposure input is missing features: "
                + ", ".join(sorted(set(missing)))
            )
        self.feature_index = feature_index
        self.transformer = RobustFeatureTransformer(
            self.feature_names,
            self.config.winsor_limit,
        )
        self._fitted = False

    def fit(
        self,
        values: np.ndarray,
        availability_mask: np.ndarray,
    ) -> "CompanyExposureEncoder":
        self.transformer.fit(values, availability_mask)
        self._fitted = True
        return self

    def _freshness_confidence(
        self,
        values: np.ndarray,
        availability_mask: np.ndarray,
    ) -> np.ndarray:
        if "financial_age_days" not in self.feature_index:
            return np.ones(len(values), dtype=np.float64)
        index = self.feature_index["financial_age_days"]
        observed = availability_mask[:, index] & np.isfinite(values[:, index])
        age = np.maximum(values[:, index], 0.0)
        excess_age = np.maximum(
            age - self.config.stale_after_days,
            0.0,
        )
        confidence = np.exp(
            -np.log(2.0)
            * excess_age
            / max(self.config.freshness_half_life_days, 1.0)
        )
        return np.where(observed, confidence, 1.0)

    @staticmethod
    def _cross_sectional_rank(
        feature_dates: tuple[date, ...],
        raw: np.ndarray,
        confidence: np.ndarray,
    ) -> np.ndarray:
        result = np.zeros_like(raw, dtype=np.float64)
        date_array = np.asarray(feature_dates, dtype=object)
        for current_date in sorted(set(feature_dates)):
            selected = date_array == current_date
            for factor_index in range(raw.shape[1]):
                usable = selected & (confidence[:, factor_index] > 0.0)
                count = int(usable.sum())
                if count <= 1:
                    continue
                ranks = rankdata(raw[usable, factor_index], method="average")
                centered = (
                    ranks - (count + 1.0) / 2.0
                ) / ((count - 1.0) / 2.0)
                result[usable, factor_index] = (
                    centered * confidence[usable, factor_index]
                )
        return result

    def transform(
        self,
        feature_dates: Iterable[date],
        symbols: Iterable[str],
        values: np.ndarray,
        availability_mask: np.ndarray,
    ) -> CompanyExposureBatch:
        if not self._fitted:
            raise RuntimeError("company exposure encoder is not fitted")
        dates = tuple(feature_dates)
        symbol_tuple = tuple(str(symbol) for symbol in symbols)
        transformed = self.transformer.transform(values, availability_mask)
        freshness = self._freshness_confidence(values, availability_mask)
        raw = np.zeros((len(values), len(FACTOR_NAMES)), dtype=np.float64)
        confidence = np.zeros_like(raw)

        for factor_index, factor_name in enumerate(FACTOR_NAMES):
            directions = FACTOR_FEATURE_DIRECTIONS[factor_name]
            indices = np.asarray(
                [self.feature_index[name] for name in directions],
                dtype=int,
            )
            signs = np.asarray(list(directions.values()), dtype=np.float64)
            available = availability_mask[:, indices]
            signed = transformed[:, indices] * signs[None, :]
            counts = available.sum(axis=1)
            raw[:, factor_index] = np.divide(
                np.where(available, signed, 0.0).sum(axis=1),
                counts,
                out=np.zeros(len(values), dtype=np.float64),
                where=counts > 0,
            )
            confidence[:, factor_index] = (
                counts / len(indices)
            ) * freshness

        ranking = self._cross_sectional_rank(dates, raw, confidence)
        return CompanyExposureBatch(
            feature_dates=dates,
            symbols=symbol_tuple,
            factor_names=FACTOR_NAMES,
            raw_exposures=raw,
            ranking_exposures=ranking,
            availability_confidence=confidence,
            metadata={
                "contract": "stable-company-exposure-1",
                "financial_age_semantics": "reliability_only",
                "economic_directions_fixed": True,
            },
        ).validate()
