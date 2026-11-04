"""Public dividend dates reuse durable trigger admission and report actions."""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.data.dividend_events import DailyDividendEventResolver
from src.triggers import TriggerEvaluator, TriggerStore, normalize_spec
from src.triggers.facts import FactSnapshot, FactValue

NOW = datetime(2026, 11, 4, 1, 50, tzinfo=timezone.utc)


def spec(**overrides):
    return {
        "label": "下一次公开派息日",
        "event": {"kind": "dividend.next_payment"},
        "actions": [{"kind": "daily", "payload": {"chat_id": "chat"}}],
        **overrides,
    }


def row(code="01339", day="2026-11-06", **values):
    return {
        "code": code,
        "ex_date": "2026-10-07",
        "payment_date": day,
        "source": "hk_dividend_schedule",
        "observed_at": NOW.isoformat(),
        **values,
    }


def snapshot(rows=(), codes=("01339", "01088"), **values):
    return FactSnapshot(
        as_of=NOW,
        observed_at=NOW,
        wildcard_symbols=codes,
        facts={
            "events.dividend.next_payment": FactValue(
                value={
                    "events": list(rows),
                    "monitored_symbols": list(codes),
                    **values,
                },
                status="available",
                observed_at=NOW,
            )
        },
    )


@pytest.fixture
def store(tmp_path):
    result = TriggerStore(tmp_path / "shared.sqlite3")
    result.create("alice", spec(), "create", now=NOW)
    return result


def test_model_defaults_and_active_business_dedup(store):
    normalized = normalize_spec(spec())
    assert normalized["match_policy"] == "once"
    assert normalized["event"]["notify_at"] == "09:50"
    assert store.create("alice", spec(label="不同措辞"), "another")["number"] == 1
    assert store.create("bob", spec(), "another")["number"] == 1
    assert (
        store.create(
            "alice",
            spec(actions=[{"kind": "daily", "payload": {"chat_id": "other"}}]),
            "other",
        )["number"]
        == 2
    )
    with pytest.raises(ValueError, match="full daily"):
        normalize_spec(spec(actions=[{"kind": "brief"}]))
    with pytest.raises(ValueError, match="full daily"):
        normalize_spec(spec(actions=[{"kind": "daily"}, {"kind": "daily"}]))


def test_earliest_same_day_group_and_once_after_restart(store):
    evaluator = TriggerEvaluator(store)
    facts = snapshot(
        [row(), row("01088"), row("00700", "2026-11-09")],
        codes=("01339", "01088", "00700"),
    )
    assert evaluator.evaluate(facts, NOW) == []
    assert evaluator.next_deadline(NOW) == NOW + timedelta(days=2)
    restarted = TriggerEvaluator(TriggerStore(store.path))
    actions = restarted.evaluate(snapshot(), NOW + timedelta(days=2))
    assert len(actions) == 1
    assert [r["code"] for r in actions[0]["context"]["dividends"]] == ["01088", "01339"]
    assert actions[0]["context"]["freshness"] == "unconfirmed"
    assert restarted.evaluate(facts, NOW + timedelta(days=2)) == []


def test_latest_supported_date_replaces_old_date(store):
    evaluator = TriggerEvaluator(store)
    evaluator.evaluate(snapshot([row()]), NOW)
    evaluator.evaluate(snapshot([row(day="2026-11-08")]), NOW + timedelta(hours=1))
    assert evaluator.evaluate(snapshot(), NOW + timedelta(days=2)) == []
    assert len(evaluator.evaluate(snapshot(), NOW + timedelta(days=4))) == 1


def test_explicit_cancellation_removes_candidate_missing_does_not(store):
    evaluator = TriggerEvaluator(store)
    evaluator.evaluate(snapshot([row()]), NOW)
    evaluator.evaluate(snapshot([row(cancelled=True)]), NOW + timedelta(hours=1))
    assert (
        json.loads(store.evaluation_state("alice", 1)["event_state"])["status"]
        == "waiting_for_data"
    )
    assert evaluator.evaluate(snapshot(), NOW + timedelta(days=2)) == []


def test_same_day_late_catchup_but_no_cross_day_replay(store):
    evaluator = TriggerEvaluator(store)
    evaluator.evaluate(snapshot([row()]), NOW)
    assert len(evaluator.evaluate(snapshot(), NOW + timedelta(days=2, hours=7))) == 1
    second = store.create("alice", spec(), "fresh-next", now=NOW)
    # Original rule already fired; new independent rule waits for next date.
    assert second["number"] == 1  # still active until report settles


