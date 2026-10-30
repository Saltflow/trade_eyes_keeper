"""Exchange-calendar cutoffs prevent false missing-future-bar failures."""

import json
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data.backtest_data import prepare_backtest_data
from src.data.market_calendar import _load_calendar, resolve_market_data_cutoff
from src.data.market_history import (
    CorporateAction,
    PointInTimeMarketStore,
    PriceHistoryBundle,
)


@pytest.mark.parametrize(
    ("code", "requested_end", "observed", "expected"),
    [
        ("600036", "2026-09-23", "2026-09-23T02:00:00+08:00", "2026-09-22"),
        ("00883", "2026-09-23", "2026-09-23T02:00:00+08:00", "2026-09-22"),
        ("VOO", "2026-09-23", "2026-09-23T02:00:00+08:00", "2026-09-21"),
        ("VOO", "2026-09-23", "2026-09-23T04:00:00+08:00", "2026-09-22"),
        ("600036", "2026-09-23", "2026-09-23T14:59:59+08:00", "2026-09-22"),
        ("600036", "2026-09-23", "2026-09-23T15:00:00+08:00", "2026-09-23"),
        ("00883", "2026-09-23", "2026-09-23T15:59:59+08:00", "2026-09-22"),
        ("00883", "2026-09-23", "2026-09-23T16:00:00+08:00", "2026-09-23"),
        ("600036", "2026-09-20", "2026-09-23T19:00:00+08:00", "2026-09-18"),
        ("600036", "2026-02-18", "2026-02-18T19:00:00+08:00", "2026-02-13"),
        ("600036", "2026-02-28", "2026-02-28T19:00:00+08:00", "2026-02-27"),
        ("600036", "2026-10-05", "2026-11-01T19:00:00+08:00", "2026-09-30"),
        ("VOO", "2026-11-27", "2026-11-27T17:59:59+00:00", "2026-11-25"),
        ("VOO", "2026-11-27", "2026-11-27T18:00:00+00:00", "2026-11-27"),
        ("VOO", "2026-03-06", "2026-03-06T20:30:00+00:00", "2026-03-05"),
        ("VOO", "2026-03-09", "2026-03-09T20:30:00+00:00", "2026-03-09"),
        ("00883", "2026-12-24", "2026-12-24T12:00:00+08:00", "2026-12-24"),
        ("00883", "2026-12-24", "2026-12-24T11:59:59+08:00", "2026-12-23"),
    ],
)
def test_last_completed_session_uses_real_exchange_calendar(
    code, requested_end, observed, expected
):
    cutoff = resolve_market_data_cutoff(
        code,
        date.fromisoformat(requested_end),
        as_of=datetime.fromisoformat(observed),
    )

    assert cutoff.effective_end == date.fromisoformat(expected)
    assert cutoff.session_close <= cutoff.as_of
    assert cutoff.requested_end == date.fromisoformat(requested_end)


def _bundle(code, dates):
    dates = pd.to_datetime(dates)
    prices = pd.DataFrame({"date": dates})
    for basis in ("raw", "qfq"):
        for field in ("open", "high", "low", "close"):
            prices[f"{basis}_{field}"] = 10.0
    prices["qfq_factor"] = 1.0
    prices["volume"] = 1000
    prices["tradable"] = True
    return PriceHistoryBundle(code=code, prices=prices, source="mock_source")


def _config(tmp_path):
    return {"point_in_time_data": {"output_dir": str(tmp_path)}}


def test_preparation_keeps_requested_dates_and_fetches_each_market_cutoff(
    tmp_path, monkeypatch
):
    requested = []

    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, code, start, end):
            requested.append((code, start, end))
            return _bundle(code, ["2026-09-01", end])

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    report = tmp_path / "readiness.json"
    result = prepare_backtest_data(
        _config(tmp_path),
        ["600036", "00883"],
        "2026-09-01",
        "2026-09-23",
        benchmark_codes=["VOO", "risk_free"],
        require_current=True,
        readiness_path=report,
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert result.ready
    assert [(code, end.isoformat()) for code, _, end in requested] == [
        ("600036", "2026-09-22"),
        ("00883", "2026-09-22"),
        ("VOO", "2026-09-21"),
    ]
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["requested_start"] == "2026-09-01"
    assert evidence["requested_end"] == "2026-09-23"
    assert evidence["market_cutoffs"]["600036"]["effective_end"] == "2026-09-22"
    assert evidence["market_cutoffs"]["VOO"]["calendar"] == "XNYS"
    assert evidence["fetched_codes"] == ["600036", "00883", "VOO"]


def test_reused_bundle_excludes_unfinished_bar_without_rewriting_source(
    tmp_path, monkeypatch
):
    store = PointInTimeMarketStore(tmp_path)
    store.write(_bundle("VOO", ["2026-09-01", "2026-09-21", "2026-09-22"]))
    original = (tmp_path / "market" / "VOO.csv").read_bytes()

    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, *_args):
            pytest.fail("completed history is already present")

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["VOO"],
        "2026-09-01",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert result.ready
    assert result.reused_codes == ["VOO"]
    assert result.bundles["VOO"].prices["date"].max().date() == date(2026, 9, 21)
    assert (tmp_path / "market" / "VOO.csv").read_bytes() == original


