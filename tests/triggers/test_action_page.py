"""Action pagination reads the real store without submitting or sending reports."""

from datetime import datetime, timedelta, timezone

import pytest

from src.triggers import TriggerStore


@pytest.fixture
def actions(tmp_path, monkeypatch):
    monkeypatch.setenv("SKIP_EMAIL", "true")
    monkeypatch.setenv("SKIP_NOTIFICATIONS", "true")
    store = TriggerStore(tmp_path / "triggers.sqlite3")
    scopes = [
        ("alice", "chat-a"),
        ("bob", "chat-a"),
        ("alice", "chat-b"),
        ("alice", "chat-a"),
        ("alice", None),
        ("alice", ""),
        ("alice", "chat-a"),
        ("bob", "chat-a"),
        ("alice", "chat-a"),
    ]
    identities = {}
    for index, (owner, chat) in enumerate(scopes):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc) + timedelta(seconds=index)
        spec = {
            "condition": {"field": "quotes.01339.last", "op": "lt", "value": 5},
            "actions": [
                {
                    "kind": "daily",
                    "payload": {"chat_id": chat} if chat is not None else {},
                }
            ],
        }
        trigger = store.create(owner, spec, f"request-{index}", now=now)
        with store.transaction() as db:
            action = store.record_occurrence(trigger, f"event-{index}", {}, now, db=db)[
                0
            ]
        identities.setdefault((owner, chat), []).append(action["id"])
    return store, identities


def test_pages_filter_owner_and_chat_before_offset_and_end_exactly(actions):
    store, ids = actions
    expected = list(reversed(ids["alice", "chat-a"]))
    first = store.action_page("alice", "chat-a", limit=2)
    assert [row["id"] for row in first["items"]] == expected[:2]
    assert first["next_offset"] == 2
    second = store.action_page("alice", "chat-a", limit=2, offset=first["next_offset"])
    assert [row["id"] for row in second["items"]] == expected[2:]
    assert second["next_offset"] is None
    assert first["coverage_gaps"] == second["coverage_gaps"] == []
    exhausted = store.action_page("alice", "chat-a", limit=2, offset=4)
    assert exhausted["items"] == [] and exhausted["next_offset"] is None
    assert all("payload" not in row and "owner" not in row for row in first["items"])
    assert {row["id"] for row in store.action_page("bob", "chat-a")["items"]} == set(
        ids["bob", "chat-a"]
    )
    assert {row["id"] for row in store.action_page("alice", "chat-b")["items"]} == set(
        ids["alice", "chat-b"]
    )
    assert store.action_page("nobody", "chat-a")["items"] == []


def test_invalid_paging_parameters_are_rejected(actions):
    store, _ = actions
    for limit, offset in [(0, 0), (101, 0), (True, 0), (2, -1), (2, True)]:
        with pytest.raises(ValueError, match="invalid action page"):
            store.action_page("alice", "chat-a", limit=limit, offset=offset)


def test_missing_chat_cannot_match_incomplete_historical_binding(actions):
    store, ids = actions
    assert ids["alice", ""]
    for chat in (None, "", " "):
        with pytest.raises(ValueError, match="authenticated chat"):
            store.action_page("alice", chat)
