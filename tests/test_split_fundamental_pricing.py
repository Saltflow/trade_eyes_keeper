from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding import (
    PRICING_FEATURE_NAMES,
    CompanyExposureEncoder,
    FundamentalPricingDataset,
    FundamentalPricingSnapshot,
    SplitPricingConfig,
    SplitPricingEvaluator,
)


def _synthetic_dataset() -> FundamentalPricingDataset:
    generator = np.random.default_rng(20260819)
    symbols = [f"{index:06d}" for index in range(9)]
    quarter_dates = [
        date(2021, 3, 31) + timedelta(days=91 * index)
        for index in range(18)
    ]
    row_count = len(symbols) * len(quarter_dates)
    values = generator.normal(
        size=(row_count, len(PRICING_FEATURE_NAMES))
    )
    persistent = generator.normal(
        size=(len(symbols), len(PRICING_FEATURE_NAMES))
    )
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


def test_split_evaluator_is_causal_and_keeps_all_mandatory_baselines():
    dataset = _synthetic_dataset()
    report = SplitPricingEvaluator(_config()).run(dataset)

    assert report.dataset["leakage_violations"] == 0
    assert report.acceptance["framework_ready"]
    assert report.acceptance["mandatory_baselines_complete"]
    assert report.acceptance["company_embedding_excludes_market_pricing"]
    required = set(report.acceptance["mandatory_baselines"])
    required.add(report.candidate_model_id)
    assert all(required <= row["scores"].keys() for row in report.predictions)
    assert all(
        state["realized_through"] < state["as_of"]
        for state in report.factor_price_states
    )


def test_market_prices_are_signed_and_not_simplex_weights():
    report = SplitPricingEvaluator(_config()).run(_synthetic_dataset())
    prices = np.asarray([
        state["factor_prices"]
        for state in report.factor_price_states
        if state["model_id"] == report.candidate_model_id
    ])

    assert np.any(prices < 0.0)
    assert np.any(prices > 0.0)
    assert not np.allclose(prices.sum(axis=1), 1.0)


def test_current_company_exposure_and_market_state_are_separate():
    dataset = _synthetic_dataset()
    latest = dataset.rows_on(max(dataset.feature_dates))
    snapshot = FundamentalPricingSnapshot(
        feature_date=max(dataset.label_end_dates) + timedelta(days=1),
        symbols=tuple(
            symbol
            for symbol, selected in zip(dataset.symbols, latest)
            if selected
        ),
        feature_names=dataset.feature_names,
        values=dataset.values[latest],
        availability_mask=dataset.availability_mask[latest],
    ).validate()
    report = SplitPricingEvaluator(_config()).run(
        dataset, inference_snapshot=snapshot
    )

    assert len(report.latest_company_exposures) == len(snapshot.symbols)
    assert all(
        not row["company_embedding_contains_market_state"]
        for row in report.latest_company_exposures
    )
    assert all(
        len(row["stable_company_exposure"]) == 4
        for row in report.latest_company_exposures
    )


def test_production_gate_requires_beating_the_strongest_baseline():
    report = SplitPricingEvaluator(
        _config(
            production_minimum_delta_rank_ic=10.0,
            production_minimum_win_rate=1.0,
        )
    ).run(_synthetic_dataset())

    assert report.paired_comparison["strongest_baseline"] is not None
    assert not report.acceptance["candidate_beats_strongest_baseline"]
    assert not report.acceptance["production_ready"]
