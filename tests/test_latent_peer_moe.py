from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from src.fundamental_embedding.api import (
    PRICING_FEATURE_NAMES,
    FundamentalPricingDataset,
    FundamentalPricingSnapshot,
)
from src.fundamental_embedding.latent_peer_moe import (
    LatentPeerMoE,
    LatentPeerMoEConfig,
    LatentPeerWalkForwardEvaluator,
)


def _dataset() -> FundamentalPricingDataset:
    generator = np.random.default_rng(20260819)
    symbols = tuple(f"{index:06d}" for index in range(12))
    dates = tuple(
        date(2021, 3, 31) + timedelta(days=91 * index)
        for index in range(18)
    )
    rows = len(symbols) * len(dates)
    values = generator.normal(
        scale=0.3, size=(rows, len(PRICING_FEATURE_NAMES))
    )
    mask = np.ones_like(values, dtype=bool)
    feature_index = {
        name: index for index, name in enumerate(PRICING_FEATURE_NAMES)
    }
    row_dates = []
    label_dates = []
    row_symbols = []
    excess = []
    returns = []
    for date_index, feature_date in enumerate(dates):
        quarter = []
        for symbol_index, symbol in enumerate(symbols):
            row = date_index * len(symbols) + symbol_index
            latent = symbol_index % 3
            quality = (symbol_index - 5.5) / 4.0
            growth = np.sin(symbol_index) + 0.05 * date_index
            cash = np.cos(symbol_index) - 0.03 * date_index
            values[row, feature_index["roe_ttm"]] = quality
            values[row, feature_index["net_margin"]] = quality * 0.8
            values[row, feature_index["revenue_yoy"]] = growth
            values[row, feature_index["net_income_yoy"]] = growth * 0.7
            values[row, feature_index["fcf_margin"]] = cash
            values[row, feature_index["cash_conversion"]] = cash * 0.6
            values[row, feature_index["earnings_yield"]] = (
                0.8 * quality if latent == 0 else -0.4 * growth
            )
            values[row, feature_index["book_yield"]] = (
                0.7 * quality if latent == 0 else 0.5 * cash
            )
            values[row, feature_index["fcf_yield"]] = (
                0.9 * cash if latent == 1 else -0.2 * quality
            )
            values[row, feature_index["dividend_yield"]] = (
                0.6 * cash if latent == 1 else 0.2 * quality
            )
            signal = (
                quality
                if latent == 0
                else cash if latent == 1 else growth
            )
            quarter.append(signal + generator.normal(scale=0.08))
            row_dates.append(feature_date)
            label_dates.append(feature_date + timedelta(days=80))
            row_symbols.append(symbol)
        centered = np.asarray(quarter) - np.mean(quarter)
        excess.extend(centered)
        returns.extend(centered + 0.02)
    return FundamentalPricingDataset(
        feature_dates=tuple(row_dates),
        label_end_dates=tuple(label_dates),
        symbols=tuple(row_symbols),
        feature_names=PRICING_FEATURE_NAMES,
        values=values,
        availability_mask=mask,
        forward_returns=np.asarray(returns),
        excess_returns=np.asarray(excess),
        metadata={"contract": "latent-peer-synthetic"},
    ).validate()


def _config() -> LatentPeerMoEConfig:
    return LatentPeerMoEConfig(
        expert_count=3,
        restarts=2,
        em_iterations=18,
        gate_steps=60,
        minimum_train_rows=60,
        minimum_train_dates=6,
    )


def test_company_conditioned_gate_is_deterministic_and_not_global():
    dataset = _dataset()
    cutoff = sorted(set(dataset.feature_dates))[12]
    train = dataset.rows_before(cutoff)
    test = dataset.rows_on(cutoff)
    first = LatentPeerMoE(dataset.feature_names, _config()).fit(
        dataset.values[train],
        dataset.availability_mask[train],
        tuple(np.asarray(dataset.feature_dates, dtype=object)[train]),
        dataset.excess_returns[train],
    )
    second = LatentPeerMoE(dataset.feature_names, _config()).fit(
        dataset.values[train],
        dataset.availability_mask[train],
        tuple(np.asarray(dataset.feature_dates, dtype=object)[train]),
        dataset.excess_returns[train],
    )
    first_gate = first.predict(
        dataset.values[test], dataset.availability_mask[test]
    )["gate"]
    second_gate = second.predict(
        dataset.values[test], dataset.availability_mask[test]
    )["gate"]

    assert np.allclose(first_gate.sum(axis=1), 1.0)
    assert np.max(first_gate.std(axis=0)) > 1e-3
    assert np.allclose(first_gate, second_gate)

    valuation_indices = first.valuation_indices
    changed = dataset.values[test].copy()
    changed[:, valuation_indices] += 1000.0
    original_valuation = first.predict(
        dataset.values[test], dataset.availability_mask[test]
    )["valuation_prediction"]
    changed_valuation = first.predict(
        changed, dataset.availability_mask[test]
    )["valuation_prediction"]
    assert np.allclose(original_valuation, changed_valuation)


def test_walk_forward_is_causal_and_discovers_soft_peers():
    dataset = _dataset()
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
    report = LatentPeerWalkForwardEvaluator(_config()).run(
        dataset, snapshot
    )

    assert report["dataset"]["leakage_violations"] == 0
    assert report["acceptance"]["peer_labels_used"] is False
    assert report["acceptance"]["company_codes_used_as_features"] is False
    assert report["gate_diagnostics"]["company_conditioned"]
    assert len(report["latest_latent_peers"]) == len(snapshot.symbols)
    assert all(
        len(row["peers"]) == 5
        for row in report["latest_latent_peers"]
    )
    assert report["valuation_metrics"]["mean_valuation_rank_ic"] is not None
