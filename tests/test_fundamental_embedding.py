from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from src.fundamental_embedding import (
    PRICING_FEATURE_NAMES,
    FundamentalPricingDataset,
    FundamentalPricingSnapshot,
    MoEConfig,
    WalkForwardMoEEvaluator,
)
from src.fundamental_embedding.dataset import _ttm_or_annual_value
from src.instruments.calculations import _populate_growth
from src.instruments.models import (
    CompanyFundamentals,
    FinancialStatementSnapshot,
    MetricStatus,
)


def _synthetic_dataset() -> FundamentalPricingDataset:
    rng = np.random.default_rng(20260818)
    symbols = [f"{index:06d}" for index in range(8)]
    quarter_dates = [date(2021, 3, 31) + timedelta(days=91 * i) for i in range(18)]
    rows = len(symbols) * len(quarter_dates)
    features = rng.normal(size=(rows, len(PRICING_FEATURE_NAMES)))
    persistent_style = rng.normal(
        size=(len(symbols), len(PRICING_FEATURE_NAMES))
    )
    for date_index in range(len(quarter_dates)):
        for symbol_index in range(len(symbols)):
            row = date_index * len(symbols) + symbol_index
            features[row] = (
                0.75 * persistent_style[symbol_index]
                + 0.25 * features[row]
            )
    mask = rng.random(features.shape) > 0.12
    features[~mask] = np.nan
    feature_dates = []
    label_dates = []
    row_symbols = []
    target = []
    returns = []
    for date_index, feature_date in enumerate(quarter_dates):
        quarter_target = []
        for symbol_index, symbol in enumerate(symbols):
            row = date_index * len(symbols) + symbol_index
            value = (
                0.025 * np.nan_to_num(features[row, 0])
                + 0.018 * np.nan_to_num(features[row, 10])
                + rng.normal(scale=0.025)
            )
            quarter_target.append(value)
            feature_dates.append(feature_date)
            label_dates.append(feature_date + timedelta(days=80))
            row_symbols.append(symbol)
        centered = np.asarray(quarter_target) - np.mean(quarter_target)
        target.extend(centered)
        returns.extend(centered + 0.02)
    return FundamentalPricingDataset(
        feature_dates=tuple(feature_dates),
        label_end_dates=tuple(label_dates),
        symbols=tuple(row_symbols),
        feature_names=PRICING_FEATURE_NAMES,
        values=features,
        availability_mask=mask,
        forward_returns=np.asarray(returns),
        excess_returns=np.asarray(target),
        metadata={"contract": "synthetic-test"},
    ).validate()


def test_walk_forward_moe_is_causal_stable_and_mixed():
    dataset = _synthetic_dataset()
    report = WalkForwardMoEEvaluator(
        MoEConfig(
            minimum_train_rows=48,
            minimum_train_dates=6,
            embedding_smoothing_alpha=0.35,
        )
    ).run(dataset)

    assert report.dataset["leakage_violations"] == 0
    assert report.dataset["test_quarter_count"] >= 6
    assert report.acceptance["framework_ready"]
    assert report.stability["stable_median_cosine"] >= 0.80
    assert (
        report.stability["stable_median_l2_turnover"]
        <= report.stability["raw_median_l2_turnover"]
    )
    latest_gates = report.expert_diagnostics["latest_gate_weights"]
    assert np.isclose(sum(latest_gates.values()), 1.0)
    assert all(value >= 0.05 - 1e-12 for value in latest_gates.values())
    assert len(report.latest_embeddings) == 8
    assert len(report.latest_embeddings[0]["embedding"]) == 12


def test_training_rows_require_fully_realized_labels():
    dataset = _synthetic_dataset()
    cutoff = dataset.feature_dates[8 * 8]
    selected = dataset.rows_before(cutoff)

    assert selected.any()
    assert all(
        label_end < cutoff
        for label_end, include in zip(dataset.label_end_dates, selected)
        if include
    )
    assert not any(
        label_end >= cutoff and include
        for label_end, include in zip(dataset.label_end_dates, selected)
    )


def test_dataset_rejects_non_forward_label():
    dataset = _synthetic_dataset()
    broken = FundamentalPricingDataset(
        feature_dates=dataset.feature_dates,
        label_end_dates=(dataset.feature_dates[0], *dataset.label_end_dates[1:]),
        symbols=dataset.symbols,
        feature_names=dataset.feature_names,
        values=dataset.values,
        availability_mask=dataset.availability_mask,
        forward_returns=dataset.forward_returns,
        excess_returns=dataset.excess_returns,
    )
    try:
        broken.validate()
    except ValueError as exc:
        assert "forward label" in str(exc)
    else:
        raise AssertionError("non-forward label was accepted")


