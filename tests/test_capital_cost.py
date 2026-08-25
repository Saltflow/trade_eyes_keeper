from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.data.market_history import PriceHistoryBundle
from src.fundamental_embedding.capital_cost import (
    CapitalCostConfig,
    CapitalMarketAssumptionStore,
    CapitalMarketAssumptions,
    estimate_capital_cost,
    estimate_robust_beta,
    estimate_weekly_beta,
)
from src.instruments.models import FinancialStatementSnapshot
from src.instruments.point_in_time import CninfoAnnualReportProvider


def test_capital_market_store_never_reads_future_assumption(tmp_path):
    store = CapitalMarketAssumptionStore(tmp_path)
    older = CapitalMarketAssumptions(
        as_of=date(2024, 1, 31),
        risk_free_rate=0.02,
        market_risk_premium=0.05,
        risk_free_source="official-old",
        market_risk_premium_source="proxy-old",
    )
    newer = CapitalMarketAssumptions(
        as_of=date(2025, 1, 31),
        risk_free_rate=0.018,
        market_risk_premium=0.055,
        risk_free_source="official-new",
        market_risk_premium_source="proxy-new",
    )
    store.upsert(newer)
    store.upsert(older)

    assert store.as_of(date(2023, 12, 31)) is None
    assert store.as_of(date(2024, 12, 31)) == older
    assert store.as_of(date(2025, 1, 31)) == newer
    assert store.as_of(
        date(2025, 8, 1), maximum_age_days=120
    ) is None


def _bundle(code: str, returns: np.ndarray) -> PriceHistoryBundle:
    dates = pd.date_range("2020-01-03", periods=len(returns), freq="W-FRI")
    prices = 100.0 * np.cumprod(1.0 + returns)
    return PriceHistoryBundle(
        code=code,
        prices=pd.DataFrame({"date": dates, "qfq_close": prices}),
    )


def test_weekly_beta_is_point_in_time_and_not_shrunk_by_default():
    market_returns = np.linspace(-0.02, 0.025, 160)
    company_returns = 1.4 * market_returns
    raw, adjusted, observations = estimate_weekly_beta(
        _bundle("TEST", company_returns),
        _bundle("INDEX", market_returns),
        date(2023, 12, 31),
        CapitalCostConfig(minimum_beta_weeks=104),
    )

    assert observations >= 150
    assert raw == pytest.approx(1.4)
    assert adjusted == pytest.approx(1.4)


def test_robust_beta_keeps_all_horizon_frequency_estimates():
    dates = pd.bdate_range("2020-01-02", periods=1500)
    market_returns = 0.001 + 0.01 * np.sin(np.arange(len(dates)) / 11.0)
    company_returns = 0.0003 + 0.7 * market_returns
    market = PriceHistoryBundle(
        code="INDEX",
        prices=pd.DataFrame(
            {
                "date": dates,
                "qfq_close": 100.0 * np.cumprod(1.0 + market_returns),
            }
        ),
    )
    company = PriceHistoryBundle(
        code="TEST",
        prices=pd.DataFrame(
            {
                "date": dates,
                "qfq_close": 100.0 * np.cumprod(1.0 + company_returns),
            }
        ),
    )

    result = estimate_robust_beta(company, market, dates[-1].date())

    assert result.beta == pytest.approx(0.7)
    # Weekly compounding creates a tiny nonlinear difference from daily OLS.
    assert result.lower_quartile == pytest.approx(0.7, abs=2e-5)
    assert result.upper_quartile == pytest.approx(0.7, abs=2e-5)
    assert [(item.horizon_years, item.frequency) for item in result.components] == [
        (2, "daily"),
        (2, "weekly"),
        (3, "daily"),
        (3, "weekly"),
        (5, "daily"),
        (5, "weekly"),
    ]
    assert all(item.standard_error is not None for item in result.components)


