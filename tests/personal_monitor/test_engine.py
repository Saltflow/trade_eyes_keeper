"""Boundary replay, durable delivery transitions and real process contention."""

import multiprocessing
from copy import deepcopy

import pytest

from src.personal_monitor import MonitorEngine, MonitorStore, RevisionConflict

NOW = "2026-10-21T06:00:00+00:00"
BINDING = {
    "model": "equity_cashflow_dcf_v1",
    "version": "approved-1",
    "scenario": "base",
    "currency": "CNY",
}


def rule():
    return {
        "enabled": True,
        "route": "chat",
        "metric": "expected_return",
        "tiers": [
            {"id": "first", "operator": "lt", "value": 0.055},
            {"id": "second", "operator": "lt", "value": 0.05},
        ],
        "bindings": {"601088": deepcopy(BINDING)},
    }


def observation(value=0.054):
    return {
        "expected_return": value,
        "quote": {
            "as_of": NOW,
            "price": 35.0,
            "currency": "CNY",
            "source": "test-fixed-provider",
            "market_open": True,
        },
        "research": deepcopy(BINDING),
    }


@pytest.fixture
def setup(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite")
    store.ensure_account("owner", "account")
    store.register_route("owner", "chat")
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 115}], NOW, 0
    )
    store.set_rule("owner", "account", "r", rule())
    return store, MonitorEngine(store)


@pytest.mark.parametrize(
    "value,ids",
    [
        (0.055, []),
        (0.054999, ["first"]),
        (0.05, ["first"]),
        (0.049999, ["first", "second"]),
    ],
)
def test_raw_boundaries_and_merged_independent_tiers(setup, value, ids):
    store, engine = setup
    result = engine.evaluate("owner", "account", {"601088": observation(value)}, NOW)
    assert len(result["enqueued"]) == bool(ids)
    tiers = store.status("owner")["accounts"][0]["tiers"]
    assert {t["tier_id"] for t in tiers} == set(ids)
    assert not engine.evaluate("owner", "account", {"601088": observation(value)}, NOW)[
        "enqueued"
    ]
    if ids:
        delivery = engine.claim_delivery("test", NOW)
        assert all(tier in delivery["text"] for tier in ids)
        assert engine.mark_submitted(delivery["id"], delivery["token"])
        assert engine.finish_delivery(delivery["id"], delivery["token"], "sent")
        assert all(
            t["state"] == "sent" for t in store.status("owner")["accounts"][0]["tiers"]
        )


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "0.04", 5.4, True])
def test_invalid_metrics_do_not_consume_tiers(setup, value):
    store, engine = setup
    result = engine.evaluate("owner", "account", {"601088": observation(value)}, NOW)
    assert result["not_ready"] and result["enqueued"] == []
    assert store.status("owner")["accounts"][0]["tiers"] == []


def test_no_cross_instrument_or_account_condition_match(setup):
    store, engine = setup
    store.import_snapshot(
        "owner", "account", [{"instrument": "00700", "quantity": 200}], NOW, 2
    )
    # The low return belongs to another security with no bound research.
    result = engine.evaluate(
        "owner",
        "account",
        {"601088": observation(0.08), "00700": observation(0.04)},
        NOW,
    )
    assert not result["enqueued"]
    assert result["not_ready"][0]["reason"] == "research_binding_missing"
    assert not engine.evaluate("owner", "account", {"AAPL": observation(0.01)}, NOW)[
        "enqueued"
    ]


def test_persistence_clear_rebuy_exception_config_change_and_explicit_reset(setup):
    store, engine = setup
    assert engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)[
        "enqueued"
    ]
    item = engine.claim_delivery("test", NOW)
    engine.mark_submitted(item["id"], item["token"])
    engine.finish_delivery(item["id"], item["token"], "sent")
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 0}], NOW, 2, "manual"
    )
    store.set_exception("owner", "account", "601088.SH", True)
    assert not engine.evaluate("owner", "account", {"601088": observation(0.01)}, NOW)[
        "enqueued"
    ]
    store.import_snapshot(
        "owner",
        "account",
        [{"instrument": "601088", "quantity": 500}],
        NOW,
        3,
        "manual",
    )
    store.set_exception("owner", "account", "601088", False)
    revised = rule()
    revised["tiers"][0]["value"] = 0.06
    store.set_rule("owner", "account", "r", revised)
    restarted = MonitorEngine(MonitorStore(store.path))
    assert not restarted.evaluate(
        "owner", "account", {"601088.SH": observation(0.01)}, NOW
    )["enqueued"]
    assert store.reset_tiers("owner", "account", "r", "601088", "first")["reset"] == 1
    assert restarted.evaluate("owner", "account", {"601088": observation(0.01)}, NOW)[
        "enqueued"
    ]
    tiers = store.status("owner")["accounts"][0]["tiers"]
    assert {t["tier_id"]: t["state"] for t in tiers} == {
        "first": "pending",
        "second": "sent",
    }