def test_current_snapshot_is_inference_only():
    dataset = _synthetic_dataset()
    latest_rows = dataset.rows_on(max(dataset.feature_dates))
    inference_date = max(dataset.label_end_dates) + timedelta(days=1)
    snapshot = FundamentalPricingSnapshot(
        feature_date=inference_date,
        symbols=tuple(
            symbol
            for symbol, selected in zip(dataset.symbols, latest_rows)
            if selected
        ),
        feature_names=dataset.feature_names,
        values=dataset.values[latest_rows],
        availability_mask=dataset.availability_mask[latest_rows],
    ).validate()

    report = WalkForwardMoEEvaluator(
        MoEConfig(minimum_train_rows=48, minimum_train_dates=6)
    ).run(dataset, inference_snapshot=snapshot)

    assert report.dataset["current_inference"]["generated"]
    assert report.dataset["current_inference"]["feature_date"] == (
        inference_date.isoformat()
    )
    assert len(report.latest_embeddings) == len(snapshot.symbols)
    assert {
        item["source"] for item in report.latest_embeddings
    } == {"current_unlabelled_snapshot"}


def test_current_index_memberships_are_diagnostic_only():
    dataset = _synthetic_dataset()
    memberships = {
        f"{index:06d}": tuple(
            cohort
            for cohort, selected in (
                ("csi_300", index < 5),
                ("csi_500", index >= 3),
                ("sse_dividend", index % 2 == 0),
            )
            if selected
        )
        for index in range(8)
    }
    evaluator = WalkForwardMoEEvaluator(
        MoEConfig(minimum_train_rows=48, minimum_train_dates=6)
    )

    plain = evaluator.run(dataset)
    grouped = evaluator.run(dataset, current_memberships=memberships)

    assert grouped.metrics["mse"] == plain.metrics["mse"]
    assert [
        item["prediction"] for item in grouped.predictions
    ] == [
        item["prediction"] for item in plain.predictions
    ]
    assert set(grouped.metrics["current_membership_cohorts"]) == {
        "csi_300",
        "csi_500",
        "sse_dividend",
    }


def test_capex_uses_complete_ttm_then_disclosed_annual_fallback():
    quarters = [
        FinancialStatementSnapshot(
            period_end=date(2025, month, day),
            published_at=date(2025, month, day) + timedelta(days=30),
            period_type="quarter",
            source="test",
            capital_expenditures=10.0,
        )
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
    ]
    assert _ttm_or_annual_value(
        quarters, "capital_expenditures"
    ) == 40.0

    quarters[-1].capital_expenditures = None
    annual = FinancialStatementSnapshot(
        period_end=date(2024, 12, 31),
        published_at=date(2025, 3, 31),
        period_type="year",
        source="test",
        capital_expenditures=55.0,
    )
    assert _ttm_or_annual_value(
        [annual, *quarters], "capital_expenditures"
    ) == 55.0


def test_growth_falls_back_by_field_when_quarter_revenue_is_missing():
    quarter_ends = (
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
        date(2023, 12, 31),
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
    )
    quarters = [
        FinancialStatementSnapshot(
            period_end=period_end,
            published_at=period_end + timedelta(days=30),
            period_type="quarter",
            source="test",
            net_income_parent=float(index + 1),
        )
        for index, period_end in enumerate(quarter_ends)
    ]
    annuals = [
        FinancialStatementSnapshot(
            period_end=date(year, 12, 31),
            published_at=date(year + 1, 3, 31),
            period_type="year",
            source="test",
            revenue=revenue,
        )
        for year, revenue in ((2023, 100.0), (2024, 120.0))
    ]
    result = CompanyFundamentals()

    _populate_growth(result, quarters, annuals)

    assert result.growth["revenue_yoy"].status == MetricStatus.DERIVED
    assert result.growth["revenue_yoy"].value_pct == pytest.approx(20.0)
    assert result.growth["revenue_ttm_yoy"].status == MetricStatus.DERIVED
    assert result.growth["revenue_ttm_yoy"].value_pct == pytest.approx(20.0)
