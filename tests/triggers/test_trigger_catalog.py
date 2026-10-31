"""Safe catalog discovery across every public daily-report section."""

from dataclasses import dataclass

import pytest

from src.models.schemas import SessionContext, StockPriceData
from src.triggers.catalog import build_report_field_catalog, safe_path


def test_complete_stock_schema_and_free_producer_discovery():
    catalog = build_report_field_catalog()
    for field in StockPriceData.__fields__:
        assert catalog.resolve(f"stocks_data.601088.{field}") is not None
    assert catalog.resolve("stocks_data.601088.rsi").producer == "history"
    assert catalog.resolve("stocks_data.00700.pe_ratio").producer == "valuation"
    assert catalog.resolve("stocks_data.00700.roe").availability == "snapshot_only"
    assert catalog.resolve("quotes.601088.last").producer == "quote"
    assert catalog.resolve("events.announcement.q3_report").value_type == "event_list"


def test_dynamic_safe_sections_are_discovered_beyond_legacy_roots():
    @dataclass
    class Reference:
        pending_cash: float
        status: str

    session = SessionContext(
        session_id="unit",
        stocks_data={"601088": StockPriceData(stock_code="601088", custom_anchor=44.5)},
        signal_scan={"stocks": {"601088": {"signal": "buy"}}},
        backtest={"hk": {"max_drawdown": 0.2}},
    )
    object.__setattr__(session, "ref_portfolio_status", Reference(12.5, "ready"))
    catalog = build_report_field_catalog(session)
    for path in (
        "stocks_data.601088.custom_anchor",
        "signal_scan.stocks.601088.signal",
        "backtest.hk.max_drawdown",
        "ref_portfolio_status.pending_cash",
    ):
        assert catalog.resolve(path).availability == "observed"
    assert all(
        "config" not in item["path"].split(".") for item in catalog.describe_fields()
    )


@pytest.mark.parametrize(
    "path",
    [
        "stocks_data.601088.__class__",
        "config.stocks",
        "env.password",
        "x.api_key",
        "x.access_token",
        "x.foo()",
        "x..y",
        "x[0]",
        "x." + "x." * 20,
    ],
)
def test_catalog_rejects_code_private_and_secret_paths(path):
    assert not safe_path(path)
    assert build_report_field_catalog().resolve(path) is None


def test_secret_observed_extras_do_not_enter_catalog():
    catalog = build_report_field_catalog(
        {
            "config": {"password": "never"},
            "research": {"confidence": 0.8, "api_key": "never"},
        }
    )
    assert catalog.resolve("research.confidence") is not None
    assert catalog.resolve("research.api_key") is None
    assert "never" not in str(catalog.describe_fields())


@pytest.mark.parametrize(
    "field",
    [
        "dividend_yield",
        "earnings_growth",
        "roe",
        "amplitude",
        "change_pct",
        "turnover",
    ],
)
def test_percentage_fields_publish_percent_scale_not_fraction(field):
    definition = build_report_field_catalog().resolve(f"stocks_data.601088.{field}")
    assert definition.unit == "percent_0_100"
    assert not definition.description.startswith("日报字段")


@pytest.mark.parametrize(
    "field,unit",
    [
        ("close", "quote_currency"),
        ("low", "quote_currency"),
        ("ma60", "quote_currency"),
        ("pe_ratio", "ratio"),
        ("pb_ratio", "ratio"),
        ("rsi", "index_0_100"),
        ("boll_pct_b", "ratio_0_1"),
    ],
)
def test_known_price_valuation_and_indicator_units(field, unit):
    assert (
        build_report_field_catalog().resolve(f"stocks_data.601088.{field}").unit == unit
    )


def test_quote_change_percent_and_unknown_extra_units_are_explicit():
    catalog = build_report_field_catalog(
        {"stocks_data": {"601088": {"custom_anchor": 0.1}}}
    )
    assert catalog.resolve("quotes.601088.change_pct").unit == "percent_0_100"
    assert catalog.resolve("quotes.601088.last").unit == "quote_currency"
    extra = catalog.resolve("stocks_data.601088.custom_anchor")
    assert extra.unit == "report_native_unknown"
    assert "不能猜测百分数" in extra.description
    deviation = catalog.resolve("alerts.*.percentage_difference")
    assert deviation.unit == "percent_0_100" and "-10" in deviation.description


def test_etf_expensive_valuation_is_snapshot_only_while_equity_is_on_demand():
    catalog = build_report_field_catalog()
    assert catalog.resolve("stocks_data.VOO.pe_ratio").availability == "snapshot_only"
    assert catalog.resolve("stocks_data.510300.pb_ratio").producer == "report_snapshot"
    assert catalog.resolve("stocks_data.601088.pe_ratio").producer == "valuation"
    assert "ETF" in catalog.resolve("stocks_data.*.pe_ratio").source
