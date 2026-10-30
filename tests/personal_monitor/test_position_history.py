"""Temporary databases exercise the real position/history transaction boundary."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.personal_monitor import MonitorStore, RevisionConflict

OLD = "2026-10-24T06:00:00+00:00"
NOW = "2026-10-25T06:00:00+00:00"
LATER = "2026-10-25T07:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    result = MonitorStore(tmp_path / "history.sqlite")
    result.ensure_account("owner", "account")
    result.register_route("owner", "chat-a")
    return result


def imported(store, rows, revision, *, request="import-1", chat="chat-a", **kwargs):
    return store.import_snapshot(
        "owner",
        "account",
        rows,
        kwargs.pop("as_of", NOW),
        revision,
        kwargs.pop("source", "manual"),
        history_scope=chat,
        history_request_id=request,
        **kwargs,
    )


def act(store, action, revision, *, request=None, chat="chat-a", **kwargs):
    return getattr(store, action + "_positions")(
        kwargs.pop("owner", "owner"),
        kwargs.pop("account", "account"),
        chat_scope=chat,
        expected_revision=revision,
        request_id=request or f"{action}-{revision}",
        **kwargs,
    )


def quantities(store):
    positions = store.status("owner", "account")["accounts"][0]["positions"]
    return {row["instrument"]: row["quantity"] for row in positions}


def rows(store, table):
    # All table names are test literals, never user-controlled SQL.
    with store.connection() as db:
        return [
            dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]


def test_delta_only_undo_redo_monotonic_revisions_and_restart(store):
    store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": 100},
            {"instrument": "00700", "quantity": 30},
        ],
        OLD,
        0,
    )
    before_other = rows(store, "positions")[1]
    created = imported(
        store,
        [
            {"instrument": "601088.SH", "quantity": 150},
            {"instrument": "AAPL", "quantity": 20},
        ],
        1,
    )
    assert created["changes"] == [
        {"instrument": "601088", "before_quantity": 100, "after_quantity": 150},
        {"instrument": "AAPL", "before_quantity": None, "after_quantity": 20},
    ]
    assert created["revision"] == 2 and created["history_id"]
    undo = act(store, "undo", 2)
    assert undo["revision"] == 3 and undo["history_id"] == created["history_id"]
    assert quantities(store) == {"601088": 100, "00700": 30}
    assert (
        next(r for r in rows(store, "positions") if r["instrument"] == "00700")
        == before_other
    )
    assert undo["changes"][1]["after_quantity"] is None
    assert undo["audit_source"]["source"] == "history:undo"
    assert store.position_history("owner", "account", chat_scope="chat-a")[
        "redo_available"
    ]
    restarted = MonitorStore(store.path)
    redo = act(restarted, "redo", 3)
    assert redo["revision"] == 4
    assert quantities(restarted) == {"601088": 150, "00700": 30, "AAPL": 20}
    assert rows(restarted, "snapshots") == []


def test_multiple_undo_redo_and_new_branch_cannot_replay_abandoned_node(store):
    first = imported(store, [{"instrument": "601088", "quantity": 10}], 0)
    second = imported(
        store, [{"instrument": "601088", "quantity": 20}], 1, request="import-2"
    )
    assert act(store, "undo", 2)["history_id"] == second["history_id"]
    assert act(store, "undo", 3)["history_id"] == first["history_id"]
    assert quantities(store) == {}
    assert act(store, "redo", 4)["history_id"] == first["history_id"]
    assert act(store, "redo", 5)["history_id"] == second["history_id"]
    act(store, "undo", 6)
    third = imported(
        store, [{"instrument": "601088", "quantity": 40}], 7, request="import-3"
    )
    assert quantities(store) == {"601088": 40}
    unavailable = act(store, "redo", 8)
    assert not unavailable["applied"] and unavailable["revision"] == 8
    assert unavailable["reason"] == "no_position_change_to_redo"
    assert {
        r["history_id"]: r["state"] for r in rows(store, "position_history_entries")
    } == {
        first["history_id"]: "active",
        second["history_id"]: "branched",
        third["history_id"]: "active",
    }
    # Undo returns to the active parent; abandoned history is never revived.
    assert act(store, "undo", 8)["history_id"] == third["history_id"]
    assert quantities(store) == {"601088": 10}


def test_cross_owner_account_and_chat_cannot_undo_another_scope(store):
    imported(store, [{"instrument": "601088", "quantity": 100}], 0)
    store.ensure_account("other", "account")
    store.ensure_account("owner", "elsewhere")
    for kwargs in ({"chat": "chat-b"}, {"owner": "other"}, {"account": "elsewhere"}):
        revision = 1 if kwargs.get("chat") else 0
        result = act(store, "undo", revision, **kwargs)
        assert result["applied"] == [] and result["history_id"] is None
    assert quantities(store) == {"601088": 100}
    summary = store.position_history("owner", "account", chat_scope="chat-b")
    assert summary["last_change"] is None and not summary["undo_available"]


def test_new_other_chat_or_legacy_write_blocks_prior_scope_and_redo(store):
    imported(store, [{"instrument": "601088", "quantity": 100}], 0)
    act(store, "undo", 1)
    imported(
        store,
        [{"instrument": "AAPL", "quantity": 5}],
        2,
        request="other-import",
        chat="chat-b",
    )
    blocked = act(store, "redo", 3)
    assert blocked["applied"] == []
    summary = store.position_history("owner", "account", chat_scope="chat-a")
    assert not summary["redo_available"]
    assert summary["blocked_reason"] == "account_changed_since_history"
    assert quantities(store) == {"AAPL": 5}
    store.import_snapshot(
        "owner",
        "account",
        [{"instrument": "00700", "quantity": 30}],
        NOW,
        3,
        "manual",
    )
    blocked = act(store, "undo", 4, chat="chat-b")
    assert blocked["reason"] == "history_conflict"
    assert quantities(store) == {"AAPL": 5, "00700": 30}


def test_idempotent_import_and_actions_do_not_repeat_or_increment_revision(store):
    initial = imported(
        store, [{"instrument": "601088", "quantity": 100}], 0, snapshot_id="snapshot-1"
    )
    replay = imported(
        store, [{"instrument": "601088", "quantity": 100}], 99, snapshot_id="snapshot-1"
    )
    assert replay == dict(initial, duplicate=True)
    undo = act(store, "undo", 1, request="stable-undo")
    redo = act(store, "redo", 2, request="stable-redo")
    assert act(store, "undo", 3, request="stable-undo") == dict(undo, duplicate=True)
    assert act(store, "redo", 0, request="stable-redo") == dict(redo, duplicate=True)
    assert quantities(store) == {"601088": 100}
    assert store.status("owner")["accounts"][0]["revision"] == 3
    with pytest.raises(ValueError, match="request_id_reused"):
        imported(store, [{"instrument": "601088", "quantity": 200}], 3)
    with pytest.raises(ValueError, match="request_id_reused"):
        act(store, "redo", 3, request="stable-undo")
    with pytest.raises(RevisionConflict):
        act(store, "undo", 0, request="new-undo")


def test_unavailable_request_never_later_undoes_new_modification(store):
    noop = act(store, "undo", 0, request="too-early")
    assert noop["reason"] == "no_position_change_to_undo"
    imported(store, [{"instrument": "601088", "quantity": 100}], 0)
    assert act(store, "undo", 1, request="too-early") == dict(noop, duplicate=True)
    assert quantities(store) == {"601088": 100}


@pytest.mark.parametrize("source", ["manual", "screenshot"])
def test_undo_never_rewinds_watermark_or_revives_old_or_same_time_image(store, source):
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 10}], OLD, 0
    )
    imported(
        store,
        [
            {"instrument": "601088", "quantity": 20},
            {"instrument": "AAPL", "quantity": 7},
        ],
        1,
        source=source,
    )
    act(store, "undo", 2)
    status = store.status("owner")["accounts"][0]
    assert status["as_of"] == NOW
    assert quantities(store) == {"601088": 10}
    for timestamp in (OLD, NOW):
        rejected = imported(
            store,
            [{"instrument": "AAPL", "quantity": 999}],
            3,
            source="screenshot",
            as_of=timestamp,
            request="stale-" + timestamp,
        )
        assert not rejected["applied"] and rejected["revision"] == 3
    # An explicit manual correction still works at the current fact timestamp.
    assert imported(
        store, [{"instrument": "AAPL", "quantity": 8}], 3, request="manual-correction"
    )["applied"] == ["AAPL"]
    assert imported(
        store,
        [{"instrument": "AAPL", "quantity": 9}],
        4,
        source="screenshot",
        as_of=LATER,
        request="fresh-image",
    )["applied"] == ["AAPL"]


def test_pending_issues_are_restored_only_for_actual_delta_and_zero_applied_no_history(
    store,
):
    old = imported(
        store,
        [
            {
                "instrument": "601088",
                "quantity": None,
                "ready": False,
                "reason": "blurred",
            }
        ],
        0,
    )
    assert old["history_id"] is None and old["applied"] == []
    assert not rows(store, "position_history_entries")
    pending_before = rows(store, "pending_issues")
    created = imported(
        store,
        [
            {"instrument": "601088", "quantity": 100},
            {
                "instrument": "AAPL",
                "quantity": None,
                "ready": False,
                "reason": "new ambiguity",
            },
        ],
        1,
        request="import-2",
    )
    assert created["history_id"]
    pending_after = rows(store, "pending_issues")
    act(store, "undo", 2)
    assert rows(store, "pending_issues") == pending_before
    assert quantities(store) == {}
    act(store, "redo", 3)
    assert rows(store, "pending_issues") == pending_after
    assert quantities(store) == {"601088": 100}


def test_legacy_snapshot_duplicate_cannot_create_or_leak_another_chat_history(store):
    created = imported(
        store, [{"instrument": "601088", "quantity": 10}], 0, snapshot_id="shared-image"
    )
    duplicate = imported(
        store, [], 0, chat="chat-b", request="duplicate", snapshot_id="shared-image"
    )
    assert duplicate["duplicate"] and duplicate["history_id"] is None
    assert duplicate["changes"] == []
    assert not store.position_history("owner", "account", chat_scope="chat-b")[
        "undo_available"
    ]
    assert act(store, "undo", 1)["history_id"] == created["history_id"]


def test_history_never_restores_cancelled_threshold_or_changes_sent_or_rules(store):
    imported(store, [{"instrument": "601088", "quantity": 100}], 0)
    with store.connection(write=True) as db:
        for state in ("queued", "sent", "submitted", "unknown"):
            db.execute(
                "INSERT INTO outbox(id,business_key,owner,account,kind,route,text,"
                "payload,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    state,
                    state,
                    "owner",
                    "account",
                    "threshold",
                    "chat-a",
                    "text",
                    json.dumps({"instrument": "601088", "rule_id": state}),
                    state,
                    NOW,
                ),
            )
            db.execute(
                "INSERT INTO tiers VALUES(?,?,?,?,?,?,?)",
                (
                    "owner",
                    "account",
                    "601088",
                    state,
                    "tier",
                    "pending" if state == "queued" else state,
                    state,
                ),
            )
        db.execute(
            "INSERT INTO rules VALUES(?,?,?,?)",
            ("owner", "account", "kept", '{"arbitrary": "unchanged"}'),
        )
        db.execute(
            "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?)",
            ("owner", "reminder", LATER, "keep", "chat-a", 1, "active", "unrelated"),
        )
    kept = {table: rows(store, table) for table in ("rules", "reminders")}
    stable_outbox = [r for r in rows(store, "outbox") if r["state"] != "queued"]
    stable_tiers = [r for r in rows(store, "tiers") if r["state"] != "pending"]
    undo = act(store, "undo", 1)
    assert undo["cancelled_thresholds"] == 1
    act(store, "redo", 2)
    assert {table: rows(store, table) for table in kept} == kept
    assert [r for r in rows(store, "outbox") if r["id"] != "queued"] == stable_outbox
    assert [
        r for r in rows(store, "tiers") if r["outbox_id"] != "queued"
    ] == stable_tiers
    assert (
        next(r for r in rows(store, "outbox") if r["id"] == "queued")["state"]
        == "cancelled"
    )


@pytest.mark.parametrize(
    "failure_table", ["position_history_entries", "position_history_requests"]
)
def test_import_history_failure_rolls_back_positions_revision_watermark_and_snapshot(
    store, failure_table
):
    with store.connection() as db:
        db.execute(
            f"CREATE TRIGGER reject_history BEFORE INSERT ON {failure_table} "
            "BEGIN SELECT RAISE(ABORT,'injected_history_failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected_history_failure"):
        imported(
            store,
            [{"instrument": "601088", "quantity": 10}],
            0,
            snapshot_id="atomic-image",
        )
    for table in (
        "positions",
        "pending_issues",
        "snapshots",
        "snapshot_watermarks",
        "position_history_entries",
        "position_history_requests",
    ):
        assert rows(store, table) == []
    assert store.status("owner")["accounts"][0]["revision"] == 0


def test_undo_receipt_failure_rolls_back_all_history_effects(store):
    imported(store, [{"instrument": "601088", "quantity": 10}], 0)
    with store.connection(write=True) as db:
        db.execute(
            "INSERT INTO outbox(id,business_key,owner,account,kind,route,text,"
            "payload,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "pending-threshold",
                "pending-threshold",
                "owner",
                "account",
                "threshold",
                "chat-a",
                "text",
                '{"instrument":"601088","rule_id":"r"}',
                "queued",
                NOW,
            ),
        )
        db.execute(
            "INSERT INTO tiers VALUES(?,?,?,?,?,?,?)",
            ("owner", "account", "601088", "r", "tier", "pending", "pending-threshold"),
        )
    tables = (
        "accounts",
        "positions",
        "snapshot_watermarks",
        "position_history_entries",
        "position_history_cursors",
        "position_history_requests",
        "position_history_barriers",
        "outbox",
        "tiers",
    )
    before = {table: rows(store, table) for table in tables}
    with store.connection() as db:
        db.execute(
            "CREATE TRIGGER reject_undo BEFORE INSERT ON position_history_requests "
            "WHEN NEW.action='undo' "
            "BEGIN SELECT RAISE(ABORT,'undo_receipt_failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="undo_receipt_failure"):
        act(store, "undo", 1)
    assert {table: rows(store, table) for table in tables} == before
    assert MonitorStore(store.path).position_history(
        "owner", "account", chat_scope="chat-a"
    )["undo_available"]


def test_concurrent_distinct_undo_requests_only_one_changes_account(store):
    imported(store, [{"instrument": "601088", "quantity": 10}], 0)

    def undo(request):
        try:
            return act(store, "undo", 1, request=request)
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(undo, ("one", "two")))
    assert results.count("conflict") == 1
    assert len([r for r in results if isinstance(r, dict) and r["applied"]]) == 1
    assert quantities(store) == {}


def test_concurrent_same_request_returns_one_receipt_without_second_revision(store):
    imported(store, [{"instrument": "601088", "quantity": 10}], 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: act(store, "undo", 1, request="same"), range(2))
        )
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert {r["revision"] for r in results} == {2}
    assert quantities(store) == {}


def test_read_snapshot_conflict_defends_out_of_band_write_without_revision(store):
    imported(store, [{"instrument": "601088", "quantity": 10}], 0)
    with store.connection(write=True) as db:
        db.execute(
            "UPDATE positions SET data=? WHERE instrument='601088'",
            ('{"instrument":"601088","quantity":999}',),
        )
    assert act(store, "undo", 1)["reason"] == "history_conflict"
    assert quantities(store) == {"601088": 999}


def test_abrupt_process_exit_before_receipt_commit_rolls_back_business_and_journal(
    store,
):
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 100}], OLD, 0
    )
    tables = (
        "accounts",
        "positions",
        "pending_issues",
        "snapshot_watermarks",
        "snapshots",
        "position_history_entries",
        "position_history_cursors",
        "position_history_requests",
    )
    before = {table: rows(store, table) for table in tables}
    code = """