def test_wacc_uses_market_weights_reported_debt_cost_and_tax():
    statements = [
        FinancialStatementSnapshot(
            period_end=date(2024, 12, 31),
            published_at=date(2025, 3, 31),
            period_type="year",
            source="official",
            short_term_borrowings=40.0,
            long_term_borrowings=40.0,
        ),
        FinancialStatementSnapshot(
            period_end=date(2025, 12, 31),
            published_at=date(2026, 3, 31),
            period_type="year",
            source="official",
            cash_and_cash_equivalents=50.0,
            restricted_cash=10.0,
            short_term_borrowings=50.0,
            long_term_borrowings=50.0,
            interest_expense=-3.0,
            income_tax_expense=20.0,
            profit_before_tax=100.0,
        ),
    ]
    assumptions = CapitalMarketAssumptions(
        as_of=date(2026, 3, 31),
        risk_free_rate=0.02,
        market_risk_premium=0.05,
        risk_free_source="official yield",
        market_risk_premium_source="documented assumption",
    )
    result = estimate_capital_cost(
        evaluation_date=date(2026, 6, 30),
        assumptions=assumptions,
        statements=statements,
        shares=100.0,
        market_price=9.0,
        raw_beta=0.8,
        adjusted_beta=0.8,
        beta_observations=200,
    )

    cost_of_equity = 0.02 + 0.8 * 0.05
    debt_cost = 3.0 / 90.0
    expected = 0.9 * cost_of_equity + 0.1 * debt_cost * 0.8
    assert result.cost_of_equity == pytest.approx(cost_of_equity)
    assert result.pre_tax_cost_of_debt == pytest.approx(debt_cost)
    assert result.effective_tax_rate == pytest.approx(0.2)
    assert result.available_cash == pytest.approx(50.0)
    assert result.net_debt == pytest.approx(50.0)
    assert result.wacc == pytest.approx(expected)
    assert result.discount_rate_kind == "point_in_time_wacc"


def test_capital_cost_range_comes_from_beta_and_erp_ranges():
    assumptions = CapitalMarketAssumptions(
        as_of=date(2026, 3, 31),
        risk_free_rate=0.02,
        market_risk_premium=0.06,
        risk_free_source="official yield",
        market_risk_premium_source="forward implied",
        market_risk_premium_low=0.05,
        market_risk_premium_high=0.07,
    )
    result = estimate_capital_cost(
        evaluation_date=date(2026, 6, 30),
        assumptions=assumptions,
        statements=[],
        shares=100.0,
        market_price=9.0,
        raw_beta=0.8,
        adjusted_beta=0.8,
        beta_observations=200,
        beta_low=0.6,
        beta_high=1.0,
    )

    assert result.cost_of_equity == pytest.approx(0.068)
    assert result.cost_of_equity_low == pytest.approx(0.05)
    assert result.cost_of_equity_high == pytest.approx(0.09)
    assert result.wacc_low is None
    assert result.wacc_high is None


def test_cninfo_parser_extracts_capital_cost_fields(monkeypatch):
    pages = [
        (
            "合并资产负债表 单位：人民币千元 "
            "货币资金85,247,150 140,410,308 "
            "短期借款43,904,550 31,008,549 "
            "一年内到期的非流动负债5,821,777 39,662,733 "
            "长期借款12,658,843 10,491,757 "
            "应付债券3,194,774 3,266,775 "
            "租赁负债1,901,053 1,825,258"
        ),
        (
            "合并利润表 单位：人民币千元 "
            "利息费用(a)(2,211,331)(2,453,361) "
            "所得税费用8,565,147 7,932,532 "
            "利润总额53,085,343 46,689,746"
        ),
        (
            "使用权受到限制的资产 单位：人民币千元 "
            "货币资金(b)16,658,157 84,222,595"
        ),
        (
            "合并现金流量表 单位：人民币千元 "
            "年末现金及现金等价物余额68,508,670 55,118,728 "
            "受限制的货币资金16,658,157 84,222,595"
        ),
        (
            "合并及公司利润表 单位：人民币千元 "
            "减：所得税(费用)/贷项四(62)(8,565,147)(7,932,532)"
        ),
    ]

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Pdf:
        def __init__(self):
            self.pages = [Page(item) for item in pages]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(
        __import__("sys").modules,
        "pdfplumber",
        SimpleNamespace(open=lambda _stream: Pdf()),
    )
    values, _ = CninfoAnnualReportProvider._parse_pdf(b"pdf")

    assert values["cash_and_cash_equivalents"] == 68_508_670_000.0
    assert values["restricted_cash"] == 16_658_157_000.0
    assert values["short_term_borrowings"] == 43_904_550_000.0
    assert values["current_portion_noncurrent_debt"] == 5_821_777_000.0
    assert values["long_term_borrowings"] == 12_658_843_000.0
    assert values["bonds_payable"] == 3_194_774_000.0
    assert values["lease_liabilities"] == 1_901_053_000.0
    assert values["interest_expense"] == -2_211_331_000.0
    assert values["income_tax_expense"] == 8_565_147_000.0
    assert values["profit_before_tax"] == 53_085_343_000.0
