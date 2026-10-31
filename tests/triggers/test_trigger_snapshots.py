"""Snapshot persistence uses only temporary public observation files."""

import json
from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from src.models.schemas import SessionContext, StockPriceData
from src.triggers.facts import load_trigger_facts
from src.triggers.snapshots import capture_report_snapshot, load_report_snapshot

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 31, 10, tzinfo=ZONE)


@pytest.fixture
def config(tmp_path):
    return {"storage": {"data_dir": str(tmp_path)}, "stocks": ["601088"]}


def test_safe_public_snapshot_roundtrip_excludes_config_env_and_secrets(config):
    session = SessionContext(
        session_id="unit",
        created_at=NOW,
        config={"api_key": "never-persist"},
        stocks_data={
            "601088": StockPriceData(
                stock_code="601088",
                close=48.0,
                date=NOW,
                custom_anchor=50.5,
                access_token="never-persist",
            )
        },
        signal_scan={"stocks": {"601088": {"signal": "buy"}}},
    )
    object.__setattr__(session, "ref_portfolio_status", {"nav": 123.0})
    path = capture_report_snapshot(config, session, "daily")
    text = path.read_text(encoding="utf-8")
    assert "never-persist" not in text and '"config"' not in text
    loaded = load_report_snapshot(config)
    assert loaded["stocks_data"]["601088"]["custom_anchor"] == 50.5
    assert loaded["ref_portfolio_status"]["nav"] == 123
    assert loaded["created_at"] == NOW.isoformat()
    assert list(path.parent.glob("*.tmp")) == []


def test_brief_missing_or_none_fields_do_not_erase_daily_and_clocks_stay_per_field(
    config,
):
    capture_report_snapshot(
        config,
        {
            "created_at": "2026-10-30T19:00:00+08:00",
            "stocks_data": {
                "601088": {
                    "close": 51.0,
                    "date": "2026-10-30",
                    "roe": 9.0,
                    "roe_as_of": "2026-09-30T00:00:00+08:00",
                }
            },
            "signal_scan": {"601088": {"signal": "buy"}},
        },
        "daily",
    )
    capture_report_snapshot(
        config,
        {
            "created_at": NOW.isoformat(),
            "stocks_data": {
                "601088": {"close": 49.0, "quote_time": NOW.isoformat(), "roe": None}
            },
        },
        "brief",
    )
    loaded = load_report_snapshot(config)
    assert loaded["stocks_data"]["601088"]["close"] == 49
    assert loaded["stocks_data"]["601088"]["roe"] == 9
    assert loaded["signal_scan"]["601088"]["signal"] == "buy"
    snap = load_trigger_facts(
        {"stocks_data.601088.roe", "stocks_data.601088.close"},
        config,
        as_of=NOW,
        session=loaded,
    )
    assert snap.resolve("stocks_data.601088.roe").as_of == datetime(
        2026, 9, 30, tzinfo=ZONE
    )
    assert snap.resolve("stocks_data.601088.close").as_of == NOW
    assert snap.resolve("stocks_data.601088.roe").source == "daily_report_snapshot"
    assert snap.resolve("stocks_data.601088.close").source == "brief_report_snapshot"


def test_explicit_zero_false_and_empty_collections_remain_known(config):
    capture_report_snapshot(
        config,
        {
            "created_at": NOW,
            "placements": {"601088": {"amount": 1, "approved": True}},
            "dividend_events": [{"amount": 1}],
        },
        "daily",
    )
    capture_report_snapshot(
        config,
        {
            "created_at": NOW,
            "placements": {"601088": {"amount": 0, "approved": False}},
            "dividend_events": [],
        },
        "brief",
    )
    loaded = load_report_snapshot(config)
    assert loaded["placements"]["601088"] == {"amount": 0, "approved": False}
    assert loaded["dividend_events"] == []
    snap = load_trigger_facts({"dividend_events"}, config, as_of=NOW, session=loaded)
    assert snap.resolve("dividend_events").status == "available"


def test_unprepared_brief_defaults_do_not_erase_daily_sections(config):
    capture_report_snapshot(
        config,
        {"created_at": NOW, "dividend_events": [{"stock_code": "601088"}]},
        "daily",
    )
    capture_report_snapshot(
        config, SessionContext(session_id="unit", created_at=NOW), "brief"
    )
    assert len(load_report_snapshot(config)["dividend_events"]) == 1


def test_financial_value_without_source_clock_does_not_borrow_brief_quote_clock(config):
    capture_report_snapshot(
        config,
        {"created_at": NOW, "stocks_data": {"601088": {"quote_time": NOW, "roe": 9.0}}},
        "brief",
    )
    loaded = load_report_snapshot(config)
    fact = load_trigger_facts(
        {"stocks_data.601088.roe"}, config, as_of=NOW, session=loaded
    ).resolve("stocks_data.601088.roe")
    assert fact.value == 9 and fact.as_of is None


def test_old_snapshot_price_cannot_substitute_for_live_quote(config):
    capture_report_snapshot(
        config,
        {
            "created_at": NOW,
            "stocks_data": {"601088": {"close": 49.0, "quote_time": NOW}},
        },
        "daily",
    )
    snap = load_trigger_facts(
        {"quotes.601088.last"},
        config,
        as_of=NOW,
        session=load_report_snapshot(config),
        providers={"quote": lambda code: None},
    )
    assert snap.resolve("quotes.601088.last").status == "unknown"


def test_corrupt_or_oversized_snapshot_is_ignored_without_hiding_good_daily(
    config, monkeypatch
):
    daily = capture_report_snapshot(
        config, {"created_at": NOW, "placements": {"value": 1}}, "daily"
    )
    brief = daily.parent / "last_brief.json"
    brief.write_text("broken-json", encoding="utf-8")
    assert load_report_snapshot(config)["placements"]["value"] == 1
    brief.write_text(json.dumps({"version": 99}), encoding="utf-8")
    assert load_report_snapshot(config)["placements"]["value"] == 1
    monkeypatch.setattr("src.triggers.snapshots.MAX_SNAPSHOT_BYTES", 5)
    assert load_report_snapshot(config) == {}


def test_oversize_and_failed_atomic_write_leave_previous_snapshot_intact(
    config, monkeypatch
):
    path = capture_report_snapshot(
        config, {"created_at": NOW, "placements": {"value": 1}}, "daily"
    )
    before = path.read_bytes()
    monkeypatch.setattr(
        "src.triggers.snapshots.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(OSError, match="disk unavailable"):
        capture_report_snapshot(
            config, {"created_at": NOW, "placements": {"value": 2}}, "daily"
        )
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []
    monkeypatch.setattr("src.triggers.snapshots.MAX_SNAPSHOT_BYTES", 20)
    with pytest.raises(ValueError, match="size limit"):
        capture_report_snapshot(
            config, {"created_at": NOW, "placements": {"value": "x" * 100}}, "daily"
        )
    assert path.read_bytes() == before