def test_stale_quote_missing_binding_and_weekend_close(setup):
    _, engine = setup
    obs = observation()
    obs["quote"]["as_of"] = "2026-10-21T05:44:59+00:00"
    assert (
        engine.evaluate("owner", "account", {"601088": obs}, NOW)["not_ready"][0][
            "reason"
        ]
        == "stale_quote"
    )
    obs["quote"].update(
        as_of="2026-10-16T07:00:00+00:00",
        market_open=False,
        latest_completed_session="2026-10-16",
    )
    assert engine.evaluate(
        "owner", "account", {"601088": obs}, "2026-10-18T06:00:00+00:00"
    )["enqueued"]


def test_us_previous_close_uses_exchange_timezone(setup):
    _, engine = setup
    obs = observation()
    obs["quote"].update(
        as_of="2026-10-20T20:00:00+00:00",
        market_open=False,
        latest_completed_session="2026-10-20",
        market_timezone="America/New_York",
    )
    assert engine.evaluate("owner", "account", {"601088": obs}, NOW)["enqueued"]


def test_unknown_no_retry_failed_retry_and_cas_finish(setup):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation()}, NOW)
    first = engine.claim_delivery("one", NOW)
    assert not engine.finish_delivery(first["id"], "wrong-token", "sent")
    assert not engine.finish_delivery(first["id"], first["token"], "sent")
    assert engine.mark_submitted(first["id"], first["token"])
    engine.finish_delivery(
        first["id"], first["token"], "failed", "platform_rejected", now=NOW
    )
    assert engine.claim_delivery("two", NOW) is None
    second = engine.claim_delivery("two", "2026-10-21T06:01:00+00:00")
    assert second["id"] == first["id"] and second["token"] != first["token"]
    engine.mark_submitted(second["id"], second["token"])
    engine.finish_delivery(second["id"], second["token"], "unknown", "timeout")
    assert engine.claim_delivery("three", NOW) is None
    with pytest.raises(RevisionConflict):
        store.reset_tiers("owner", "account", "r")
    assert engine.resend_unknown("owner", second["id"])["duplicate_possible"]
    with pytest.raises(RevisionConflict):
        store.reset_tiers("owner", "account", "r")
    assert engine.claim_delivery("four", NOW)["id"] == second["id"]


@pytest.mark.parametrize("submitted", [False, True])
def test_worker_crash_becomes_unknown_never_retried(setup, submitted):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation()}, NOW)
    entry = engine.claim_delivery("crashed", NOW)
    if submitted:
        engine.mark_submitted(entry["id"], entry["token"])
    assert engine.claim_delivery("new", "2026-10-21T06:06:00+00:00") is None
    assert store.status("owner")["outbox"][0]["state"] == "unknown"
    assert not engine.finish_delivery(entry["id"], entry["token"], "sent")


def test_cancel_claimed_and_edit_due_jobs_before_submission(setup):
    store, engine = setup
    store.create_reminder(
        "owner",
        "r",
        "2026-10-21T05:59:00+00:00",
        "test",
        "chat",
        now="2026-10-20T00:00:00+00:00",
    )
    assert len(engine.enqueue_due(NOW)) == 1
    item = engine.claim_delivery("worker", NOW)
    assert not store.cancel_reminder("owner", "r", 1)["possibly_sent"]
    assert not engine.mark_submitted(item["id"], item["token"])
    assert not engine.finish_delivery(item["id"], item["token"], "failed")
    assert engine.claim_delivery("worker", NOW) is None
    store.create_reminder(
        "owner",
        "r2",
        "2026-10-21T05:59:00+00:00",
        "test2",
        "chat",
        now="2026-10-20T00:00:00+00:00",
    )
    engine.enqueue_due(NOW)
    store.edit_reminder("owner", "r2", 1, "2026-10-23T00:00:00+00:00", now=NOW)
    assert engine.claim_delivery("worker", NOW) is None
    assert not engine.enqueue_due("2026-10-22T00:00:00+00:00")
    assert len(engine.enqueue_due("2026-10-23T00:00:00+00:00")) == 1


