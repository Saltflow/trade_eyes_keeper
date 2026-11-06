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
        dividend_schedule={"status": "partial"},
    )
    object.__setattr__(session, "ref_portfolio_status", {"nav": 123.0})
    path = capture_report_snapshot(config, session, "daily")
    text = path.read_text(encoding="utf-8")
    assert "never-persist" not in text and '"config"' not in text
    loaded = load_report_snapshot(config)
    assert loaded["stocks_data"]["601088"]["custom_anchor"] == 50.5
    assert loaded["dividend_schedule"]["status"] == "partial"
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
            "announcements": {"601088": [{"title": "公开公告"}]},
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
    assert loaded["announcements"]["601088"][0]["title"] == "公开公告"
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


@pytest.mark.parametrize("older_kind,newer_kind", [("daily", "brief"), ("brief", "daily")])
def test_unavailable_quote_clears_previous_prices_without_losing_fundamentals(
    config, older_kind, newer_kind,
):
    financial_time = "2026-09-30T00:00:00+08:00"
    capture_report_snapshot(
        config,
        {
            "created_at": NOW,
            "stocks_data": {"601088": {
                "date": "2026-10-30", "close": 51.0, "open": 50.0,
                "high": 52.0, "low": 49.0, "prev_close": 50.5,
                "change_pct": 1.0, "dividend_yield": 5.0,
                "roe": 9.0, "roe_as_of": financial_time,
                "dividend_per_share": 2.0,
            }},
        },
        older_kind,
    )
    capture_report_snapshot(
        config,
        {
            "created_at": NOW,
            "stocks_data": {"601088": {
                "quote_status": "unavailable", "quote_as_of": None,
                "quote_time": None, "quote_observed_at": NOW.isoformat(),
                "quote_reason": "source_unavailable", "close": None, "roe": None,
            }},
        },
        newer_kind,
    )
    loaded = load_report_snapshot(config)
    row = loaded["stocks_data"]["601088"]
    assert row["quote_status"] == "unavailable"
    for field in (
        "date", "open", "close", "high", "low", "prev_close",
        "change_pct", "dividend_yield", "quote_time",
    ):
        assert row[field] is None, field
    assert row["roe"] == 9.0 and row["dividend_per_share"] == 2.0
    metadata = loaded["_fact_metadata"]
    assert metadata["stocks_data.601088.roe"]["as_of"] == financial_time
    assert metadata["stocks_data.601088.close"]["as_of"] is None
    assert metadata["stocks_data.601088.close"]["source"] == f"{newer_kind}_report_snapshot"
    assert "stocks_data.601088.prev_close" not in metadata


@pytest.mark.parametrize("status", ["realtime", "delayed", "last_close"])
def test_quote_replacement_keeps_one_source_clock_and_does_not_mix_ohlc(config, status):
    capture_report_snapshot(
        config,
        {"created_at": NOW, "stocks_data": {"601088": {
            "close": 51.0, "low": 48.0, "open": 49.0, "prev_close": 50.0,
        }}},
        "daily",
    )
    source_time = "2026-10-30T15:00:00+08:00"
    observed_time = "2026-10-31T09:59:00+08:00"
    capture_report_snapshot(
        config,
        {"created_at": NOW, "stocks_data": {"601088": {
            "quote_status": status, "close": 52.0,
            "quote_as_of": source_time, "quote_time": NOW.isoformat(),
            "quote_observed_at": observed_time,
        }}},
        "brief",
    )
    loaded = load_report_snapshot(config)
    row = loaded["stocks_data"]["601088"]
    assert row["close"] == 52.0
    assert row["low"] is None and row["open"] is None and row["prev_close"] is None
    metadata = loaded["_fact_metadata"]["stocks_data.601088.close"]
    assert metadata["as_of"] == source_time
    assert metadata["observed_at"] == observed_time


@pytest.mark.parametrize("source_time", [None, "2026-10-31T10:00:00", "bad-clock"])
def test_quote_metadata_never_borrows_capture_or_naive_clock(config, source_time):
    capture_report_snapshot(
        config,
        {"created_at": NOW, "stocks_data": {"601088": {
            "quote_status": "unavailable", "close": None,
            "quote_as_of": source_time, "quote_time": NOW.isoformat(),
            "quote_observed_at": "2026-10-31T10:00:00",
        }}},
        "brief",
    )
    loaded = load_report_snapshot(config)
    metadata = loaded["_fact_metadata"]["stocks_data.601088.close"]
    assert metadata["as_of"] is None
    assert metadata["observed_at"] is None


def test_retired_report_roots_are_not_captured_or_restored_from_old_snapshots(config):
    roots = {
        "alerts", "signal_scan", "portfolio_results", "backtest", "evaluation_reports",
        "opt_data_a",
        "opt_data_non_a", "opt_data_hk", "opt_data_us",
    }
    old_values = {root: {"old_value": 1} for root in roots}
    path = capture_report_snapshot(
        config, {"created_at": NOW, "dividend_events": [], **old_values}, "daily"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert roots.isdisjoint(payload["data"])
    # Historical files stay in place, but their retired sections are not facts.
    payload["data"].update(old_values)
    for root in roots:
        payload["fact_metadata"][root + ".old_value"] = {
            "as_of": NOW.isoformat(), "observed_at": NOW.isoformat(),
            "source": "historical_snapshot",
        }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_report_snapshot(config)
    assert roots.isdisjoint(loaded)
    assert not any(
        key.split(".")[0] in roots for key in loaded["_fact_metadata"]
    )


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
