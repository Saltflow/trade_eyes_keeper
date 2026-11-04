"""The dividend fact source is the same public view consumed by the Bot."""

from datetime import datetime
from unittest.mock import Mock

from zoneinfo import ZoneInfo

from src.triggers.facts import TriggerFactProviders, load_trigger_facts


def test_dividend_fact_reuses_public_schedule_and_surfaces_failure():
    now = datetime(2026, 11, 6, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    path = "events.dividend.next_payment"
    schedule = {
        "events": [{"code": "01339", "payment_date": "2026-11-06"}],
        "monitored_symbols": ["01339"],
        "status": "partial",
    }
    source = Mock(return_value=schedule)
    config = {"stocks": [{"code": "01339"}]}
    providers = TriggerFactProviders(dividends=source)
    fact = load_trigger_facts({path}, config, as_of=now, providers=providers).resolve(
        path
    )
    source.assert_called_once_with(["01339"], now)
    assert fact.value == schedule
    assert fact.status == "partial"
    source.side_effect = OSError("offline")
    fact = load_trigger_facts({path}, config, as_of=now, providers=providers).resolve(
        path
    )
    assert fact.status == "unknown"
    assert fact.error == "offline"


def test_default_dividend_source_does_not_consume_pending_display(monkeypatch):
    from src.data.dividend_events import DailyDividendEventResolver

    now = datetime(2026, 11, 6, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = Mock(return_value={"events": [], "status": "available"})
    monkeypatch.setattr(DailyDividendEventResolver, "public_schedule", source)
    path = "events.dividend.next_payment"
    fact = load_trigger_facts({path}, {"stocks": ["01339"]}, as_of=now).resolve(path)
    assert fact.status == "available"
    assert source.call_args.kwargs["as_of"] == now.date()
    assert source.call_args.args[1]["stock_code"].tolist() == ["01339"]
