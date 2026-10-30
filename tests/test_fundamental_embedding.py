from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from src.fundamental_embedding import PRICING_FEATURE_NAMES, FundamentalPricingDataset
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
    persistent_style = rng.normal(size=(len(symbols), len(PRICING_FEATURE_NAMES)))
    for date_index in range(len(quarter_dates)):
        for symbol_index in range(len(symbols)):
            row = date_index * len(symbols) + symbol_index
            features[row] = 0.75 * persistent_style[symbol_index] + 0.25 * features[row]
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
    assert _ttm_or_annual_value(quarters, "capital_expenditures") == 40.0

    quarters[-1].capital_expenditures = None
    annual = FinancialStatementSnapshot(
        period_end=date(2024, 12, 31),
        published_at=date(2025, 3, 31),
        period_type="year",
        source="test",
        capital_expenditures=55.0,
    )
    assert _ttm_or_annual_value([annual, *quarters], "capital_expenditures") == 55.0


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
