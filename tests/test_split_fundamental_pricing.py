from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding import (
    PRICING_FEATURE_NAMES,
    CompanyExposureEncoder,
    FundamentalPricingDataset,
    SplitPricingConfig,
)


def _synthetic_dataset() -> FundamentalPricingDataset:
    generator = np.random.default_rng(20260819)
    symbols = [f"{index:06d}" for index in range(9)]
    quarter_dates = [
        date(2021, 3, 31) + timedelta(days=91 * index) for index in range(18)
    ]
    row_count = len(symbols) * len(quarter_dates)
    values = generator.normal(size=(row_count, len(PRICING_FEATURE_NAMES)))
    persistent = generator.normal(size=(len(symbols), len(PRICING_FEATURE_NAMES)))
    feature_dates = []
    label_dates = []
    row_symbols = []
    returns = []
    excess = []
    for date_index, feature_date in enumerate(quarter_dates):
        quarter_return = []
        for symbol_index, symbol in enumerate(symbols):
            row = date_index * len(symbols) + symbol_index
            values[row] = 0.8 * persistent[symbol_index] + 0.2 * values[row]
            values[row, -1] = 80.0 + 10.0 * (symbol_index % 4)
            market_price = (-1.0) ** date_index
            quarter_return.append(
                0.035 * values[row, 0]
                + market_price * 0.025 * values[row, 10]
                + generator.normal(scale=0.02)
            )
            feature_dates.append(feature_date)
            label_dates.append(feature_date + timedelta(days=80))
            row_symbols.append(symbol)
        centered = np.asarray(quarter_return) - np.mean(quarter_return)
        excess.extend(centered)
        returns.extend(centered + 0.02)
    mask = generator.random(values.shape) > 0.08
    mask[:, -1] = True
    values[~mask] = np.nan
    return FundamentalPricingDataset(
        feature_dates=tuple(feature_dates),
        label_end_dates=tuple(label_dates),
        symbols=tuple(row_symbols),
        feature_names=PRICING_FEATURE_NAMES,
        values=values,
        availability_mask=mask,
        forward_returns=np.asarray(returns),
        excess_returns=np.asarray(excess),
        metadata={"contract": "split-synthetic-test"},
    ).validate()


def _config(**changes) -> SplitPricingConfig:
    return replace(
        SplitPricingConfig(),
        minimum_train_rows=45,
        minimum_train_dates=6,
        production_minimum_symbols=5,
        **changes,
    )


def test_company_exposure_is_label_free_and_age_only_changes_confidence():
    dataset = _synthetic_dataset()
    train = dataset.rows_before(dataset.feature_dates[9 * 9])
    encoder = CompanyExposureEncoder(dataset.feature_names, _config()).fit(
        dataset.values[train],
        dataset.availability_mask[train],
    )
    original = encoder.transform(
        tuple(np.asarray(dataset.feature_dates, dtype=object)[train]),
        tuple(np.asarray(dataset.symbols, dtype=object)[train]),
        dataset.values[train],
        dataset.availability_mask[train],
    )
    changed = dataset.values[train].copy()
    age_index = dataset.feature_names.index("financial_age_days")
    changed[:, age_index] += 2000.0
    stale = encoder.transform(
        original.feature_dates,
        original.symbols,
        changed,
        dataset.availability_mask[train],
    )

    assert np.array_equal(original.raw_exposures, stale.raw_exposures)
    assert np.all(stale.availability_confidence <= original.availability_confidence)
