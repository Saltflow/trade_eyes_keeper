from datetime import date

from src.instruments.calculations import (
    derive_company_fundamentals,
    to_standalone_quarters,
)
from src.instruments.models import (
    FinancialStatementSnapshot,
    MetricStatus,
    MetricValue,
)


def test_missing_cumulative_predecessor_does_not_create_false_quarter():
    first_quarter = FinancialStatementSnapshot(
        period_end=date(2025, 3, 31),
        published_at=date(2025, 4, 30),
        period_type="quarter",
        is_cumulative=True,
        source="test",
        revenue=100.0,
        net_income_parent=10.0,
    )
    third_quarter = FinancialStatementSnapshot(
        period_end=date(2025, 9, 30),
        published_at=date(2025, 10, 30),
        period_type="quarter",
        is_cumulative=True,
        source="test",
        revenue=390.0,
        net_income_parent=42.0,
    )

    quarters = to_standalone_quarters([first_quarter, third_quarter])

    assert quarters[0].revenue == 100.0
    assert quarters[1].revenue is None
    assert quarters[1].net_income_parent is None
    assert (
        "cumulative_predecessor_missing_flows_unavailable"
        in quarters[1].diagnostics
    )


def test_missing_predecessor_field_does_not_leave_cumulative_value_as_quarter():
    first_quarter = FinancialStatementSnapshot(
        period_end=date(2025, 3, 31),
        published_at=date(2025, 4, 30),
        period_type="quarter",
        is_cumulative=True,
        source="test",
        revenue=None,
        net_income_parent=10.0,
    )
    second_quarter = FinancialStatementSnapshot(
        period_end=date(2025, 6, 30),
        published_at=date(2025, 8, 30),
        period_type="quarter",
        is_cumulative=True,
        source="test",
        revenue=230.0,
        net_income_parent=24.0,
    )

    quarters = to_standalone_quarters([first_quarter, second_quarter])

    assert quarters[1].revenue is None
    assert quarters[1].net_income_parent == 14.0
    assert "derived_from_cumulative_statement" in quarters[1].diagnostics


def test_a_share_cumulative_cash_flow_produces_single_quarter_and_ttm_fcf():
    periods = [
        (date(2025, 3, 31), "quarter", 10.0),
        (date(2025, 6, 30), "quarter", 18.0),
        (date(2025, 9, 30), "quarter", 25.0),
        (date(2025, 12, 31), "year", 40.0),
        (date(2026, 3, 31), "quarter", 5.0),
        (date(2026, 6, 30), "quarter", 2.0),
    ]
    statements = [
        FinancialStatementSnapshot(
            period_end=period,
            published_at=period,
            period_type=period_type,
            is_cumulative=True,
            currency="CNY",
            source="baostock_profit+cninfo_annual_report",
            common_shares_outstanding=100.0,
            parent_equity=500.0,
            free_cash_flow=free_cash_flow,
        )
        for period, period_type, free_cash_flow in periods
    ]

    quarters = to_standalone_quarters(statements)
    latest_quarter = next(
        item for item in quarters if item.period_end == date(2026, 6, 30)
    )
    assert latest_quarter.free_cash_flow == -3.0

    result = derive_company_fundamentals(
        statements,
        current_price=MetricValue(
            value=10.0,
            status=MetricStatus.OBSERVED,
            source="test_price",
        ),
        evaluation_date=date(2026, 8, 18),
    )

    assert result.latest_quarter_free_cash_flow.value == -3.0
    assert result.latest_quarter_free_cash_flow.as_of == date(2026, 6, 30)
    assert result.ttm_free_cash_flow.value == 24.0
    assert result.ttm_free_cash_flow.period == "TTM"
