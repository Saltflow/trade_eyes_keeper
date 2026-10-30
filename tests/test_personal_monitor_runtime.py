"""Deterministic runtime acceptance: no network, model call or real delivery."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.personal_monitor_runtime import (
    PersonalMonitorRuntime,
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


def runtime(engine=None, store=None, send=None, **kwargs):
    engine = engine or Mock()
    engine.claim_delivery.return_value = None
    return PersonalMonitorRuntime(
        store or Mock(),
        {
            "scheduler": {
                "run_time": "19:00",
                "daily_enabled": False,
                "brief_reports": [
                    {
                        "id": "afternoon",
                        "run_time": "14:30",
                        "enabled": False,
                        "skip_weekends": True,
                    }
                ],
            }
        },
        send or Mock(),
        engine=engine,
        observation_provider=Mock(),
        **kwargs,
    )


def test_market_times_ignore_report_switches_and_apply_weekend_rule():
    monitor = runtime()
    assert monitor.market_slot(monitor.config, instant())
    assert (
        monitor.market_slot(monitor.config, instant("2026-10-24T14:30:00+08:00"))
        is None
    )
    assert monitor.market_slot(monitor.config, instant("2026-10-24T19:00:00+08:00"))
    assert (
        monitor.market_slot(monitor.config, instant("2026-10-21T14:31:00+08:00"))
        is None
    )


def test_deadline_poll_is_independent_of_market_schedule_and_starts_with_catchup():
    monitor = runtime()
    now = instant("2026-10-24T02:00:00+08:00")
    monitor.tick(now)
    monitor.engine.enqueue_due.assert_called_once_with(now)
    monitor.store.list_accounts.assert_not_called()


def test_market_per_rule_binding_and_account_isolation_once_per_slot():
    store = Mock()
    store.list_accounts.return_value = [
        {"owner": "u1", "account": "a"},
        {"owner": "u2", "account": "a"},
    ]
    definitions = [
        {"rule_id": "r1", "definition": {"bindings": {"601088": binding()}}},
        {
            "rule_id": "r2",
            "definition": {
                "bindings": {"601088": {**binding(), "version": "research-2"}}
            },
        },
    ]
    store.status.return_value = {
        "accounts": [
            {
                "positions": [
                    {"instrument": "601088", "quantity": 100},
                    {"instrument": "00700", "quantity": 100},
                    {"instrument": "AAPL", "quantity": 0},
                ],
                "rules": definitions,
                "exceptions": ["00700"],
            }
        ]
    }
    monitor = runtime(store=store)
    monitor.observation_provider.side_effect = lambda position, bound, now: {
        "research": bound
    }
    monitor.check_market(instant())
    monitor.check_market(instant() + timedelta(seconds=20))
    assert monitor.engine.evaluate.call_count == 2
    owner, account, observations, _now = monitor.engine.evaluate.call_args_list[0].args
    assert (owner, account) == ("u1", "a")
    assert (
        observations["__rules__"]["r1"]["601088"]["research"]["version"] == "research-1"
    )
    assert (
        observations["__rules__"]["r2"]["601088"]["research"]["version"] == "research-2"
    )
    assert "00700" not in observations["__rules__"]["r1"]
    assert monitor.observation_provider.call_count == 4


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"outcome": "sent"}, "sent"),
        ({"outcome": "failed", "error": "rejected"}, "failed"),
        ({"outcome": "unknown"}, "unknown"),
        ((False, "Timeout"), "unknown"),
        ((True, "ok"), "unknown"),
        (None, "unknown"),
    ],
)
def test_delivery_requires_explicit_result_and_persists_before_http(response, expected):
    monitor = runtime(send=Mock(return_value=response))
    item = {"id": "d1", "token": "lease", "route": "chat1", "text": "due"}
    monitor.engine.claim_delivery.return_value = item
    monitor.engine.mark_submitted.return_value = True
    order = []
    monitor.engine.mark_submitted.side_effect = lambda *_, **__: (
        order.append("submitted") or True
    )
    monitor.send_callback.side_effect = lambda *_: order.append("http") or response
    monitor.tick(instant(), check_market=False)
    assert order == ["submitted", "http"]
    assert monitor.engine.finish_delivery.call_args.args == ("d1", "lease", expected)
    monitor.send_callback.assert_called_once_with("chat1", "due")


def test_cancel_after_claim_prevents_submission_and_network():
    monitor = runtime()
    monitor.engine.claim_delivery.return_value = {"id": "d", "token": "t"}
    monitor.engine.mark_submitted.return_value = False
    monitor.tick(instant(), check_market=False)
    monitor.send_callback.assert_not_called()
    monitor.engine.finish_delivery.assert_not_called()


def test_exception_after_submission_is_unknown_not_retryable_failure():
    monitor = runtime(send=Mock(side_effect=TimeoutError("timeout")))
    monitor.engine.claim_delivery.return_value = {
        "id": "d",
        "token": "t",
        "route": "c",
        "text": "t",
    }
    monitor.engine.mark_submitted.return_value = True
    monitor.tick(instant(), check_market=False)
    assert monitor.engine.finish_delivery.call_args.args[-1] == "unknown"


def test_naive_datetime_is_rejected_before_any_mutation():
    monitor = runtime()
    with pytest.raises(ValueError):
        monitor.tick(datetime(2026, 10, 21, 14, 30))  # noqa: DTZ001 -- reject naive caller input
    monitor.engine.enqueue_due.assert_not_called()


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


def test_actual_engine_runtime_thresholds_restart_and_due_catchup(tmp_path):
    from src.personal_monitor import MonitorStore

    store = MonitorStore(tmp_path / "monitor.sqlite3")
    store.register_route("u", "chat")
    store.ensure_account("u", "actual")
    store.import_snapshot(
        "u",
        "actual",
        [{"instrument": "601088", "quantity": 100, "currency": "CNY"}],
        instant(),
        0,
        source="manual",
    )
    store.set_rule(
        "u",
        "actual",
        "return",
        {
            "metric": "expected_return",
            "route": "chat",
            "bindings": {"601088": binding()},
            "tiers": [
                {"id": "first", "operator": "lt", "value": 0.055},
                {"id": "second", "operator": "lt", "value": 0.05},
            ],
        },
    )
    quotes = SourcedObservationProvider(
        {}, quote_fetcher=lambda _: {"time": "20261021143000", "close": 200}
    )
    delivered = []
    send = lambda route, text: delivered.append((route, text)) or {"outcome": "sent"}
    config = {"scheduler": {"run_time": "14:30", "daily_enabled": False}}
    monitor = PersonalMonitorRuntime(store, config, send, observation_provider=quotes)
    monitor.tick(instant())
    assert len(delivered) == 1
    assert "first" in delivered[0][1] and "second" in delivered[0][1]
    restored = PersonalMonitorRuntime(
        MonitorStore(store.path), config, send, observation_provider=quotes
    )
    restored.tick(instant() + timedelta(seconds=1))
    assert len(delivered) == 1
    store.create_reminder(
        "u",
        "repo",
        instant() + timedelta(days=1),
        "资金预计到期，决定是否滚存",
        "chat",
        now=instant(),
    )
    restored.tick(instant() + timedelta(days=3, hours=1), check_market=False)
    restored.tick(instant() + timedelta(days=3, hours=1, seconds=1), check_market=False)
    assert len(delivered) == 2
    assert "原定时间" in delivered[-1][1]
    assert store.status("u")["reminders"][0]["state"] == "completed"


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


def test_actual_engine_does_not_retry_uncertain_http_delivery(tmp_path):
    from src.personal_monitor import MonitorStore

    store = MonitorStore(tmp_path / "unknown.sqlite3")
    store.register_route("u", "chat")
    store.create_reminder(
        "u", "repo", instant(), "到期", "chat", now=instant() - timedelta(days=1)
    )
    send = Mock(side_effect=TimeoutError("response lost after HTTP submission"))
    monitor = PersonalMonitorRuntime(store, {}, send, observation_provider=Mock())
    monitor.tick(instant(), check_market=False)
    monitor.tick(instant() + timedelta(seconds=1), check_market=False)
    restored = PersonalMonitorRuntime(
        MonitorStore(store.path), {}, send, observation_provider=Mock()
    )
    restored.tick(instant() + timedelta(hours=1), check_market=False)
    send.assert_called_once()
    assert store.status("u")["reminders"][0]["state"] == "unknown"


def test_background_deadline_runs_while_market_provider_is_blocked(tmp_path):
    import threading

    from src.personal_monitor import MonitorStore

    now = datetime.now(timezone.utc)
    store = MonitorStore(tmp_path / "independent.sqlite3")
    store.register_route("u", "chat")
    store.ensure_account("u", "a")
    store.import_snapshot(
        "u", "a", [{"instrument": "601088", "quantity": 100}], now, 0, source="manual"
    )
    store.set_rule(
        "u",
        "a",
        "return",
        {
            "route": "chat",
            "metric": "expected_return",
            "bindings": {"601088": binding()},
            "tiers": [{"id": "first", "operator": "lt", "value": 0.055}],
        },
    )
    store.create_reminder(
        "u", "repo", now + timedelta(seconds=0.3), "到期", "chat", now=now
    )
    entered, release, delivered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def blocked(*_):
        entered.set()
        release.wait(5)
        return {"ready": False}

    monitor = PersonalMonitorRuntime(
        store,
        {
            "scheduler": {
                "run_time": now.astimezone(
                    __import__("pytz").timezone("Asia/Shanghai")
                ).strftime("%H:%M"),
            }
        },
        lambda *_: delivered.set() or {"outcome": "sent"},
        observation_provider=blocked,
        poll_seconds=0.1,
        config_provider=lambda: {
            "scheduler": {
                "run_time": datetime.now(timezone.utc)
                .astimezone(__import__("pytz").timezone("Asia/Shanghai"))
                .strftime("%H:%M")
            }
        },
    )
    try:
        monitor.start()
        assert entered.wait(3), "market work did not start"
        assert delivered.wait(3), "deadline was blocked behind market work"
        assert not release.is_set()
    finally:
        release.set()
        monitor.stop()


def test_generic_price_rule_works_without_research_but_return_rule_does_not(tmp_path):
    from src.personal_monitor import MonitorEngine, MonitorStore

    store = MonitorStore(tmp_path / "generic.sqlite3")
    store.register_route("u", "chat")
    store.ensure_account("u", "a")
    store.import_snapshot(
        "u",
        "a",
        [{"instrument": "601088", "quantity": 100}],
        instant(),
        0,
        source="manual",
    )
    for rule_id, metric, threshold in (
        ("price", "price", 200),
        ("return", "expected_return", 0.055),
    ):
        store.set_rule(
            "u",
            "a",
            rule_id,
            {
                "route": "chat",
                "metric": metric,
                "tiers": [{"id": "a", "operator": "lt", "value": threshold}],
            },
        )
    provider = SourcedObservationProvider(
        {}, quote_fetcher=lambda _: {"time": "20261021143000", "close": 100}
    )
    observation = provider({"instrument": "601088"}, {}, instant())
    assert observation["metrics"] == {"price": 100}
    result = MonitorEngine(store).evaluate("u", "a", {"601088": observation}, instant())
    assert result["checked"] == 1
    assert len(result["enqueued"]) == 1
    assert result["not_ready"][0]["rule_id"] == "return"


def test_us_completed_close_valid_across_beijing_midnight(tmp_path):
    from src.personal_monitor import MonitorEngine, MonitorStore

    now = instant("2026-10-21T09:50:00+08:00")
    store = MonitorStore(tmp_path / "us.sqlite3")
    store.register_route("u", "chat")
    store.ensure_account("u", "a")
    store.import_snapshot(
        "u", "a", [{"instrument": "AAPL", "quantity": 3}], now, 0, source="manual"
    )
    research = {**binding(), "currency": "USD"}
    store.set_rule(
        "u",
        "a",
        "r",
        {
            "route": "chat",
            "metric": "expected_return",
            "bindings": {"AAPL": research},
            "tiers": [{"id": "a", "operator": "lt", "value": 0.055}],
        },
    )
    provider = SourcedObservationProvider(
        {}, quote_fetcher=lambda _: {"time": "2026-10-20 16:00:00", "close": 100}
    )
    observation = provider({"instrument": "AAPL"}, research, now)
    assert observation["ready"]
    assert observation["quote"]["latest_completed_session"] == "2026-10-20"
    assert observation["quote"]["market_timezone"] == "America/New_York"
    result = MonitorEngine(store).evaluate("u", "a", {"AAPL": observation}, now)
    assert not result["not_ready"]
    assert len(result["enqueued"]) == 1


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
