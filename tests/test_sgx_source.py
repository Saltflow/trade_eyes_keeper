"""Exercise the public SGX contract without network calls."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from src.data import sgx
from src.data.market_calendar import resolve_market_session
from src.data.web_crawler import StockWebCrawler
from src.instruments.classifier import detect_market, normalize_yahoo_symbol
from src.triggers.facts import _quote_facts


@pytest.fixture
def source(tmp_path, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 11, 3, 10, 0, tzinfo=sgx.ZONE)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(sgx, "datetime", Clock)
    config = {
        "storage": {"authority_root": str(tmp_path)},
        "scheduler": {
            "timezone": "Asia/Shanghai",
            "run_time": "19:00",
            "brief_reports": [{"run_time": "09:50"}, {"run_time": "14:30"}],
        },
    }
    http = Mock()
    from src.platform_composition import collection_expiry

    return (
        sgx.SGXSource(
            config,
            http=http,
            cache_until=lambda observed: collection_expiry(config, observed),
        ),
        http,
        Clock,
    )


def row(code="C38U", **updates):
    return {
        "nc": code,
        "type": "reits" if code == "C38U" else "etfs",
        "n": "Test security",
        "cur": "SGD",
        "lt": 2.3,
        "o": 2.29,
        "h": 2.31,
        "l": 2.28,
        "pv": 2.29,
        "vl": 1.234,
        "trading_time": "20261103_015800",
        **updates,
    }


def response(field, rows, *, status=200, meta="200"):
    result = Mock(status_code=status, headers={})
    result.content = b'{"meta":{}}'
    result.json.return_value = {"meta": {"code": meta}, "data": {field: rows}}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


def test_batch_shared_across_symbols_instances_and_slots(source):
    client, http, clock = source
    http.get.return_value = response("prices", [row(), row("G3B")])
    first = client.quote("C38U.SI")
    other = sgx.SGXSource(client.config, http=http, cache_until=client.cache_until)
    assert other.quote("G3B-SI")["stock_code"] == "G3B.SI"
    assert http.get.call_count == 1
    assert first["quote_time"] == "2026-11-03T09:58:00+08:00"
    assert first["volume"] == 1234
    assert first["delay_seconds"] == 120
    assert first["amount"] is None
    clock.current = clock.current.replace(hour=14, minute=30)
    client.quote("C38U.SI")
    assert http.get.call_count == 2
    clock.current = clock.current.replace(hour=19, minute=0)
    client.quote("C38U.SI")
    assert http.get.call_count == 3


@pytest.mark.parametrize("status,meta", [(429, "200"), (403, "200"), (200, "SGX_4015")])
def test_errors_do_not_retry_even_across_instances(source, status, meta):
    client, http, _ = source
    http.get.return_value = response("prices", [], status=status, meta=meta)
    for instance in (
        client,
        sgx.SGXSource(client.config, http=http, cache_until=client.cache_until),
    ):
        with pytest.raises((requests.HTTPError, ValueError)):
            instance.quote("C38U.SI")
    assert http.get.call_count == 1


def test_history_merges_real_snapshot_and_caches_daily(source):
    client, http, clock = source
    http.get.side_effect = [
        response("prices", [row()]),
        response("historic", [row(trading_time="20261102_091600", lt=2.29)]),
        response("prices", [row(lt=2.31)]),
    ]
    frame = client.history("C38U.SI", 120)
    assert len(frame) == 2
    assert frame.iloc[-1]["close"] == 2.3
    assert frame.attrs["adjustment"] == "unadjusted"
    clock.current = clock.current.replace(hour=14, minute=30)
    assert client.history("C38U.SI").iloc[-1]["close"] == 2.31
    assert http.get.call_count == 3  # One historic request, two batch snapshots.


def test_identity_and_invalid_prices_fail_closed(source):
    client, http, _ = source
    http.get.return_value = response("prices", [row()])
    with pytest.raises(ValueError, match="missing"):
        client.quote("G3B.SI")
    with pytest.raises(ValueError, match="sgx_invalid_ohlc"):
        client._row(row(l=3))
    http.get.side_effect = None
    for value in (None, 0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="sgx_invalid_ohlc"):
            client._row(row(lt=value))


def test_crawler_routes_both_canonical_forms_to_sgx(source, monkeypatch):
    client, http, _ = source
    http.get.return_value = response("prices", [row(), row("G3B")])
    crawler = StockWebCrawler(client.config)
    monkeypatch.setattr(crawler, "_sgx_source", lambda: client)
    assert crawler._normalize_stock_code("SGR.SI")[-1] == "SGR.SI"
    for code in ("C38U.SI", "G3B-SI"):
        assert detect_market(code) == "sg"
        assert normalize_yahoo_symbol(code).endswith(".SI")
        assert crawler.fetch_realtime_quote(code)["source"] == "sgx_public_delayed"
    assert http.get.call_count == 1


def test_singapore_calendar_and_trigger_provenance(source):
    client, http, _ = source
    now = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
    http.get.return_value = response("prices", [row()])
    session = resolve_market_session("C38U-SI", as_of=now)
    assert session.is_open(now)
    assert not session.is_open(now.replace(hour=4, minute=30))  # Lunch break.
    facts = _quote_facts(
        "C38U-SI",
        client.quote("C38U.SI"),
        now=now,
        observed=now,
        session_provider=resolve_market_session,
        max_age=300,
    )
    assert facts["quotes.C38U-SI.last"].status == "available"
    assert facts["quotes.C38U-SI.last"].source == "sgx_public_delayed"
    later = _quote_facts(
        "C38U-SI",
        client.quote("C38U.SI"),
        now=now.replace(minute=10),
        observed=now,
        session_provider=resolve_market_session,
        max_age=300,
    )
    assert later["quotes.C38U-SI.last"].status == "stale"


def test_sgx_half_day_holiday_and_completed_bar_cutoff():
    from src.data.market_calendar import resolve_market_data_cutoff

    noon = datetime(2026, 12, 24, 4, 30, tzinfo=timezone.utc)
    session = resolve_market_session("G3B.SI", as_of=noon)
    assert session.close.hour == 4 and session.close.minute == 16
    assert not session.is_open(noon)
    cutoff = resolve_market_data_cutoff("G3B.SI", noon.date(), as_of=noon)
    assert cutoff.effective_end == noon.date()
    assert resolve_market_session("G3B.SI", as_of=noon.replace(day=25)) is None
