"""Personal facts never invoke reference-account trade semantics."""

from datetime import datetime, timezone

import pytest

from src.personal_monitor import (
    MonitorStore,
    RevisionConflict,
    canonical_instrument,
    reminder_time,
)

NOW = "2026-10-21T06:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    result = MonitorStore(tmp_path / "personal.sqlite")
    result.ensure_account("owner", "account")
    result.register_route("owner", "chat")
    return result


def test_partial_merge_manual_priority_and_account_cas(store):
    result = store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088.SH", "quantity": 115},
            {"instrument": "00700.HK", "quantity": 10},
        ],
        NOW,
        0,
    )
    assert result["revision"] == 1
    store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": 116},
        ],
        NOW,
        1,
        "manual",
    )
    with pytest.raises(RevisionConflict):
        store.import_snapshot(
            "owner",
            "account",
            [
                {"instrument": "601088", "quantity": 20},
            ],
            "2026-10-22T06:00:00+00:00",
            1,
        )
    old = store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": 999},
        ],
        NOW,
        2,
    )
    assert not old["applied"]
    positions = store.status("owner", "account")["accounts"][0]["positions"]
    assert {p["instrument"]: p["quantity"] for p in positions} == {
        "601088": 116,
        "00700": 10,
    }
    # A later manual correction at the same reliable timestamp is allowed.
    assert store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": 117},
        ],
        NOW,
        2,
        "manual",
    )["applied"] == ["601088"]


def test_old_account_snapshot_rejected_including_unseen_rows_and_replay_idempotent(
    store,
):
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 100}], NOW, 0
    )
    result = store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": 900},
            {"instrument": "00700", "quantity": 200},
            {"instrument": "AAPL", "quantity": None},
        ],
        "2026-10-20T06:00:00+00:00",
        1,
        snapshot_id="image-message",
    )
    assert result["applied"] == []
    assert len(result["rejected"]) == 3
    replay = store.import_snapshot(
        "owner", "account", [], NOW, 0, snapshot_id="image-message"
    )
    assert replay["duplicate"] and replay["revision"] == 1
    assert store.status("owner", "account")["accounts"][0]["revision"] == 1


def test_ambiguous_and_duplicate_alias_rows_do_not_write(store):
    result = store.import_snapshot(
        "owner",
        "account",
        [
            {"instrument": "601088", "quantity": 100},
            {"instrument": "601088.SH", "quantity": 200},
            {
                "instrument": "00700",
                "quantity": 100,
                "ready": False,
                "reason": "blurred",
            },
            {"instrument": "0700", "quantity": 100},
        ],
        NOW,
        0,
    )
    assert result["applied"] == []
    assert len(result["rejected"]) == 4
    account = store.status("owner")["accounts"][0]
    assert account["revision"] == 1
    assert account["positions"] == []
    assert {issue["instrument"] for issue in account["pending_issues"]} == {
        "00700",
        "601088",
    }


def test_owners_and_routes_are_isolated(store):
    store.ensure_account("other", "account")
    store.register_route("other", "elsewhere")
    store.import_snapshot(
        "owner", "account", [{"instrument": "601088", "quantity": 100}], NOW, 0
    )
    assert store.status("other")["accounts"][0]["positions"] == []
    with pytest.raises(PermissionError):
        store.create_reminder(
            "other", "r", "2026-11-01T00:00:00+00:00", "test", "chat", now=NOW
        )
    store.create_reminder(
        "owner", "r", "2026-11-01T00:00:00+00:00", "test", "chat", now=NOW
    )
    with pytest.raises(KeyError):
        store.cancel_reminder("other", "r", 1)
    assert store.status("other")["reminders"] == []
    assert store.allowed_routes("owner") == ["chat"]


def test_time_resolution_and_naive_rejection(store):
    created = "2026-12-20T18:42:16+08:00"
    assert (
        reminder_time(now=created, days=28).isoformat() == "2027-01-17T10:42:16+00:00"
    )
    assert (
        reminder_time(on_date="2027-01-01").isoformat() == "2027-01-01T01:50:00+00:00"
    )
    assert reminder_time(on_date="2027-01-01", at="13:37").hour == 5
    with pytest.raises(ValueError, match="timezone"):
        store.import_snapshot("owner", "account", [], datetime(2026, 10, 21), 0)  # noqa: DTZ001


@pytest.mark.parametrize(
    "code,expected",
    [
        ("601088.SH", "601088"),
        ("601088.SS", "601088"),
        ("00700.hk", "00700"),
        ("700.HK", "00700"),
        ("AAPL.US", "AAPL"),
        ("brk.b", "BRK.B"),
        ("000001.SZ", "000001"),
    ],
)
def test_identity_aliases(code, expected):
    assert canonical_instrument(code) == expected


def test_past_due_creation_counts_once_and_same_request_deduplicates(store):
    result = store.create_reminder(
        "owner", "old", "2026-10-20T01:50:00+00:00", "rollover", "chat", "message", NOW
    )
    assert result["immediate"] and result["state"] == "completed"
    repeat = store.create_reminder(
        "owner",
        "different-id",
        "2026-10-23T01:50:00+00:00",
        "rollover",
        "chat",
        "message",
        NOW,
    )
    assert repeat["duplicate"] and repeat["reminder_id"] == "old"
    assert store.status("owner")["outbox"] == []


def test_change_and_cancel_reminder_use_revision(store):
    store.create_reminder(
        "owner", "r", "2026-11-01T00:00:00+00:00", "old", "chat", now=NOW
    )
    edited = store.edit_reminder("owner", "r", 1, "2026-11-02T00:00:00+00:00", now=NOW)
    assert edited["revision"] == 2
    with pytest.raises(RevisionConflict):
        store.cancel_reminder("owner", "r", 1)
    cancelled = store.cancel_reminder("owner", "r", 2)
    assert cancelled["state"] == "cancelled"
    assert datetime.fromisoformat(cancelled["due_at"]).tzinfo == timezone.utc
