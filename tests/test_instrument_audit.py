from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.instruments.audit import InstrumentAuditService, render_audit_html
from src.instruments.calculations import (
    available_statements,
    calculate_look_through,
    derive_company_fundamentals,
    derive_fund_profile,
    to_standalone_quarters,
)
from src.instruments.classifier import (
    classify_instrument,
    normalize_yahoo_symbol,
)
from src.instruments.models import (
    FinancialStatementSnapshot,
    FundHolding,
    FundProfile,
    HoldingFundamentals,
    InstrumentType,
    MetricStatus,
    MetricValue,
)
from src.instruments.providers import ProviderPayload, QQQuoteProvider
from src.instruments.providers import SecCompanyFactsProvider


def test_sec_user_agent_environment_overrides_config(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "trade-eyes-test test@example.com")
    provider = SecCompanyFactsProvider(
        {"instrument_audit": {"sec_user_agent": "config@example.com"}}
    )
    assert provider.sec_user_agent == "trade-eyes-test test@example.com"


def test_sec_user_agent_falls_back_to_email_sender(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("EMAIL_SENDER", "owner@example.com")
    provider = SecCompanyFactsProvider({})
    assert provider.sec_user_agent == (
        "trade-eyes-keeper/1.0 owner@example.com"
    )


def _statement(
    period_end: date,
    *,
    revenue: float,
    income: float,
    equity: float,
    eps: float,
    published_at: date | None = None,
    cumulative: bool = False,
) -> FinancialStatementSnapshot:
    return FinancialStatementSnapshot(
        period_end=period_end,
        published_at=published_at or period_end,
        period_type="quarter",
        is_cumulative=cumulative,
        currency="CNY",
        source="test_statement",
        common_shares_outstanding=100.0,
        parent_equity=equity,
        revenue=revenue,
        net_income_parent=income,
        adjusted_net_income_parent=income * 0.9,
        diluted_eps=eps,
    )


def _eight_quarters() -> list[FinancialStatementSnapshot]:
    return [
        _statement(date(2024, 3, 31), revenue=70, income=4, equity=360, eps=0.04),
        _statement(date(2024, 6, 30), revenue=80, income=5, equity=370, eps=0.05),
        _statement(date(2024, 9, 30), revenue=90, income=6, equity=380, eps=0.06),
        _statement(date(2024, 12, 31), revenue=100, income=7, equity=400, eps=0.07),
        _statement(date(2025, 3, 31), revenue=110, income=8, equity=420, eps=0.08),
        _statement(date(2025, 6, 30), revenue=120, income=9, equity=440, eps=0.09),
        _statement(date(2025, 9, 30), revenue=130, income=10, equity=460, eps=0.10),
        _statement(date(2025, 12, 31), revenue=140, income=13, equity=500, eps=0.13),
    ]


def test_classifier_separates_reit_and_fund_subtypes():
    assert classify_instrument("508091") == InstrumentType.REIT
    assert (
        classify_instrument("518660", quote_type="ETF", name="黄金ETF")
        == InstrumentType.COMMODITY_ETF
    )
    assert (
        classify_instrument("512810", quote_type="ETF", name="军工行业ETF")
        == InstrumentType.SECTOR_ETF
    )
    assert classify_instrument("601398") == InstrumentType.EQUITY


def test_symbol_normalization_fixes_berkshire_dot():
    assert normalize_yahoo_symbol("BRK.B") == "BRK-B"
    assert normalize_yahoo_symbol("00700") == "0700.HK"
    assert normalize_yahoo_symbol("601398") == "601398.SS"


def test_unpublished_statement_is_excluded():
    statement = _statement(
        date(2025, 12, 31),
        revenue=1,
        income=1,
        equity=1,
        eps=1,
        published_at=date(2026, 3, 30),
    )
    assert available_statements([statement], date(2026, 3, 29)) == []
    assert available_statements([statement], date(2026, 3, 30)) == [statement]


def test_cumulative_statements_are_converted_to_standalone_quarters():
    statements = [
        _statement(
            date(2025, 3, 31),
            revenue=100,
            income=10,
            equity=100,
            eps=0.1,
            cumulative=True,
        ),
        _statement(
            date(2025, 6, 30),
            revenue=230,
            income=24,
            equity=110,
            eps=0.2,
            cumulative=True,
        ),
        _statement(
            date(2025, 9, 30),
            revenue=390,
            income=42,
            equity=120,
            eps=0.3,
            cumulative=True,
        ),
        _statement(
            date(2025, 12, 31),
            revenue=600,
            income=65,
            equity=130,
            eps=0.4,
            cumulative=True,
        ),
    ]
    quarters = to_standalone_quarters(statements)
    assert [item.revenue for item in quarters] == [100, 130, 160, 210]
    assert [item.net_income_parent for item in quarters] == [10, 14, 18, 23]


def test_company_valuation_uses_financial_statement_values():
    result = derive_company_fundamentals(
        _eight_quarters(),
        current_price=MetricValue(
            value=10,
            status=MetricStatus.OBSERVED,
            as_of=date(2026, 1, 2),
            source="test_price",
        ),
        evaluation_date=date(2026, 1, 2),
        ttm_dividend_per_share=MetricValue(
            value=0.5,
            status=MetricStatus.OBSERVED,
            source="test_dividend",
        ),
    )
    assert result.book_value_per_share.value == pytest.approx(5.0)
    assert result.pb.value == pytest.approx(2.0)
    assert result.pe_ttm.value == pytest.approx(25.0)
    assert result.ttm_net_income_parent.value == pytest.approx(40.0)
    assert result.roe_ttm.value == pytest.approx(40 / 450 * 100)
    assert result.dividend_yield.value == pytest.approx(5.0)
    assert result.growth["revenue_qoq"].value_pct == pytest.approx(
        140 / 130 * 100 - 100)
    assert result.growth["revenue_yoy"].value_pct == pytest.approx(40.0)
    assert result.growth["net_income_ttm_yoy"].value_pct == pytest.approx(
        40 / 22 * 100 - 100
    )


def test_negative_profit_and_equity_are_not_meaningful():
    statements = _eight_quarters()
    statements[-1].net_income_parent = -100
    statements[-1].diluted_eps = -1
    statements[-1].parent_equity = -10
    result = derive_company_fundamentals(
        statements,
        current_price=MetricValue(
            value=10,
            status=MetricStatus.OBSERVED,
            as_of=date(2026, 1, 2),
            source="test_price",
        ),
        evaluation_date=date(2026, 1, 2),
    )
    assert result.pe_ttm.status == MetricStatus.NOT_MEANINGFUL
    assert result.pb.status == MetricStatus.NOT_MEANINGFUL


def test_profit_turnaround_is_labeled_instead_of_exploding_percentage():
    statements = _eight_quarters()
    statements[-5].net_income_parent = -2
    statements[-1].net_income_parent = 13
    result = derive_company_fundamentals(
        statements,
        current_price=MetricValue(
            value=10,
            status=MetricStatus.OBSERVED,
            source="test_price",
        ),
        evaluation_date=date(2026, 1, 2),
    )
    growth = result.growth["net_income_yoy"]
    assert growth.status == MetricStatus.NOT_MEANINGFUL
    assert growth.interpretation == "扭亏"
    assert growth.value_pct is None


def test_look_through_uses_yield_aggregation_not_average_multiples():
    holdings = [
        FundHolding(
            code="A",
            weight=0.6,
            fundamentals=HoldingFundamentals(
                pe_ttm=MetricValue(value=10, status=MetricStatus.OBSERVED),
                pb=MetricValue(value=2, status=MetricStatus.OBSERVED),
                dividend_yield=MetricValue(value=3, status=MetricStatus.OBSERVED),
            ),
        ),
        FundHolding(
            code="B",
            weight=0.4,
            fundamentals=HoldingFundamentals(
                pe_ttm=MetricValue(value=20, status=MetricStatus.OBSERVED),
                pb=MetricValue(value=4, status=MetricStatus.OBSERVED),
                dividend_yield=MetricValue(value=1, status=MetricStatus.OBSERVED),
            ),
        ),
    ]
    result = calculate_look_through(holdings)
    assert result["pe_ttm"].value.value == pytest.approx(12.5)
    assert result["pb"].value.value == pytest.approx(2.5)
    assert result["roe_ttm"].value.value == pytest.approx(20.0)
    assert result["dividend_yield"].value.value == pytest.approx(2.2)
    assert result["pe_ttm"].covered_weight == pytest.approx(1.0)


def test_fund_nav_dividend_and_ffo_are_type_appropriate():
    fund = FundProfile(
        nav_per_unit=MetricValue(
            value=9.5,
            status=MetricStatus.OBSERVED,
            source="official_nav",
        ),
        ttm_dividend_per_unit=MetricValue(
            value=0.4,
            status=MetricStatus.OBSERVED,
            source="official_distribution",
        ),
        ffo_per_unit=MetricValue(
            value=0.5,
            status=MetricStatus.OBSERVED,
            source="official_reit_report",
        ),
    )
    result = derive_fund_profile(
        fund,
        current_price=MetricValue(
            value=10,
            status=MetricStatus.OBSERVED,
            source="close",
        ),
    )
    assert result.premium_discount_rate.value == pytest.approx(100 * 0.5 / 9.5)
    assert result.p_nav.value == pytest.approx(10 / 9.5)
    assert result.dividend_yield.value == pytest.approx(4.0)
    assert result.p_ffo.value == pytest.approx(20.0)


class _FakeProvider:
    def __init__(self, payloads):
        self.payloads = payloads

    def fetch(self, code, evaluation_date, *args):
        return self.payloads.get(str(code), ProviderPayload())


def test_audit_uses_applicable_fields_and_does_not_count_reit_as_etf():
    statements = _eight_quarters()
    yahoo = _FakeProvider(
        {
            "601398": ProviderPayload(
                metadata={
                    "name": "Company",
                    "quote_type": "EQUITY",
                    "exchange": "SSE",
                    "currency": "CNY",
                },
                statements=statements,
                ttm_dividend=MetricValue(
                    value=0.3,
                    status=MetricStatus.OBSERVED,
                    source="dividend",
                ),
            ),
            "508091": ProviderPayload(
                metadata={
                    "name": "Infrastructure REIT",
                    "exchange": "SSE",
                    "currency": "CNY",
                }
            ),
        }
    )
    qq = _FakeProvider(
        {
            code: ProviderPayload(
                price=MetricValue(
                    value=10,
                    status=MetricStatus.OBSERVED,
                    source="qq",
                )
            )
            for code in ("601398", "508091")
        }
    )
    fund = _FakeProvider(
        {
            "508091": ProviderPayload(
                fund=FundProfile(
                    nav_per_unit=MetricValue(
                        value=9,
                        status=MetricStatus.OBSERVED,
                        source="official",
                    )
                )
            )
        }
    )
    service = InstrumentAuditService(
        {"stocks": ["601398", "508091"]},
        yahoo_provider=yahoo,
        qq_provider=qq,
        sec_provider=_FakeProvider({}),
        fund_provider=fund,
    )
    report = service.run(write_files=False, evaluation_date=date(2026, 1, 2))
    profiles = {profile.code: profile for profile in report.profiles}
    assert profiles["601398"].instrument_type == InstrumentType.EQUITY
    assert profiles["508091"].instrument_type == InstrumentType.REIT
    reit_statuses = profiles["508091"].completeness["statuses"]
    assert "pe_ttm" not in reit_statuses
    assert "top_holdings" not in reit_statuses
    assert reit_statuses["p_nav"] == MetricStatus.DERIVED.value
    assert "标的画像审计" in render_audit_html(report)


class _Response:
    def __init__(self, text):
        self.text = text
        self.url = "http://qq.test"

    def raise_for_status(self):
        return None


class _Http:
    def __init__(self, text):
        self.text = text

    def get(self, *args, **kwargs):
        return _Response(self.text)


def _qq_payload(code, pe, field_46):
    items = [""] * 50
    items[1] = code
    items[3] = "10"
    items[39] = str(pe)
    items[44] = "100"
    items[45] = "120"
    items[46] = str(field_46)
    return 'v_test="' + "~".join(items) + '";'


def test_qq_field_46_is_only_pb_for_a_shares():
    a_payload = QQQuoteProvider(
        {},
        http=_Http(_qq_payload("A", 8, 0.75)),
    ).fetch("601398", date(2026, 1, 2))
    hk_payload = QQQuoteProvider(
        {},
        http=_Http(_qq_payload("HK", 17, "TENCENT")),
    ).fetch("00700", date(2026, 1, 2))
    assert a_payload.quoted_pb.value == pytest.approx(0.75)
    assert hk_payload.quoted_pe.value == pytest.approx(17)
    assert hk_payload.quoted_pb is None


def test_annual_fallback_provides_yoy_without_fabricating_qoq():
    annual_2024 = _statement(
        date(2024, 12, 31),
        revenue=100,
        income=10,
        equity=200,
        eps=1,
        published_at=date(2025, 3, 20),
    )
    annual_2024.period_type = "year"
    annual_2025 = _statement(
        date(2025, 12, 31),
        revenue=120,
        income=15,
        equity=240,
        eps=1.5,
        published_at=date(2026, 3, 20),
    )
    annual_2025.period_type = "year"
    result = derive_company_fundamentals(
        [annual_2024, annual_2025],
        current_price=MetricValue(
            value=12,
            status=MetricStatus.OBSERVED,
            as_of=date(2026, 3, 21),
            source="test_price",
        ),
        evaluation_date=date(2026, 3, 21),
    )
    assert result.ttm_revenue.value == pytest.approx(120)
    assert result.ttm_revenue.note == "季度不足，使用最新已披露年报"
    assert result.pe_ttm.value == pytest.approx(8)
    assert result.roe_ttm.value == pytest.approx(15 / 220 * 100)
    assert result.growth["revenue_yoy"].value_pct == pytest.approx(20)
    assert "revenue_qoq" not in result.growth


def test_semiannual_cadence_does_not_create_quarterly_growth_or_ttm_sum():
    statements = [
        _statement(
            date(2024, 6, 30),
            revenue=80,
            income=8,
            equity=180,
            eps=0.8,
        ),
        _statement(
            date(2024, 12, 31),
            revenue=100,
            income=10,
            equity=200,
            eps=1,
        ),
        _statement(
            date(2025, 6, 30),
            revenue=96,
            income=9,
            equity=220,
            eps=0.9,
        ),
        _statement(
            date(2025, 12, 31),
            revenue=120,
            income=12,
            equity=240,
            eps=1.2,
        ),
    ]
    result = derive_company_fundamentals(
        statements,
        current_price=MetricValue(
            value=12,
            status=MetricStatus.OBSERVED,
            source="test_price",
        ),
        evaluation_date=date(2026, 1, 2),
    )
    assert result.ttm_revenue.value is None
    assert "revenue_qoq" not in result.growth
    assert result.growth["revenue_yoy"].value_pct == pytest.approx(20)


def test_detailed_audit_renders_holding_fundamentals_and_coverage():
    yahoo = _FakeProvider(
        {
            "512810": ProviderPayload(
                metadata={
                    "name": "行业ETF",
                    "quote_type": "ETF",
                    "currency": "CNY",
                }
            ),
            "601398": ProviderPayload(
                metadata={
                    "name": "成分公司",
                    "quote_type": "EQUITY",
                    "currency": "CNY",
                },
                statements=_eight_quarters(),
            ),
        }
    )
    qq = _FakeProvider(
        {
            code: ProviderPayload(
                price=MetricValue(
                    value=10,
                    status=MetricStatus.OBSERVED,
                    source="qq",
                )
            )
            for code in ("512810", "601398")
        }
    )
    configured_fund = _FakeProvider(
        {
            "512810": ProviderPayload(
                fund=FundProfile(
                    holdings_as_of=date(2025, 12, 31),
                    top_holdings=[
                        FundHolding(
                            code="601398",
                            name="成分公司",
                            weight=0.12,
                            as_of=date(2025, 12, 31),
                            source="issuer_holdings",
                        )
                    ],
                )
            )
        }
    )
    service = InstrumentAuditService(
        {"stocks": ["512810"]},
        yahoo_provider=yahoo,
        qq_provider=qq,
        sec_provider=_FakeProvider({}),
        fund_provider=configured_fund,
        public_fund_provider=_FakeProvider({}),
    )
    report = service.run(write_files=False, evaluation_date=date(2026, 1, 2))
    rendered = render_audit_html(report)
    assert "前十大成分股逐只画像" in rendered
    assert "601398" in rendered
    assert "有效权重" in rendered
    assert "2025-12-31" in rendered


def test_optimizer_package_does_not_import_current_instrument_profiles():
    optimization_roots = (
        Path("src/strategy"),
        Path("src/search"),
        Path("src/backtest"),
    )
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in optimization_roots
        for path in root.rglob("*.py")
    )
    assert "InstrumentAuditService" not in text
    assert "instrument_audit" not in text


