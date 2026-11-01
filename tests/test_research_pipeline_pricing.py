"""Official two-currency tariffs and holiday boundary accounting."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.research_pipeline.pricing import (
    CNY_URL,
    URL,
    PriceService,
    parse_prices,
    tariff_phase,
)


def table(cny=False):
    headings = (
        ("缓存命中", "缓存未命中", "百万tokens输出")
        if cny
        else ("cache hit", "cache miss", "output tokens")
    )
    rates = (
        (("0.02", "0.04"), ("1", "2"), ("4", "8"))
        if cny
        else (("0.003", "0.006"), ("0.15", "0.3"), ("0.6", "1.2"))
    )
    result = "deepseek-flash<table>"
    for heading, prices in zip(headings, rates, strict=True):
        for index, price in enumerate(prices):
            phase = (
                ("空闲时段", "高峰时段")[index] if cny else ("OFF-PEAK", "PEAK")[index]
            )
            result += (
                f"<tr><td>{heading}</td><td>{phase}</td><td>{price}元</td></tr>"
                if cny
                else f"<tr><td>{heading}</td><td>{phase}</td><td>${price}</td></tr>"
            )
    return result + "</table>"


class HTTP:
    def get(self, url, **kwargs):
        assert url in {URL, CNY_URL}
        text = table(url == CNY_URL)
        return SimpleNamespace(
            text=text, content=text.encode(), raise_for_status=lambda: None
        )


def test_official_two_currency_sources_are_frozen_and_ratio_not_fx(tmp_path):
    service = PriceService(tmp_path, HTTP())
    snapshot = service.refresh()
    assert snapshot["account_tariff_ratio"] == "0.15"
    assert snapshot["account_tariff_ratio_is_exchange_rate"] is False
    assert service.cached() == snapshot
    assert parse_prices(table(cny=True), cny=True)["rates"]["peak"]["output"] == "8"


def test_holiday_and_weekend_tariffs_use_public_calendar_not_trading_days():
    assert tariff_phase(datetime(2026, 5, 1, 2, tzinfo=timezone.utc)) == "off_peak"
    assert tariff_phase(datetime(2026, 5, 6, 2, tzinfo=timezone.utc)) == "peak"
    assert tariff_phase(datetime(2026, 5, 9, 2, tzinfo=timezone.utc)) == "off_peak"
    assert tariff_phase(datetime(2026, 5, 6, 5, tzinfo=timezone.utc)) == "off_peak"


def test_unknown_holiday_year_cannot_masquerade_as_verified_peak():
    with pytest.raises(ValueError, match="pricing_holiday_calendar_unverified"):
        tariff_phase(datetime(2027, 1, 4, 2, tzinfo=timezone.utc))