def test_cancel_already_submitted_reports_possible_send(setup):
    store, engine = setup
    store.create_reminder(
        "owner",
        "r",
        "2026-10-21T05:59:00+00:00",
        "test",
        "chat",
        now="2026-10-20T00:00:00+00:00",
    )
    engine.enqueue_due(NOW)
    entry = engine.claim_delivery("worker", NOW)
    assert engine.mark_submitted(entry["id"], entry["token"])
    assert store.cancel_reminder("owner", "r", 1)["possibly_sent"]
    assert engine.finish_delivery(entry["id"], entry["token"], "sent")
    assert store.status("owner")["reminders"][0]["state"] == "cancelled"


def test_outage_catchup_once_and_past_created_no_outbox(setup):
    store, engine = setup
    store.create_reminder(
        "owner",
        "r",
        "2026-10-22T01:50:00+00:00",
        "预计可用，决定是否滚存",
        "chat",
        now=NOW,
    )
    assert not engine.enqueue_due(NOW)
    restarted = MonitorEngine(MonitorStore(store.path))
    assert len(restarted.enqueue_due("2026-10-26T04:00:00+00:00")) == 1
    assert not restarted.enqueue_due("2026-10-26T04:01:00+00:00")
    delivery = restarted.claim_delivery("test", "2026-10-26T04:01:00+00:00")
    assert "2026-10-22" in delivery["text"] and "已到账" not in delivery["text"]


def test_clear_or_pause_after_claim_blocks_threshold_send(setup):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation()}, NOW)
    entry = engine.claim_delivery("worker", NOW)
    store.set_exception("owner", "account", "601088")
    assert not engine.mark_submitted(entry["id"], entry["token"])
    assert store.status("owner")["accounts"][0]["tiers"] == []


def _process_evaluate(path, start, results):
    engine = MonitorEngine(MonitorStore(path))
    start.wait(15)
    results.put(
        engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    )


def _process_claim(path, start, results):
    engine = MonitorEngine(MonitorStore(path))
    start.wait(15)
    results.put(engine.claim_delivery("process", NOW))


def _race(target, path):
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    children = [
        ctx.Process(target=target, args=(path, start, results)) for _ in range(2)
    ]
    for child in children:
        child.start()
    start.set()
    values = [results.get(timeout=30) for _ in children]
    for child in children:
        child.join(15)
        assert child.exitcode == 0
    return values


def test_two_real_processes_enqueue_and_claim_once(setup):
    store, _ = setup
    values = _race(_process_evaluate, store.path)
    assert sum(len(v["enqueued"]) for v in values) == 1
    assert len(store.status("owner")["accounts"][0]["tiers"]) == 2
    deliveries = _race(_process_claim, store.path)
    assert sum(value is not None for value in deliveries) == 1


def test_each_rule_uses_its_bound_observation(setup):
    store, engine = setup
    second = rule()
    second["bindings"]["601088"]["version"] = "approved-2"
    store.set_rule("owner", "account", "r2", second)
    revised = observation(0.04)
    revised["research"]["version"] = "approved-2"
    result = engine.evaluate(
        "owner",
        "account",
        {
            "__rules__": {
                "r": {"601088": observation(0.08)},
                "r2": {"601088": revised},
            }
        },
        NOW,
    )
    assert len(result["enqueued"]) == 1
    assert store.status("owner")["accounts"][0]["tiers"][0]["rule_id"] == "r2"


def test_failed_send_backoff_does_not_starve_new_due_reminder(setup):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation()}, NOW)
    first = engine.claim_delivery("worker", NOW)
    engine.mark_submitted(first["id"], first["token"])
    engine.finish_delivery(first["id"], first["token"], "failed", now=NOW)
    store.create_reminder(
        "owner",
        "due",
        "2026-10-21T05:59:00+00:00",
        "new",
        "chat",
        now="2026-10-20T00:00:00+00:00",
    )
    engine.enqueue_due(NOW)
    next_item = engine.claim_delivery("worker", NOW)
    assert next_item["kind"] == "time"
    engine.mark_submitted(next_item["id"], next_item["token"])
    engine.finish_delivery(
        next_item["id"],
        next_item["token"],
        "sent",
        now=NOW,
        metadata={"platform_message_id": "om-receipt"},
    )
    time_entry = next(
        row for row in store.status("owner")["outbox"] if row["kind"] == "time"
    )
    assert "om-receipt" in time_entry["metadata"]
    assert engine.claim_delivery("worker", NOW) is None
    assert (
        engine.claim_delivery("worker", "2026-10-21T06:01:00+00:00")["id"]
        == first["id"]
    )