def test_latest_empty_quarter_does_not_hide_latest_balance_sheet():
    statements = _eight_quarters()
    annual = _statement(
        date(2025, 12, 31),
        revenue=500,
        income=40,
        equity=500,
        eps=0.4,
        published_at=date(2026, 2, 1),
    )
    annual.period_type = "year"
    statements.append(annual)
    statements.append(
        FinancialStatementSnapshot(
            period_end=date(2026, 3, 31),
            period_type="quarter",
            source="test_statement",
            total_shares=100.0,
            published_at=date(2026, 4, 1),
        )
    )
    result = derive_company_fundamentals(
        statements,
        current_price=MetricValue(
            value=10,
            status=MetricStatus.OBSERVED,
            source="test_price",
        ),
        evaluation_date=date(2026, 4, 2),
    )
    assert result.book_value_per_share.value == pytest.approx(5)
    assert result.pb.value == pytest.approx(2)
    assert result.roe_ttm.value == pytest.approx(40 / 450 * 100)
    assert result.book_value_per_share.as_of == date(2025, 12, 31)
    assert result.ttm_net_income_parent.value == pytest.approx(40)
    assert result.ttm_net_income_parent.as_of == date(2025, 12, 31)


def test_dual_class_valuation_uses_share_basis_compatible_with_eps():
    first = FinancialStatementSnapshot(
        period_end=date(2024, 12, 31),
        published_at=date(2025, 2, 1),
        period_type="year",
        source="test_statement",
        currency="USD",
        common_shares_outstanding=100,
        diluted_average_shares=100_000,
        parent_equity=180_000,
        revenue=500_000,
        net_income_parent=90_000,
        diluted_eps=0.9,
    )
    second = FinancialStatementSnapshot(
        period_end=date(2025, 12, 31),
        published_at=date(2026, 2, 1),
        period_type="year",
        source="test_statement",
        currency="USD",
        common_shares_outstanding=100,
        diluted_average_shares=100_000,
        parent_equity=200_000,
        revenue=600_000,
        net_income_parent=100_000,
        diluted_eps=1,
    )
    result = derive_company_fundamentals(
        [first, second],
        current_price=MetricValue(
            value=2,
            status=MetricStatus.OBSERVED,
            source="test_price",
        ),
        evaluation_date=date(2026, 2, 2),
    )
    assert result.total_shares.value == pytest.approx(100_000)
    assert (
        result.total_shares.note
        == "稀释加权股数与报告 EPS/交易股类相容"
    )
    assert result.book_value_per_share.value == pytest.approx(2)
    assert result.pb.value == pytest.approx(1)
    assert result.pe_ttm.value == pytest.approx(2)


