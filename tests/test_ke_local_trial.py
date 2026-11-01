"""Synthetic unit fixtures for causal daily Ke; never real replay results."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.market_history import CorporateAction, PriceHistoryBundle
from src.experiments.ke_local_trial import MemoryStatementStore, make_ke_context
from src.instruments.models import FinancialStatementSnapshot
from src.search.config import ExecutionConfig
from src.strategy.api import StrategyMarketData


def statement(*, published="2025-01-02", eps=7.0, currency="CNY"):
    return FinancialStatementSnapshot(
        period_end=date(2024, 12, 31),
        published_at=date.fromisoformat(published),
        period_type="year",
        source="synthetic_unit_fixture",
        currency=currency,
        total_shares=100.0,
        parent_equity=1000.0,
        revenue=2000.0,
        net_income_parent=eps * 100,
        diluted_eps=eps,
    )


def panel(
    statements, *, excluded=(), code="600001", currency="CNY", actions=(), tradable=None
):
    dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
    prices = np.array([[100.0], [100.0], [50.0]])
    bundle = PriceHistoryBundle(
        code=code,
        currency=currency,
        actions=list(actions),
        prices=pd.DataFrame(
            {
                "date": dates,
                "raw_close": prices[:, 0],
                "tradable": [True, True, True] if tradable is None else tradable,
            }
        ),
    )
    data = StrategyMarketData(
        indicator_matrix=np.ones((3, 1, 1)),
        prices=prices,
        dates=dates,
        symbols=[code],
    )
    diagnostics = {}
    context = make_ke_context(
        MemoryStatementStore(statements),
        {code: bundle},
        ExecutionConfig(),
        set(excluded),
        diagnostics,
    )
    return context(data), diagnostics[code]


def test_daily_formula_and_next_day_disclosure_visibility():
    result, audit = panel([statement()])
    assert result.fundamental_availability_mask[:, 0, 0].tolist() == [False, True, True]
    assert result.fundamental_features[1, 0, 0] == pytest.approx(0.09)
    assert result.fundamental_features[2, 0, 0] == pytest.approx(0.16)
    assert result.fundamental_as_of_dates[1, 0] == date(2025, 1, 2)
    assert audit["available_days"] == 2


def test_future_revision_cannot_change_prior_ke():
    original, _ = panel([statement(published="2025-01-01")])
    revised, _ = panel(
        [statement(published="2025-01-01"), statement(published="2025-01-06", eps=70.0)]
    )
    np.testing.assert_equal(original.fundamental_features, revised.fundamental_features)


def test_reporting_currency_converts_to_quote_currency_once():
    result, audit = panel(
        [statement(published="2025-01-01")], code="00001", currency="HKD"
    )
    assert result.fundamental_features[0, 0, 0] == pytest.approx(0.02 + 0.07 / 0.9)
    assert audit["earnings_to_quote_fx"] == pytest.approx(1 / 0.9)


@pytest.mark.parametrize(
    "statements", [[], [statement(eps=-1)], [statement(currency=None)]]
)
def test_missing_or_nonmeaningful_pe_never_becomes_zero_ke(statements):
    result, audit = panel(statements)
    assert not result.fundamental_availability_mask.any()
    assert np.isnan(result.fundamental_features).all()
    assert audit["missing_reason"]


def test_configured_observation_pool_cannot_generate_orders():
    result, audit = panel([statement()], excluded=["600001"])
    assert not result.fundamental_availability_mask.any()
    assert audit["missing_reason"] == "configured_skip_signals"


def test_split_rebuilds_eps_from_parent_income_and_adjusted_shares():
    action = CorporateAction(
        code="600001",
        action_type="stock_split",
        ex_date=date(2025, 1, 6),
        published_at=date(2025, 1, 2),
        share_multiplier=2.0,
        source="synthetic_unit_fixture",
    )
    result, audit = panel([statement(published="2025-01-01")], actions=[action])
    assert result.fundamental_features[0, 0, 0] == pytest.approx(0.09)
    assert result.fundamental_features[2, 0, 0] == pytest.approx(0.09)
    assert audit["split_safe_income_per_share_days"] == 1


def test_future_currency_cannot_disable_earlier_valuation():
    result, _ = panel(
        [
            statement(published="2025-01-01"),
            statement(published="2025-01-06", currency="USD"),
        ]
    )
    assert result.fundamental_availability_mask[:, 0, 0].tolist() == [True, True, True]


def test_latest_reported_ttm_loss_cannot_reuse_old_profit():
    old = statement(published="2025-01-01", eps=7)
    old.period_end = date(2023, 12, 31)
    new = statement(published="2025-01-02", eps=-1)
    for item in (old, new):
        item.diagnostics = ["diluted_eps_contains_baostock_ttm_eps"]
    result, audit = panel([old, new])
    assert result.fundamental_availability_mask[:, 0, 0].tolist() == [
        True,
        False,
        False,
    ]
    assert audit["nonpositive_reported_ttm_days"] == 2


def test_explicit_halt_flag_survives_positive_prices():
    from src.strategy.api import Params
    from src.strategy.plugins.ke_threshold_allocation import (
        KeThresholdAllocationStrategy,
    )

    result, _ = panel([statement(published="2025-01-01")], tradable=[True, False, True])
    plan = KeThresholdAllocationStrategy().make_signals(Params({}), result)
    assert plan.buy_signals[:, 0].tolist() == [True, False, True]


def test_split_without_safe_earnings_cannot_use_old_eps():
    report = statement(published="2025-01-01")
    report.net_income_parent = None
    action = CorporateAction(
        code="600001",
        action_type="stock_split",
        ex_date=date(2025, 1, 6),
        published_at=date(2025, 1, 2),
        share_multiplier=2.0,
        source="synthetic_unit_fixture",
    )
    result, _ = panel([report], actions=[action])
    assert result.fundamental_availability_mask[:, 0, 0].tolist() == [True, True, False]


def test_fresh_explicit_ttm_after_split_keeps_its_current_share_basis():
    old = statement(published="2025-01-01")
    old.period_end = date(2023, 12, 31)
    new = statement(published="2025-01-02", eps=3.5)
    new.diagnostics = ["diluted_eps_contains_baostock_ttm_eps"]
    action = CorporateAction(
        code="600001",
        action_type="stock_split",
        ex_date=date(2024, 7, 1),
        published_at=date(2024, 6, 1),
        share_multiplier=2.0,
        source="synthetic_unit_fixture",
    )
    result, audit = panel([old, new], actions=[action])
    assert result.fundamental_features[1, 0, 0] == pytest.approx(0.055)
    assert result.fundamental_features[2, 0, 0] == pytest.approx(0.09)
    assert audit["split_safe_income_per_share_days"] == 1


def test_cumulative_quarter_eps_is_not_summed_as_standalone_eps():
    reports = []
    for month, income, eps in ((3, 20, 2), (6, 45, 4.5), (9, 75, 7.5), (12, 100, 10)):
        report = statement(published="2025-01-01", eps=eps)
        report.period_end = date(2024, month, 31 if month in (3, 12) else 30)
        report.period_type = "year" if month == 12 else "quarter"
        report.is_cumulative = True
        report.net_income_parent = income
        reports.append(report)
    result, audit = panel(reports)
    assert result.fundamental_features[0, 0, 0] == pytest.approx(0.03)
    assert audit["cumulative_eps_guard_days"] == 3
