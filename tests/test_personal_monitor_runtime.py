"""Deterministic runtime acceptance: no network, model call or real delivery."""

from datetime import datetime
from unittest.mock import Mock

import pytest

from src.personal_monitor_runtime import (
    SourcedObservationProvider,
    bound_cashflow_return,
    exchange_state,
    instrument_identity,
    quote_timestamp,
)


def instant(value="2026-10-21T14:30:00+08:00"):
    return datetime.fromisoformat(value)


def binding(**assumptions):
    return {
        "model": "equity_cashflow_dcf_v1",
        "version": "research-1",
        "scenario": "base",
        "currency": "CNY",
        "assumptions": {
            "cashflow_per_share": 3,
            "growth_first": 0.02,
            "growth_second": 0.02,
            "terminal_growth": 0.02,
            **assumptions,
        },
    }


@pytest.mark.parametrize(
    "symbol, expected",
    [
        ("601088", ("601088", "CNY", "XSHG", "Asia/Shanghai")),
        ("SH601088", ("601088", "CNY", "XSHG", "Asia/Shanghai")),
        ("601088.SS", ("601088", "CNY", "XSHG", "Asia/Shanghai")),
        ("01088", ("01088", "HKD", "XHKG", "Asia/Hong_Kong")),
        ("HK01088", ("01088", "HKD", "XHKG", "Asia/Hong_Kong")),
        ("1088.HK", ("01088", "HKD", "XHKG", "Asia/Hong_Kong")),
        ("AAPL", ("AAPL", "USD", "XNYS", "America/New_York")),
    ],
)
def test_identity_uses_code_not_stock_name(symbol, expected):
    assert instrument_identity(symbol) == expected


def test_bound_return_matches_independent_growing_perpetuity():
    # With all growth rates 2%, the exact perpetuity equation is r = g + CF1/P.
    assert bound_cashflow_return(binding(), 100, "CNY") == pytest.approx(0.0506)
    assert bound_cashflow_return(binding(), 200, "CNY") == pytest.approx(0.0353)
    total = binding(cashflow=3_000_000, shares_outstanding=1_000_000)
    total["assumptions"].pop("cashflow_per_share")
    assert bound_cashflow_return(total, 100, "CNY") == pytest.approx(0.0506)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "dividend_yield"},
        {"version": ""},
        {"scenario": ""},
        {"currency": "HKD"},
    ],
)
def test_missing_or_mismatched_binding_never_substitutes(change):
    definition = binding()
    definition.update(change)
    with pytest.raises(ValueError):
        bound_cashflow_return(definition, 100, "CNY")


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "3", True, -1])
def test_invalid_cashflow_fails_closed(bad):
    with pytest.raises(ValueError):
        bound_cashflow_return(binding(cashflow_per_share=bad), 100, "CNY")


def test_qq_timestamp_is_provider_time_and_market_timezone():
    assert quote_timestamp("20261021142959", "Asia/Shanghai") == instant(
        "2026-10-21T14:29:59+08:00"
    )
    assert quote_timestamp("20261020160000", "America/New_York") == instant(
        "2026-10-20T16:00:00-04:00"
    )
    with pytest.raises(ValueError):
        quote_timestamp("", "Asia/Shanghai")


def test_sourced_provider_ignores_generated_today_and_preserves_true_timestamp():
    fetch = Mock(
        return_value={"time": "20261020150000", "date": "2026-10-21", "close": 100}
    )
    provider = SourcedObservationProvider(
        {},
        quote_fetcher=fetch,
        calendar_resolver=lambda *_: {
            "market_open": True,
            "latest_completed_session": "2026-10-20",
        },
    )
    result = provider({"instrument": "601088", "currency": "CNY"}, binding(), instant())
    assert result["ready"]
    assert result["quote"]["as_of"].startswith("2026-10-20T15:00:00")
    assert result["quote"]["source"] == "qq_realtime"
    fetch.assert_called_once_with("601088")


def test_sourced_provider_missing_timestamp_and_wrong_currency_not_ready():
    provider = SourcedObservationProvider(
        {}, quote_fetcher=lambda _: {"date": "2026-10-21", "close": 100}
    )
    assert not provider({"instrument": "601088"}, binding(), instant())["ready"]
    assert not provider(
        {"instrument": "01088", "currency": "CNY"}, binding(), instant()
    )["ready"]


def test_real_exchange_calendar_weekend_holiday_and_new_session():
    weekend = exchange_state("XSHG", instant("2026-10-24T09:50:00+08:00"))
    assert not weekend["market_open"]
    assert weekend["latest_completed_session"] == "2026-10-23"
    trading = exchange_state("XSHG", instant())
    assert trading["market_open"]
    assert trading["latest_completed_session"] == "2026-10-20"
    holiday = exchange_state("XNYS", instant("2026-12-25T12:00:00-05:00"))
    assert not holiday["market_open"]
    assert holiday["latest_completed_session"] == "2026-12-24"