def test_pb_falls_back_to_pe_times_roe_when_book_equity_is_missing():
    statement = FinancialStatementSnapshot(
        period_end=date(2025, 12, 31),
        published_at=date(2026, 3, 1),
        period_type="year",
        source="test_statement",
        currency="CNY",
        total_shares=100.0,
        average_parent_equity=500.0,
        net_income_parent=50.0,
        diluted_eps=0.5,
    )
    result = derive_company_fundamentals(
        [statement],
        current_price=MetricValue(
            value=10.0,
            status=MetricStatus.OBSERVED,
            as_of=date(2026, 3, 2),
            source="test_price",
        ),
        evaluation_date=date(2026, 3, 2),
    )
    assert result.pe_ttm.value == pytest.approx(20.0)
    assert result.roe_ttm.value == pytest.approx(10.0)
    assert result.pb.value == pytest.approx(2.0)
    assert result.pb.period == "TTM/average_equity"
    assert "not period-end PB" in result.pb.note


def test_sec_annual_free_cash_flow_uses_operating_cash_less_capex(monkeypatch):
    observation = {
        "end": "2025-12-31",
        "filed": "2026-02-15",
        "form": "10-K",
        "frame": "CY2025",
    }
    facts = {
        "facts": {
            "us-gaap": {
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [{**observation, "val": 100.0}],
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [{**observation, "val": 30.0}],
                    }
                },
            }
        },
        "entityName": "Test Company",
    }

    class Response:
        url = "https://data.sec.gov/api/xbrl/companyfacts/test.json"

        def json(self):
            return facts

    provider = SecCompanyFactsProvider(
        {"instrument_audit": {"sec_user_agent": "test test@example.com"}}
    )
    monkeypatch.setattr(
        provider,
        "_load_tickers",
        lambda: {"TEST": "0000000001"},
    )
    monkeypatch.setattr(provider, "_sec_get", lambda _url: Response())

    payload = provider.fetch("TEST", date(2026, 3, 1))

    assert len(payload.statements) == 1
    statement = payload.statements[0]
    assert statement.operating_cash_flow == pytest.approx(100.0)
    assert statement.capital_expenditures == pytest.approx(30.0)
    assert statement.free_cash_flow == pytest.approx(70.0)
    assert (
        "free_cash_flow=operating_cash_flow-capital_expenditures"
        in statement.diagnostics
    )


