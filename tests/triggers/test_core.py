"""Synthetic facts and temporary shared SQLite exercise real trigger lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.core.execution import ExecutionStore
from src.triggers import (
    TriggerEvaluator,
    TriggerStore,
    collect_dependencies,
    normalize_spec,
)
from src.triggers.evaluator import FALSE, TRUE, UNKNOWN, evaluate_condition
from src.triggers.schedule import due_times, next_due, normalize_schedule

NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)


@dataclass
class Snapshot:
    facts: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    as_of: datetime = NOW
    observed_at: datetime = NOW

    def resolve(self, path):
        return self.facts.get(path, {"status": "unknown", "value": None})


def fact(value, now=NOW, **overrides):
    return {
        "value": value,
        "status": "available",
        "as_of": now,
        "observed_at": now,
        **overrides,
    }


def price_spec(**overrides):
    return {
        "label": "合成测试价格条件",
        "condition": {
            "field": "quotes.601088.last",
            "op": "lt",
            "value": 50,
            "max_age_seconds": 120,
        },
        "actions": [{"kind": "brief", "payload": {}}],
        **overrides,
    }


@pytest.fixture
def store(tmp_path):
    return TriggerStore(tmp_path / "shared.sqlite3")


def test_owner_numbers_never_reused_and_create_is_idempotent_after_edits(store):
    first = store.create("alice", price_spec(), "message1", now=NOW)
    assert first["number"] == 1
    assert store.create("alice", price_spec(), "message1")["number"] == 1
    edited = store.update("alice", 1, {"label": "新标题"}, expected_revision=1)
    assert edited["number"] == 1 and edited["revision"] == 2
    assert store.create("alice", price_spec(), "message1")["label"] == "新标题"
    store.remove("alice", [1])
    assert store.create("alice", price_spec(), "message1")["state"] == "deleted"
    assert store.create("alice", price_spec(), "message2")["number"] == 2
    assert store.create("bob", price_spec(), "message1")["number"] == 1
    assert store.get("bob", 2) is None
    assert store.remove("bob", [2])["not_found"] == [2]
    with pytest.raises(ValueError, match="different"):
        store.create("alice", price_spec(label="different"), "message1")


def test_concurrent_creation_and_duplicate_message_are_atomic(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(
            pool.map(lambda _: store.create("alice", price_spec(), "same"), range(16))
        )
        others = list(
            pool.map(
                lambda i: store.create("alice", price_spec(), f"unique{i}"), range(20)
            )
        )
    assert {item["number"] for item in rows} == {1}
    assert sorted(item["number"] for item in others) == list(range(2, 22))


@pytest.mark.parametrize(
    "field",
    [
        "__class__",
        "quotes.__dict__.token",
        "stocks_data['601088']",
        "a..b",
        "a;exit()",
        "a/b",
        "x." * 50,
    ],
)
def test_fact_paths_are_data_only(field):
    with pytest.raises(ValueError):
        normalize_spec(price_spec(condition={"field": field, "op": "eq", "value": 1}))


def test_no_owner_override_or_unregistered_action_is_accepted():
    with pytest.raises(ValueError):
        normalize_spec({**price_spec(), "owner": "victim"})
    with pytest.raises(ValueError):
        normalize_spec(
            price_spec(actions=[{"kind": "shell", "payload": {"code": "anything"}}])
        )


def test_edge_reset_requires_definite_false_and_survives_restart(store):
    store.create("alice", price_spec(), "edge")
    engine = TriggerEvaluator(store)

    def evaluate(value, **metadata):
        return engine.evaluate(
            Snapshot({"quotes.601088.last": fact(value, **metadata)}),
            now=NOW,
            owner="alice",
        )

    assert len(evaluate(49)) == 1
    assert evaluate(48) == []
    assert evaluate(None, status="unknown") == []
    assert evaluate(47) == []  # Unknown never rearms a latched price crossing.
    assert evaluate(51) == []
    assert len(evaluate(49)) == 1
    restarted = TriggerEvaluator(TriggerStore(store.path))
    assert (
        restarted.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW, "alice")
        == []
    )
    assert len(store.actions("alice")) == 2


@pytest.mark.parametrize(
    "metadata",
    [
        {"status": "stale"},
        {"as_of": None},
        {"as_of": NOW - timedelta(minutes=5)},
        {"as_of": NOW + timedelta(seconds=1)},
        {"observed_at": NOW + timedelta(minutes=1)},
    ],
)
def test_unavailable_stale_and_future_prices_do_not_match(store, metadata):
    store.create("alice", price_spec(), "fresh")
    assert (
        TriggerEvaluator(store).evaluate(
            Snapshot({"quotes.601088.last": fact(49, **metadata)}), NOW
        )
        == []
    )
    assert store.evaluation_state("alice", 1)["last_truth"] == UNKNOWN


def test_any_all_and_wildcard_keep_three_valued_logic():
    snapshot = Snapshot(
        {
            "a": fact(0),
            "b": fact(None, status="unknown"),
            "stocks_data.*.roe": fact([fact(10), fact(None, status="unknown")]),
        }
    )
    leaf = lambda path, value: {"field": path, "op": "gt", "value": value}
    assert (
        evaluate_condition({"all": [leaf("a", 1), leaf("b", 1)]}, snapshot, NOW).truth
        == FALSE
    )
    assert (
        evaluate_condition({"any": [leaf("a", -1), leaf("b", 1)]}, snapshot, NOW).truth
        == TRUE
    )
    assert (
        evaluate_condition({"all": [leaf("a", -1), leaf("b", 1)]}, snapshot, NOW).truth
        == UNKNOWN
    )
    assert (
        evaluate_condition(
            {**leaf("stocks_data.*.roe", 5), "quantifier": "any"}, snapshot, NOW
        ).truth
        == TRUE
    )
    assert (
        evaluate_condition(
            {**leaf("stocks_data.*.roe", 5), "quantifier": "all"}, snapshot, NOW
        ).truth
        == UNKNOWN
    )


def test_every_safe_report_field_and_legacy_trigger_root_can_be_dependencies():
    spec = normalize_spec(
        price_spec(
            condition={
                "all": [
                    {
                        "field": "stocks_data.601088.unusual_report_metric",
                        "op": "gte",
                        "value": 3,
                    },
                    {"field": "trigger.roe", "op": "gt", "value": 5},
                ]
            }
        )
    )
    assert collect_dependencies(spec) == {
        "stocks_data.601088.unusual_report_metric",
        "trigger.roe",
    }


def test_once_and_level_policies_are_bounded(store):
    store.create("once", price_spec(match_policy="once"), "m")
    store.create("level", price_spec(match_policy="level", cooldown_seconds=60), "m")
    engine = TriggerEvaluator(store)
    snapshot = Snapshot({"quotes.601088.last": fact(49)})
    assert len(engine.evaluate(snapshot, NOW)) == 2
    assert engine.evaluate(snapshot, NOW + timedelta(seconds=59)) == []
    assert len(engine.evaluate(snapshot, NOW + timedelta(seconds=60))) == 1
    engine.evaluate(Snapshot({"quotes.601088.last": fact(51)}), NOW)
    assert engine.evaluate(snapshot, NOW, owner="once") == []


def test_manual_two_maturities_and_recurring_calendar(store):
    spec = {
        "label": "合成逆回购到期",
        "schedule": {
            "times": ["2026-11-02T09:50:00+08:00", "2026-11-20T09:50:00+08:00"]
        },
        "actions": [{"kind": "reminder", "text": "到期检查"}],
    }
    store.create("alice", spec, "maturities")
    engine = TriggerEvaluator(store)
    early = datetime.fromisoformat("2026-11-02T09:49:59+08:00")
    assert engine.evaluate(now=early) == []
    assert len(engine.evaluate(now=early + timedelta(seconds=1))) == 1
    assert engine.evaluate(now=early + timedelta(minutes=1)) == []
    assert (
        len(engine.evaluate(now=datetime.fromisoformat("2026-11-20T10:00:00+08:00")))
        == 1
    )
    schedule = normalize_schedule({"kind": "weekly", "at": "09:50", "weekdays": [0, 4]})
    monday = datetime.fromisoformat("2026-11-02T09:50:00+08:00")
    assert due_times(schedule, monday) == [monday.astimezone(timezone.utc)]
    assert (
        next_due(schedule, monday).astimezone(Zone()).strftime("%Y-%m-%d %H:%M")
        == "2026-11-06 09:50"
    )


def Zone():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Shanghai")


def test_q3_each_year_event_day_and_each_action_are_independently_unique(store):
    spec = {
        "label": "合成三季报",
        "event": {
            "kind": "announcement.q3_report",
            "report_period": "Q3",
            "day_offsets": [0, 1],
        },
        "actions": [{"kind": "brief"}, {"kind": "daily"}],
    }
    store.create("alice", spec, "event")
    engine = TriggerEvaluator(store)
    publication = datetime.fromisoformat(
        "2026-10-31T20:00:00+08:00"
    )  # Saturday; day0 is immediate.
    event = SimpleNamespace(
        id="notice-2026",
        kind="announcement.q3_report",
        symbol="601088",
        report_period="2026Q3",
        published_at=publication,
    )
    snapshot = Snapshot(events=[event])
    first = engine.evaluate(snapshot, publication)
    assert {item["kind"] for item in first} == {"brief", "daily"}
    assert engine.evaluate(snapshot, publication + timedelta(hours=1)) == []
    assert (
        engine.evaluate(snapshot, datetime.fromisoformat("2026-11-01T09:49:59+08:00"))
        == []
    )
    second = engine.evaluate(
        snapshot, datetime.fromisoformat("2026-11-01T09:50:00+08:00")
    )
    assert len(second) == 2
    assert (
        first[0]["context"]["day_offset"] == 0
        and second[0]["context"]["day_offset"] == 1
    )
    next_publication = datetime.fromisoformat("2027-10-31T21:00:00+08:00")
    event.id, event.report_period, event.published_at = (
        "notice-2027",
        "2027Q3",
        next_publication,
    )
    assert len(engine.evaluate(snapshot, next_publication)) == 2
    assert engine.evaluate(snapshot, next_publication - timedelta(seconds=1)) == []


def test_event_explicit_notification_clock_and_future_deadline(store):
    store.create(
        "alice",
        {
            "event": {
                "kind": "announcement.q3_report",
                "day_offsets": [0, 1],
                "notify_at": "21:00",
                "day_after_time": "10:00",
            },
            "actions": [{"kind": "brief"}],
        },
        "clock",
    )
    publication = datetime.fromisoformat("2026-10-31T20:00:00+08:00")
    snapshot = Snapshot(
        events=[
            {"id": "n", "kind": "announcement.q3_report", "published_at": publication}
        ]
    )
    engine = TriggerEvaluator(store)
    assert engine.evaluate(snapshot, publication) == []
    assert engine.next_deadline(publication, snapshot).astimezone(Zone()).hour == 21
    assert len(engine.evaluate(snapshot, publication + timedelta(hours=1))) == 1
    assert (
        engine.next_deadline(publication + timedelta(hours=2), snapshot)
        .astimezone(Zone())
        .strftime("%m-%d %H:%M")
        == "11-01 10:00"
    )


def test_pause_edit_delete_cancel_only_unsubmitted_and_keep_history(store):
    created = store.create(
        "alice", price_spec(actions=[{"kind": "brief"}, {"kind": "daily"}]), "m"
    )
    engine = TriggerEvaluator(store)
    engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    first, _second = store.claim_pending(limit=2)
    store.mark_submitted(first["id"], "run1")
    result = store.remove("alice", [1, 999, 1])
    assert result["removed"] == [1] and result["not_found"] == [999]
    assert result["submitted_actions"] == 1 and result["uncertain_actions"] == 1
    assert store.claim_pending() == []
    assert not store.action_allowed(first["id"])
    assert store.actions("alice", 1)[0]["state"] in {"submitted", "submitting"}
    assert created["revision"] == 1
    with pytest.raises(KeyError):
        store.update("alice", 1, {"label": "deleted"}, 2)


def test_update_revision_cas_cancels_pending_and_never_reuses_occurrence(store):
    store.create("alice", price_spec(), "m")
    engine = TriggerEvaluator(store)
    engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    updated = store.update("alice", 1, {"label": "edited"}, 1)
    assert updated["revision"] == 2 and store.claim_pending() == []
    assert store.actions("alice")[0]["state"] == "cancelled"
    with pytest.raises(ValueError, match="revision"):
        store.update("alice", 1, {"label": "racing edit"}, 1)
    assert engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW) == []
    store.pause("alice", 1)
    store.resume("alice", 1)
    assert engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW) == []


def test_shared_execution_enqueue_and_action_binding_commit_or_rollback_together(store):
    execution = ExecutionStore(store.path)
    store.create("alice", price_spec(), "m")
    TriggerEvaluator(store).evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    with pytest.raises(RuntimeError, match="simulated"), store.transaction() as db:
        action = store.claim_pending(db=db)[0]
        execution.submit(
            "reports.brief", action["payload"], request_id=action["id"], db=db
        )
        raise RuntimeError("simulated failure before shared commit")
    assert store.actions("alice")[0]["state"] == "pending"
    with execution.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
    with store.transaction() as db:
        action = store.claim_pending(db=db)[0]
        run_id = execution.submit(
            "reports.brief", action["payload"], request_id=action["id"], db=db
        )
        store.mark_submitted(action["id"], run_id, db=db)
    assert execution.get(run_id)["status"] == "queued"
    assert store.actions("alice")[0]["run_id"] == run_id
    assert store.remove("alice", [1])["submitted_actions"] == 1
    assert not store.action_allowed(action["id"])


def test_interrupted_or_uncertain_submission_is_never_automatically_replayed(store):
    store.create("alice", price_spec(), "m")
    engine = TriggerEvaluator(store)
    engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    claimed = store.claim_pending()[0]
    restarted = TriggerStore(store.path)
    assert restarted.claim_pending() == []
    assert restarted.recover_submitting() == 1
    assert restarted.actions("alice")[0]["state"] == "uncertain"
    assert engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW) == []
    with pytest.raises(ValueError):
        restarted.mark_submitted(claimed["id"], "unproven_retry")


def test_once_becomes_done_only_after_outcome_and_keeps_uncertain_receipt(store):
    store.create("alice", price_spec(match_policy="once"), "once")
    engine = TriggerEvaluator(store)
    engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    action = store.claim_pending()[0]
    assert store.get("alice", 1)["state"] == "active"
    assert store.action_allowed(action["id"])
    store.mark_submitted(action["id"], "run1")
    assert store.get("alice", 1)["state"] == "active"
    store.mark_completed(
        action["id"],
        status="unknown",
        reason="acknowledgement lost",
        deliveries={"feishu": {"status": "unknown"}},
    )
    finished = store.get("alice", 1)
    assert finished["state"] == "done"
    assert finished["last_result"]["result"]["status"] == "unknown"
    assert store.list("alice") == []
    assert store.list("alice", include_inactive=True)[0]["number"] == 1
    assert engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW) == []
    # A later durable confirmation reconciles history; it never sends again.
    store.mark_completed(
        action["id"], status="succeeded", deliveries={"feishu": {"status": "sent"}}
    )
    assert store.get("alice", 1)["last_result"]["state"] == "completed"
    with pytest.raises(ValueError, match="history"):
        store.mark_completed(
            action["id"], status="failed", reason="cannot replace delivered history"
        )


def test_all_dated_occurrences_settle_before_done_and_edit_can_add_future_time(store):
    store.create(
        "alice",
        {
            "schedule": {"times": ["2026-11-02T09:50+08:00", "2026-11-20T09:50+08:00"]},
            "actions": [{"kind": "reminder"}, {"kind": "brief"}],
        },
        "dates",
    )
    engine = TriggerEvaluator(store)
    for date, final in (
        ("2026-11-02T09:50+08:00", False),
        ("2026-11-20T09:50+08:00", True),
    ):
        moment = datetime.fromisoformat(date)
        engine.evaluate(now=moment)
        actions = store.claim_pending()
        assert len(actions) == 2
        for index, action in enumerate(actions):
            store.mark_submitted(action["id"], "run" + action["id"])
            store.mark_completed(action["id"], status="succeeded")
            if index == 0:
                assert store.get("alice", 1)["state"] == "active"
        assert store.get("alice", 1)["state"] == ("done" if final else "active")
    original = store.actions("alice")
    changed = store.update(
        "alice", 1, {"schedule": {"at": "2026-12-01T09:50+08:00"}}, expected_revision=1
    )
    assert changed["state"] == "active" and changed["number"] == 1
    assert store.actions("alice") == original
    assert (
        len(engine.evaluate(now=datetime.fromisoformat("2026-12-01T09:50+08:00"))) == 2
    )


def test_two_evaluators_cannot_double_admit_same_crossing(store):
    store.create("alice", price_spec(), "racing-evaluation")
    snapshot = Snapshot({"quotes.601088.last": fact(49)})
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(
            pool.map(
                lambda _: TriggerEvaluator(store).evaluate(snapshot, NOW), range(16)
            )
        )
    assert sum(len(batch) for batch in batches) == 1
    assert len(store.actions("alice")) == 1


def test_kind_filter_keeps_disabled_reports_pending_without_blocking_reminders(store):
    store.create(
        "alice",
        price_spec(
            actions=[{"kind": "daily"}, {"kind": "reminder"}, {"kind": "brief"}]
        ),
        "mixed",
    )
    TriggerEvaluator(store).evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    claimed = store.claim_pending(kinds={"reminder"})
    assert [action["kind"] for action in claimed] == ["reminder"]
    remaining = [
        action for action in store.actions("alice") if action["state"] == "pending"
    ]
    assert {action["kind"] for action in remaining} == {"daily", "brief"}
    assert store.claim_pending(kinds=[]) == []
    with pytest.raises(ValueError, match="kind"):
        store.claim_pending(kinds=["shell"])


def test_reconciling_unchanged_unknown_outcome_performs_no_redundant_write(store):
    store.create("alice", price_spec(), "unknown")
    TriggerEvaluator(store).evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    action = store.claim_pending()[0]
    store.mark_submitted(action["id"], "run")
    store.mark_completed(action["id"], status="unknown", reason="ack lost")
    with store.transaction() as db:
        before = db.total_changes
        store.mark_completed(action["id"], status="unknown", reason="ack lost", db=db)
        assert db.total_changes == before


def test_unknown_fact_poll_does_not_advance_recurring_catchup_cursor(store):
    store.create(
        "alice",
        price_spec(schedule={"kind": "daily", "at": "09:50", "catch_up": True}),
        "catchup",
    )
    engine = TriggerEvaluator(store)
    start = datetime.fromisoformat("2026-10-30T09:00+08:00")
    engine.evaluate(Snapshot({"quotes.601088.last": fact(51, start)}), start)
    engine.evaluate(None, NOW)
    resumed = datetime.fromisoformat("2026-11-01T10:00+08:00")
    actions = engine.evaluate(
        Snapshot({"quotes.601088.last": fact(49, resumed)}), resumed
    )
    assert len(actions) == 3
    assert len({action["occurrence_key"] for action in actions}) == 3


def test_older_evaluation_cannot_rearm_a_newer_threshold_crossing(store):
    store.create("alice", price_spec(), "ordered")
    engine = TriggerEvaluator(store)
    assert len(engine.evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)) == 1
    older = NOW - timedelta(seconds=20)
    assert (
        engine.evaluate(Snapshot({"quotes.601088.last": fact(51, older)}), older) == []
    )
    assert engine.last_outcomes[0]["reasons"] == ["out_of_order_evaluation"]
    newer = NOW + timedelta(seconds=20)
    assert (
        engine.evaluate(Snapshot({"quotes.601088.last": fact(49, newer)}), newer) == []
    )


def test_edit_finished_once_date_rearms_only_new_date_and_retains_old_history(store):
    store.create(
        "alice",
        {
            "schedule": {"at": NOW.isoformat()},
            "match_policy": "once",
            "actions": [{"kind": "reminder"}],
        },
        "once-date",
    )
    engine = TriggerEvaluator(store)
    engine.evaluate(now=NOW)
    first = store.claim_pending()[0]
    store.mark_submitted(first["id"], "first")
    store.mark_completed(first["id"], status="succeeded")
    assert store.get("alice", 1)["state"] == "done"
    with pytest.raises(ValueError, match="future schedule"):
        store.resume("alice", 1)
    later = NOW + timedelta(days=2)
    edited = store.update(
        "alice",
        1,
        {"schedule": {"times": [NOW.isoformat(), later.isoformat()]}},
        expected_revision=1,
    )
    assert edited["number"] == 1 and edited["state"] == "active"
    assert engine.evaluate(now=NOW) == []
    assert len(engine.evaluate(now=later)) == 1
    assert len(store.actions("alice")) == 2
    assert store.get_action(first["id"])["state"] == "completed"


def test_resume_cancelled_once_finishes_without_replaying_its_action(store):
    store.create("alice", price_spec(match_policy="once"), "pause-once")
    engine = TriggerEvaluator(store)
    snapshot = Snapshot({"quotes.601088.last": fact(49)})
    assert len(engine.evaluate(snapshot, NOW)) == 1
    store.pause("alice", 1)
    assert store.actions("alice")[0]["state"] == "cancelled"
    assert store.resume("alice", 1)["state"] == "done"
    assert engine.evaluate(snapshot, NOW) == []


def test_changed_once_condition_rearms_but_label_and_actions_do_not_replay(store):
    store.create("alice", price_spec(match_policy="once"), "changed-condition")
    engine = TriggerEvaluator(store)
    snapshot = Snapshot({"quotes.601088.last": fact(49)})
    engine.evaluate(snapshot, NOW)
    original = store.claim_pending()[0]
    store.mark_submitted(original["id"], "old-rule")
    store.mark_completed(original["id"], status="succeeded")
    assert store.get("alice", 1)["state"] == "done"
    store.update("alice", 1, {"label": "改标题"}, expected_revision=1)
    assert store.get("alice", 1)["state"] == "done"
    assert engine.evaluate(snapshot, NOW) == []
    store.update("alice", 1, {"actions": [{"kind": "daily"}]}, expected_revision=2)
    assert store.get("alice", 1)["state"] == "done"
    assert engine.evaluate(snapshot, NOW) == []
    changed = store.update(
        "alice",
        1,
        {"condition": {"field": "quotes.601088.last", "op": "lt", "value": 55}},
        expected_revision=3,
    )
    assert changed["state"] == "active" and changed["number"] == 1
    state = store.evaluation_state("alice", 1)
    assert state["last_evaluated_at"] is None
    assert '"truth":"unknown"' in state["diagnostic"]
    assert "condition_changed" in state["diagnostic"]
    actions = engine.evaluate(snapshot, NOW)
    assert len(actions) == 1 and actions[0]["kind"] == "daily"
    assert actions[0]["occurrence_key"] == "edge:2"
    assert store.get_action(original["id"])["state"] == "completed"
    assert engine.evaluate(snapshot, NOW) == []


def test_permanent_unknown_receipts_cannot_hide_new_submitted_result(store):
    for owner in ("old", "new"):
        store.create(owner, price_spec(), owner)
    TriggerEvaluator(store).evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    for action in store.claim_pending():
        store.mark_submitted(action["id"], action["owner"])
        if action["owner"] == "old":
            store.mark_completed(action["id"], status="unknown", now=NOW)
    assert store.list_inflight_actions(limit=1)[0]["owner"] == "new"


def test_late_success_is_reconciled_past_permanent_unknown_history(store):
    execution = ExecutionStore(store.path)
    for index in range(3):
        store.create(str(index), price_spec(), str(index))
    TriggerEvaluator(store).evaluate(Snapshot({"quotes.601088.last": fact(49)}), NOW)
    actions = store.claim_pending()
    for index, action in enumerate(actions):
        with store.transaction() as db:
            run_id = execution.submit(
                "reports.brief", {}, request_id=action["id"], db=db
            )
            store.mark_submitted(action["id"], run_id, db=db)
            db.execute(
                "UPDATE execution_runs SET status='unknown' WHERE id=?", (run_id,)
            )
        store.mark_completed(
            action["id"], status="unknown", now=NOW + timedelta(seconds=index)
        )
    assert store.list_inflight_actions(limit=1) == []
    late = actions[-1]
    with store.transaction() as db:
        db.execute(
            "UPDATE execution_runs SET status='succeeded' WHERE id=?",
            (store.get_action(late["id"], db=db)["run_id"],),
        )
    eligible = store.list_inflight_actions(limit=1)
    assert [action["id"] for action in eligible] == [late["id"]]
    store.mark_completed(eligible[0]["id"], status="succeeded")
    assert store.get_action(late["id"])["state"] == "completed"
    assert store.list_inflight_actions(limit=1) == []


def test_explicit_event_symbol_uses_same_canonical_identity_as_facts(store):
    store.create(
        "alice",
        {
            "event": {"kind": "announcement.q3_report", "symbols": [" brk.b "]},
            "actions": [{"kind": "brief"}],
        },
        "us-event",
    )
    snapshot = Snapshot(
        events=[
            {
                "id": "source-q3",
                "kind": "announcement.q3_report",
                "symbol": "BRK-B",
                "published_at": NOW,
            }
        ]
    )
    assert len(TriggerEvaluator(store).evaluate(snapshot, NOW)) == 1