def test_bad_screenshot_pauses_affected_rules_until_manual_correction(setup):
    store, engine = setup
    changed = store.import_snapshot(
        "owner",
        "account",
        [
            {
                "instrument": "601088",
                "quantity": None,
                "ready": False,
                "reason": "unreadable_quantity",
            },
            {"instrument": "00700", "quantity": 200},
        ],
        "2026-10-21T06:00:01+00:00",
        2,
    )
    assert changed["pending_issues"] == ["601088"]
    assert changed["applied"] == ["00700"]
    account = store.status("owner")["accounts"][0]
    assert (
        next(p for p in account["positions"] if p["instrument"] == "601088")["quantity"]
        == 115
    )
    result = engine.evaluate("owner", "account", {"601088": observation(0.01)}, NOW)
    assert not result["enqueued"]
    assert (
        next(item for item in result["not_ready"] if item["instrument"] == "601088")[
            "reason"
        ]
        == "unreadable_quantity"
    )
    assert account["tiers"] == []
    store.import_snapshot(
        "owner",
        "account",
        [{"instrument": "601088", "quantity": 123}],
        "2026-10-21T06:00:01+00:00",
        3,
        "manual",
    )
    assert store.status("owner")["accounts"][0]["pending_issues"] == []
    assert engine.evaluate("owner", "account", {"601088": observation(0.01)}, NOW)[
        "enqueued"
    ]
    # A late OCR failure at the manual correction's timestamp cannot pause it.
    store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": None, "ready": False},
        ],
        "2026-10-21T06:00:01+00:00",
        4,
    )
    assert store.status("owner")["accounts"][0]["pending_issues"] == []


def test_not_ready_reasons_persist_and_price_metric_needs_no_research(setup):
    store, engine = setup
    obs = observation(None)
    obs["reason"] = "research_binding_not_available"
    engine.evaluate("owner", "account", {"601088": obs}, NOW)
    assert (
        store.status("owner")["accounts"][0]["evaluations"][0]["reason"]
        == "research_binding_not_available"
    )
    price_rule = {
        "route": "chat",
        "metric": "price",
        "tiers": [{"id": "price", "operator": "gt", "value": 30}],
    }
    store.set_rule("owner", "account", "price-rule", price_rule)
    obs["metrics"] = {"price": 35}
    obs.pop("research")
    result = engine.evaluate("owner", "account", {"601088": obs}, NOW)
    assert len(result["enqueued"]) == 1
    item = engine.claim_delivery("worker", NOW)
    assert engine.mark_submitted(item["id"], item["token"])


def test_pending_group_reset_releases_unfired_tiers_without_touching_history(setup):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation(0.01)}, NOW)
    result = store.reset_tiers("owner", "account", "r", "601088", "first")
    assert result["reset"] == 1
    assert engine.claim_delivery("worker", NOW) is None
    assert engine.evaluate("owner", "account", {"601088": observation(0.01)}, NOW)[
        "enqueued"
    ]


def test_same_observation_replay_keeps_one_pending_outbox(setup):
    store, engine = setup
    first = engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    assert len(first["enqueued"]) == 1
    assert not engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)[
        "enqueued"
    ]
    assert len(store.status("owner")["outbox"]) == 1


def test_failed_old_crossing_is_released_when_current_condition_no_longer_matches(
    setup,
):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    old = engine.claim_delivery("old-worker", NOW)
    assert engine.mark_submitted(old["id"], old["token"])
    engine.finish_delivery(old["id"], old["token"], "failed", now=NOW)
    assert not engine.evaluate("owner", "account", {"601088": observation(0.08)}, NOW)[
        "enqueued"
    ]
    status = store.status("owner")
    assert status["outbox"][0]["state"] == "cancelled"
    assert status["accounts"][0]["tiers"] == []
    assert engine.claim_delivery("later", "2026-10-21T06:02:00+00:00") is None
    assert engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)[
        "enqueued"
    ]