def test_sec_quarterly_ytd_cash_flow_is_normalized_to_negative_q2_fcf(
    monkeypatch,
):
    def observation(
        start,
        end,
        filed,
        value,
        *,
        frame="",
    ):
        row = {
            "end": end,
            "filed": filed,
            "form": "10-Q",
            "val": value,
        }
        if start:
            row["start"] = start
        if frame:
            row["frame"] = frame
        return row

    q1_filed = "2026-04-30"
    q2_filed = "2026-07-23"
    facts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            observation(
                                "2025-01-01",
                                "2025-03-31",
                                "2025-04-25",
                                90_234_000_000.0,
                                frame="CY2025Q1",
                            ),
                            observation(
                                "2025-01-01",
                                "2025-03-31",
                                q1_filed,
                                90_234_000_000.0,
                                frame="CY2025Q1",
                            ),
                            observation(
                                "2026-01-01",
                                "2026-03-31",
                                q1_filed,
                                109_896_000_000.0,
                                frame="CY2026Q1",
                            ),
                            observation(
                                "2026-04-01",
                                "2026-06-30",
                                q2_filed,
                                119_796_000_000.0,
                                frame="CY2026Q2",
                            ),
                            observation(
                                "2026-01-01",
                                "2026-06-30",
                                q2_filed,
                                229_692_000_000.0,
                            ),
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            observation(
                                "2026-01-01",
                                "2026-03-31",
                                q1_filed,
                                62_578_000_000.0,
                            ),
                            observation(
                                "2026-01-01",
                                "2026-06-30",
                                q2_filed,
                                174_685_000_000.0,
                            ),
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            observation(
                                "2026-01-01",
                                "2026-03-31",
                                q1_filed,
                                45_790_000_000.0,
                            ),
                            observation(
                                "2026-01-01",
                                "2026-06-30",
                                q2_filed,
                                84_859_000_000.0,
                            ),
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            observation(
                                "2026-01-01",
                                "2026-03-31",
                                q1_filed,
                                35_674_000_000.0,
                            ),
                            observation(
                                "2026-01-01",
                                "2026-06-30",
                                q2_filed,
                                80_598_000_000.0,
                            ),
                        ]
                    }
                },
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            observation(
                                None,
                                "2026-06-30",
                                q2_filed,
                                500_000_000_000.0,
                                frame="CY2026Q2I",
                            )
                        ]
                    }
                },
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            observation(
                                None,
                                "2026-06-30",
                                q2_filed,
                                12_000_000_000.0,
                                frame="CY2026Q2I",
                            )
                        ]
                    }
                },
            }
        },
        "entityName": "Test Company",
    }

    class Response:
        url = "https://data.sec.gov/api/xbrl/companyfacts/test.json"

        def json(self):
            return facts

    provider = SecCompanyFactsProvider(
        {"instrument_audit": {"sec_user_agent": "test test@example.com"}}
    )
    monkeypatch.setattr(
        provider,
        "_load_tickers",
        lambda: {"TEST": "0000000001"},
    )
    monkeypatch.setattr(provider, "_sec_get", lambda _url: Response())

    payload = provider.fetch("TEST", date(2026, 8, 18))
    periods = {
        (item.period_end, item.published_at): item
        for item in payload.statements
    }

    assert (date(2025, 3, 31), date(2025, 4, 25)) in periods
    assert (date(2025, 3, 31), date(2026, 4, 30)) not in periods
    q1 = periods[(date(2026, 3, 31), date(2026, 4, 30))]
    q2 = periods[(date(2026, 6, 30), date(2026, 7, 23))]
    assert q1.is_cumulative is True
    assert q2.is_cumulative is True
    assert q2.revenue == pytest.approx(229_692_000_000.0)
    assert q2.free_cash_flow == pytest.approx(4_261_000_000.0)

    standalone = to_standalone_quarters([q1, q2])
    latest = standalone[-1]
    assert latest.revenue == pytest.approx(119_796_000_000.0)
    assert latest.net_income_parent == pytest.approx(112_107_000_000.0)
    assert latest.operating_cash_flow == pytest.approx(39_069_000_000.0)
    assert latest.capital_expenditures == pytest.approx(44_924_000_000.0)
    assert latest.free_cash_flow == pytest.approx(-5_855_000_000.0)

    result = derive_company_fundamentals(
        [q1, q2],
        current_price=MetricValue(
            value=200.0,
            status=MetricStatus.OBSERVED,
            source="test_price",
        ),
        evaluation_date=date(2026, 8, 18),
    )
    assert result.latest_quarter_free_cash_flow.value == pytest.approx(
        -5_855_000_000.0
    )