def test_historical_screenshot_uses_that_sessions_raw_close_not_todays_quote():
    fetch = Mock(
        side_effect=AssertionError("current quote must not verify an old image")
    )
    history = Mock(
        return_value={"price": 39.7, "currency": "CNY", "source": "qq_raw_daily"}
    )
    provider = SourcedObservationProvider(
        {}, quote_fetcher=fetch, history_fetcher=history, clock=lambda: instant()
    )
    result = provider.screenshot_quote("601088", "2026-10-20")
    history.assert_called_once_with("601088", "2026-10-20")
    assert result == {
        "price": 39.7,
        "currency": "CNY",
        "source": "qq_raw_daily",
        "provider_time": "2026-10-20T07:00:00+00:00",
        "market_closed": True,
        "latest_trading_date": "2026-10-20",
        "market_timezone": "Asia/Shanghai",
        "snapshot_date": "2026-10-20",
        "session_date": "2026-10-20",
        "session_close": "2026-10-20T07:00:00+00:00",
        "calendar_as_of": "2026-10-20T23:59:59.999999+08:00",
        "session_verified": True,
    }
    fetch.assert_not_called()


def test_historical_intraday_cannot_use_daily_close_or_current_price():
    fetch, history = Mock(), Mock()
    provider = SourcedObservationProvider(
        {}, quote_fetcher=fetch, history_fetcher=history, clock=lambda: instant()
    )
    with pytest.raises(ValueError, match="intraday"):
        provider.screenshot_quote("601088", "2026-10-20T10:00:00+08:00")
    fetch.assert_not_called()
    history.assert_not_called()


def test_screenshot_today_during_market_cannot_use_stale_provider_date():
    provider = SourcedObservationProvider(
        {},
        clock=lambda: instant(),
        quote_fetcher=lambda _: {
            "close": 40,
            "date": "2026-10-21",
            "time": "20261020150000",
        },
    )
    with pytest.raises(ValueError, match="intraday"):
        provider.screenshot_quote("601088", instant())


def test_us_historical_screenshot_uses_new_york_session_not_beijing_date():
    history = Mock(
        return_value={"price": 250, "currency": "USD", "source": "yahoo_raw_daily"}
    )
    provider = SourcedObservationProvider(
        {}, history_fetcher=history, clock=lambda: instant()
    )
    result = provider.screenshot_quote("AAPL", "2026-10-20")
    history.assert_called_once_with("AAPL", "2026-10-20")
    assert result["provider_time"] == "2026-10-20T20:00:00+00:00"


def test_raw_history_qq_boundary_requests_unadjusted_day(monkeypatch):
    response = Mock()
    response.json.return_value = {
        "data": {
            "sh601088": {
                "day": [["2026-10-20", "40", "41", "42", "39"]],
                "qfqday": [["2026-10-20", "20", "21", "22", "19"]],
            }
        }
    }
    get = Mock(return_value=response)
    monkeypatch.setattr("requests.get", get)
    result = SourcedObservationProvider._raw_history_quote("601088", "2026-10-20")
    assert result["price"] == 41
    assert "fqkline" not in get.call_args.args[0]
    assert (
        get.call_args.kwargs["params"]["param"]
        == "sh601088,day,2026-10-20,2026-10-20,1"
    )


def test_closed_market_requires_closing_observation_not_an_old_intraday_tick():
    provider = SourcedObservationProvider(
        {},
        quote_fetcher=lambda _: {
            "time": "20261021110000",
            "close": 40,
        },
    )
    result = provider(
        {"instrument": "601088"}, binding(), instant("2026-10-21T19:00:00+08:00")
    )
    assert not result["ready"]
    assert "closing observation" in result["reason"]


def test_closed_quote_expires_at_real_next_exchange_opening():
    provider = SourcedObservationProvider(
        {},
        quote_fetcher=lambda _: {
            "time": "20261023150000",
            "close": 100,
        },
    )
    observed = instant("2026-10-24T09:50:00+08:00")
    observation = provider({"instrument": "601088"}, binding(), observed)
    assert observation["ready"]
    assert observation["quote"]["valid_until"] == "2026-10-26T01:30:00+00:00"
    assert exchange_state("XSHG", observed)["next_open"] == instant(
        "2026-10-26T09:30:00+08:00"
    )


@pytest.mark.parametrize("code", ["920001", "430001", "830001", "200001", "900901"])
def test_unsupported_exchange_or_share_currency_is_explicitly_not_ready(code):
    provider = SourcedObservationProvider({}, quote_fetcher=Mock())
    result = provider({"instrument": code}, binding(), instant())
    assert result["ready"] is False
    assert "unsupported" in result["reason"]
    provider.quote_fetcher.assert_not_called()


def test_personal_notification_runtime_and_engine_are_retired():
    import importlib.util

    import src.personal_monitor as personal
    import src.personal_monitor.tasks as clocks
    import src.personal_monitor_runtime as quotes

    assert not hasattr(personal, "MonitorEngine")
    assert not hasattr(quotes, "PersonalMonitorRuntime")
    assert not hasattr(clocks, "PersonalMonitorTasks")
    assert importlib.util.find_spec("src.personal_monitor.engine") is None