def test_missing_completed_session_is_not_masked_by_a_future_bar(tmp_path, monkeypatch):
    bundle = _bundle("600036", ["2026-09-01", "2026-09-21", "2026-09-23"])
    store = PointInTimeMarketStore(tmp_path)
    store.write(bundle)
    original = (tmp_path / "market" / "600036.csv").read_bytes()

    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, *_args):
            return bundle

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["600036"],
        "2026-09-01",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert not result.ready
    assert result.bundles == {}
    assert (
        "coverage ends at 2026-09-21, requested 2026-09-22" in result.issues[0].reason
    )
    assert (tmp_path / "market" / "600036.csv").read_bytes() == original


def test_provider_future_rows_are_not_written_to_the_strict_store(
    tmp_path, monkeypatch
):
    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, code, _start, end):
            assert end == date(2026, 9, 22)
            return _bundle(code, ["2026-09-01", "2026-09-22", "2026-09-23"])

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["600036"],
        "2026-09-01",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert result.ready
    stored = PointInTimeMarketStore(tmp_path).read("600036")
    assert stored.prices["date"].max().date() == date(2026, 9, 22)
    assert result.bundles["600036"].prices["date"].max().date() == date(2026, 9, 22)


def test_failed_refresh_preserves_old_bundle_and_source_error(tmp_path, monkeypatch):
    store = PointInTimeMarketStore(tmp_path)
    store.write(_bundle("00883", ["2026-09-01"]))
    original = (tmp_path / "market" / "00883.csv").read_bytes()

    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, *_args):
            raise RuntimeError("HTTP 403 Forbidden")

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["00883"],
        "2026-09-01",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert not result.ready
    assert "fetch=HTTP 403 Forbidden" in result.issues[0].reason
    assert result.issues[0].missing_end == "2026-09-23"
    assert result.market_cutoffs["00883"]["effective_end"] == "2026-09-22"
    assert (tmp_path / "market" / "00883.csv").read_bytes() == original


@pytest.mark.parametrize("failure", ["raw_qfq_identity", "unresolved_action"])
def test_cutoff_does_not_weaken_price_or_action_contract(
    tmp_path, monkeypatch, failure
):
    bundle = _bundle("600036", ["2026-09-01", "2026-09-22"])
    if failure == "raw_qfq_identity":
        bundle.prices.loc[0, "qfq_close"] = 11.0
    else:
        bundle.actions = [
            CorporateAction(
                code="600036",
                action_type="adjustment",
                ex_date=date(2026, 9, 1),
                published_at=date(2026, 9, 1),
                source="mock_source",
            )
        ]

    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, *_args):
            return bundle

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["600036"],
        "2026-09-01",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert not result.ready
    assert not (tmp_path / "market" / "600036.csv").exists()
    expected = (
        "qfq_close != raw_close * qfq_factor"
        if failure == "raw_qfq_identity"
        else "unresolved corporate action factor"
    )
    assert expected in result.issues[0].reason


def test_calendar_failure_cannot_fall_back_to_weekdays(tmp_path, monkeypatch):
    def unavailable(*_args):
        raise ValueError("calendar unavailable")

    monkeypatch.setattr("src.data.market_calendar._load_calendar", unavailable)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["600036"],
        "2026-09-01",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )

    assert not result.ready
    assert result.issues[0].source == "market_calendar"
    assert "calendar unavailable" in result.issues[0].reason
    assert not list(tmp_path.rglob("*.csv"))


def test_naive_observation_time_is_rejected():
    naive_observation = datetime(2026, 9, 23, 2)  # noqa: DTZ001 - invalid input
    with pytest.raises(ValueError, match="must include a timezone"):
        resolve_market_data_cutoff("600036", date(2026, 9, 23), as_of=naive_observation)


def test_outdated_calendar_dependency_is_rejected(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "exchange_calendars",
        SimpleNamespace(__version__="4.11.3"),
    )
    _load_calendar.cache_clear()
    with pytest.raises(ValueError, match="unsupported exchange-calendars"):
        resolve_market_data_cutoff(
            "600036",
            date(2026, 9, 23),
            as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
        )


def test_window_with_no_completed_session_fails_without_fetch(tmp_path, monkeypatch):
    class Provider:
        def __init__(self, _config):
            pass

        def fetch(self, *_args):
            pytest.fail("a window without any completed sessions cannot fetch")

    monkeypatch.setattr("src.data.backtest_data.MarketHistoryProvider", Provider)
    result = prepare_backtest_data(
        _config(tmp_path),
        ["600036"],
        "2026-09-23",
        "2026-09-23",
        as_of=datetime.fromisoformat("2026-09-23T02:00:00+08:00"),
    )
    assert not result.ready
    assert "no completed session" in result.issues[0].reason