def test_combined_unsent_message_is_rebuilt_with_only_currently_matching_tier(setup):
    _store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    old = engine.claim_delivery("old-worker", NOW)
    changed = engine.evaluate("owner", "account", {"601088": observation(0.054)}, NOW)
    assert len(changed["enqueued"]) == 1
    assert not engine.mark_submitted(old["id"], old["token"])
    current = engine.claim_delivery("new-worker", NOW)
    assert current["payload"]["value"] == 0.054
    assert [tier["id"] for tier in current["payload"]["tiers"]] == ["first"]
    assert engine.mark_submitted(current["id"], current["token"])
    engine.finish_delivery(current["id"], current["token"], "sent", now=NOW)
    engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    remaining = engine.claim_delivery("new-worker", NOW)
    assert [tier["id"] for tier in remaining["payload"]["tiers"]] == ["second"]


@pytest.mark.parametrize(
    "change",
    ["position", "issue", "duplicate", "exception", "rule_text", "binding", "tier"],
)
def test_fact_changes_invalidate_claimed_message_and_release_unsent_tiers(
    setup, change
):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    old = engine.claim_delivery("old-worker", NOW)
    if change in {"position", "issue", "duplicate"}:
        row = {"instrument": "601088", "quantity": 500}
        if change == "issue":
            row.update(ready=False, reason="unclear")
        rows = (
            [row, dict(row, instrument="601088.SH")] if change == "duplicate" else [row]
        )
        store.import_snapshot("owner", "account", rows, "2026-10-21T06:01:00+00:00", 2)
    elif change == "exception":
        store.set_exception("owner", "account", "601088", True)
    else:
        definition = rule()
        if change == "rule_text":
            definition["text"] = "新的提醒文字"
        elif change == "binding":
            definition["bindings"]["601088"]["version"] = "approved-2"
        else:
            definition["tiers"][0]["value"] = 0.056
        store.set_rule("owner", "account", "r", definition)
    assert not engine.mark_submitted(old["id"], old["token"])
    status = store.status("owner")
    assert status["outbox"][0]["state"] == "cancelled"
    assert status["accounts"][0]["tiers"] == []
    if change in {"issue", "duplicate"}:
        assert not engine.evaluate(
            "owner", "account", {"601088": observation(0.049)}, NOW
        )["enqueued"]
        assert status["accounts"][0]["pending_issues"]


@pytest.mark.parametrize("state", ["submitted", "sent", "unknown"])
def test_fact_invalidation_preserves_delivered_or_uncertain_consumed_tiers(
    setup, state
):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    old = engine.claim_delivery("old-worker", NOW)
    assert engine.mark_submitted(old["id"], old["token"])
    if state != "submitted":
        engine.finish_delivery(old["id"], old["token"], state, now=NOW)
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 0}], NOW, 2, "manual"
    )
    status = store.status("owner")
    assert status["outbox"][0]["state"] == state
    assert len(status["accounts"][0]["tiers"]) == 2


@pytest.mark.parametrize("closed", [False, True])
def test_submission_revalidates_quote_expiry_and_releases_unfired_tiers(setup, closed):
    store, engine = setup
    obs = observation(0.049)
    if closed:
        obs["quote"].update(
            market_open=False,
            latest_completed_session="2026-10-21",
            valid_until="2026-10-22T01:30:00+00:00",
        )
        later = "2026-10-22T01:30:00+00:00"
    else:
        later = "2026-10-21T06:16:00+00:00"
    engine.evaluate("owner", "account", {"601088": obs}, NOW)
    old = engine.claim_delivery("worker", NOW)
    assert not engine.mark_submitted(old["id"], old["token"], now=later)
    assert store.status("owner")["accounts"][0]["tiers"] == []


def test_unavailable_current_observation_cancels_pending_threshold(setup):
    store, engine = setup
    engine.evaluate("owner", "account", {"601088": observation(0.049)}, NOW)
    result = engine.evaluate("owner", "account", {}, NOW)
    assert result["not_ready"][0]["reason"] == "observation_unavailable"
    assert store.status("owner")["outbox"][0]["state"] == "cancelled"
    assert not store.status("owner")["accounts"][0]["tiers"]