import os
import sys
from src.personal_monitor import MonitorStore
from src.personal_monitor import history
store = MonitorStore(sys.argv[1])
history.save_receipt = lambda *args: os._exit(73)
store.import_snapshot(
    "owner", "account", [{"instrument": "601088", "quantity": 999}],
    "2026-10-25T06:00:00+00:00", 1, "manual", snapshot_id="uncommitted-image",
    history_scope="chat-a", history_request_id="uncommitted-import",
)
"""
    crashed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, store.path],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 73, crashed.stderr
    reopened = MonitorStore(store.path)
    assert {table: rows(reopened, table) for table in tables} == before
    assert quantities(reopened) == {"601088": 100}


def test_legacy_rows_survive_history_schema_upgrade(tmp_path):
    path = tmp_path / "legacy.sqlite"
    original = MonitorStore(path)
    original.ensure_account("owner", "account")
    original.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 100}], NOW, 0
    )
    original_data = {
        t: rows(original, t) for t in ("accounts", "positions", "snapshot_watermarks")
    }
    with original.connection() as db:
        for table in (
            "position_history_entries",
            "position_history_cursors",
            "position_history_requests",
            "position_history_barriers",
        ):
            db.execute(f"DROP TABLE {table}")
    upgraded = MonitorStore(path)
    assert {t: rows(upgraded, t) for t in original_data} == original_data
    assert not upgraded.position_history("owner", "account", chat_scope="chat-a")[
        "undo_available"
    ]


@pytest.mark.parametrize(
    "scope,history_request", [(None, "request"), ("chat-a", None), ("", "r")]
)
def test_history_context_must_be_complete_and_nonempty(store, scope, history_request):
    with pytest.raises(ValueError, match="invalid_identity"):
        store.import_snapshot(
            "owner",
            "account",
            [],
            NOW,
            0,
            history_scope=scope,
            history_request_id=history_request,
        )


@pytest.mark.parametrize("invalid_revision", [None, True, -1, "0"])
def test_tracked_mutations_require_an_explicit_integer_cas(store, invalid_revision):
    with pytest.raises(ValueError, match="expected_revision_required"):
        imported(store, [{"instrument": "601088", "quantity": 10}], invalid_revision)
    with pytest.raises(ValueError, match="expected_revision_required"):
        act(store, "undo", invalid_revision)
    assert quantities(store) == {}