def test_missed_persisted_target_is_done_and_diagnostic(store):
    evaluator = TriggerEvaluator(store)
    evaluator.evaluate(snapshot([row()]), NOW)
    assert evaluator.evaluate(snapshot(), NOW + timedelta(days=3)) == []
    assert store.get("alice", 1)["state"] == "done"
    assert (
        json.loads(store.evaluation_state("alice", 1)["diagnostic"])["status"]
        == "missed"
    )


def test_missing_data_and_removed_monitor_do_not_fire(store):
    evaluator = TriggerEvaluator(store)
    evaluator.evaluate(snapshot(), NOW)
    assert (
        json.loads(store.evaluation_state("alice", 1)["event_state"])["status"]
        == "waiting_for_data"
    )
    evaluator.evaluate(snapshot([row()]), NOW)
    assert evaluator.evaluate(snapshot(codes=("01088",)), NOW + timedelta(days=2)) == []


def test_public_dates_survive_payday_while_report_pending_expires(tmp_path):
    resolver = DailyDividendEventResolver(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    announcements = {
        "01339": [
            {
                "url": "https://example.org/dividend",
                "date": "2026-09-01",
                "llm_extracted_dividend": {
                    "success": True,
                    "cash_dividend_per_share": 0.1,
                    "ex_rights_date": "2026-10-07",
                    "payment_date": "2026-11-06",
                },
            }
        ]
    }
    result = resolver.public_schedule(
        announcements,
        pd.DataFrame([{"stock_code": "01339"}]),
        as_of=(NOW + timedelta(days=2)).date(),
    )
    assert result["events"][0]["payment_date"] == "2026-11-06"
    assert result["events"][0]["source_url"] == "https://example.org/dividend"
    assert result["status"] == "partial"
    assert any(item["status"] == "unavailable" for item in result["coverage"])
    assert resolver.pending_events(result) == []


def test_public_payment_without_ex_date_is_readable_with_explicit_trigger_gap(tmp_path):
    resolver = DailyDividendEventResolver(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        http_get=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = resolver.public_schedule(
        {
            "601088": [
                {
                    "llm_extracted_dividend": {
                        "success": True,
                        "payment_date": "2026-11-06",
                    }
                }
            ]
        },
        pd.DataFrame([{"stock_code": "601088"}]),
        as_of=NOW.date(),
    )
    assert result["events"][0]["payment_date"] == "2026-11-06"
    assert result["events"][0]["ex_date"] is None
    assert result["events"][0]["trigger_available"] is False
    assert result["coverage"][0]["gaps"]
    assert resolver.pending_events(result) == []


def test_explicit_revision_to_past_date_marks_missed_without_replay(store):
    evaluator = TriggerEvaluator(store)
    evaluator.evaluate(snapshot([row()]), NOW)
    assert (
        evaluator.evaluate(snapshot([row(day="2026-11-03")]), NOW + timedelta(hours=1))
        == []
    )
    assert store.get("alice", 1)["state"] == "done"
    assert (
        json.loads(store.evaluation_state("alice", 1)["diagnostic"])["status"]
        == "missed"
    )


def test_public_schedule_budget_stops_network_and_exposes_remaining_coverage(tmp_path):
    calls = []
    budget_checks = []
    resolver = DailyDividendEventResolver(
        {"point_in_time_data": {"output_dir": str(tmp_path)}},
        http_get=lambda *a, **kw: calls.append(a),
    )

    def remaining_budget():
        budget_checks.append(True)
        if len(budget_checks) > 2:
            raise TimeoutError("source budget exhausted")

    result = resolver.public_schedule(
        {},
        pd.DataFrame([{"stock_code": "01339"}, {"stock_code": "01088"}]),
        as_of=NOW.date(),
        before_read=remaining_budget,
    )
    assert calls == []
    assert len(budget_checks) == 6
    assert len(result["coverage"]) == 6
    assert all(
        item["status"] == "unavailable" and item["error"] == "TimeoutError"
        for item in result["coverage"][2:]
    )
    assert result["status"] == "partial"


def test_public_schedule_preserves_explicit_source_freshness_and_observation(
    tmp_path, monkeypatch
):
    resolver = DailyDividendEventResolver(
        {"point_in_time_data": {"output_dir": str(tmp_path)}}
    )
    original_time = "2026-11-03T08:00:00+00:00"
    monkeypatch.setattr(
        resolver,
        "_announcement_events",
        lambda *a, **kw: [
            row(
                "601088",
                freshness="unconfirmed",
                observed_at=original_time,
            )
        ],
    )
    result = resolver.public_schedule(
        {}, pd.DataFrame([{"stock_code": "601088"}]), as_of=NOW.date()
    )
    assert result["events"][0]["freshness"] == "unconfirmed"
    assert result["events"][0]["observed_at"] == original_time
