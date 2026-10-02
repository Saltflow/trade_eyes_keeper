"""Real SQLite coverage for the durable, owner-scoped conversation journal."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.interactive.assistant.store import (
    CONVERSATION_ERROR_LIMIT,
    CONVERSATION_EVENT_LIMIT,
    CONVERSATION_MAX_EVENTS,
    CONVERSATION_TEXT_LIMIT,
    ProposalStore,
)


@pytest.fixture
def store(tmp_path):
    return ProposalStore(tmp_path / "assistant.sqlite3")


def test_question_answer_and_tool_event_survive_new_instance(store):
    turn = store.begin_turn("chat", "sender", "message", "日报什么时候发？")
    store.finish_turn(turn, "running")
    store.record_turn_event(
        turn, "tool_result", {"name": "read_configuration", "result": {"time": "19:00"}}
    )
    store.finish_turn(turn, "completed", "日报 19:00 发送。")

    restarted = ProposalStore(store.path)
    row = restarted.recent_turns("chat", "sender")[0]
    assert row["id"] == turn
    assert row["question"] == "日报什么时候发？"
    assert row["answer"] == "日报 19:00 发送。"
    assert row["status"] == "completed"
    assert row["created"] <= row["updated"]
    assert row["events"][0]["data"]["result"] == {"time": "19:00"}
    with restarted.connection() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_owner_filter_does_not_expose_another_sender_or_chat(store):
    own = store.begin_turn("chat", "alice", "one", "mine")
    other_sender = store.begin_turn("chat", "bob", "two", "private bob")
    other_chat = store.begin_turn("elsewhere", "alice", "three", "private chat")
    for turn in (own, other_sender, other_chat):
        store.record_turn_event(turn, "tool_args", {"owner_turn": turn})
    rows = store.recent_turns("chat", "alice")
    assert [row["id"] for row in rows] == [own]
    assert rows[0]["events"][0]["data"] == {"owner_turn": own}
    assert store.recent_turns("unknown", "alice") == []


def test_duplicate_message_retains_question_and_does_not_share_cross_owner(store):
    turn = store.begin_turn("chat", "alice", "same", "first")
    assert store.begin_turn("chat", "alice", "same", "changed") == turn
    assert store.recent_turns("chat", "alice")[0]["question"] == "first"
    with pytest.raises(ValueError, match="其他会话"):
        store.begin_turn("chat", "bob", "same", "second")
    with pytest.raises(ValueError, match="其他会话"):
        store.begin_turn("elsewhere", "alice", "same", "second")
    assert store.recent_turns("chat", "bob") == []


def test_empty_message_ids_allow_distinct_direct_invocations(store):
    first = store.begin_turn("chat", "alice", "", "question")
    second = store.begin_turn("chat", "alice", "", "question")
    assert first != second
    assert [row["id"] for row in store.recent_turns("chat", "alice", 1)] == [second]


def test_delivery_failure_preserves_generated_answer_and_prior_error(store):
    turn = store.begin_turn("chat", "alice", "message", "question")
    store.finish_turn(turn, "completed", answer="generated answer")
    store.finish_turn(turn, "delivery_failed", error="Feishu unavailable")
    store.finish_turn(turn, "delivery_failed")
    row = store.recent_turns("chat", "alice")[0]
    assert row["answer"] == "generated answer"
    assert row["error"] == "Feishu unavailable"
    assert row["status"] == "delivery_failed"


def test_restart_only_interrupts_unfinished_work_and_does_not_replay(store):
    for status in ("queued", "running", "completed", "failed", "delivery_failed"):
        turn = store.begin_turn("chat", "alice", status, status)
        store.finish_turn(turn, status, answer="saved partial or final answer")
    restarted = ProposalStore(store.path)
    assert restarted.recover_turns() == 2
    assert restarted.recover_turns() == 0
    rows = {row["message_id"]: row for row in restarted.recent_turns("chat", "alice")}
    for status in ("queued", "running"):
        assert rows[status]["status"] == "interrupted"
        assert "不会自动重放" in rows[status]["error"]
        assert rows[status]["answer"] == "saved partial or final answer"
    for status in ("completed", "failed", "delivery_failed"):
        assert rows[status]["status"] == status


def test_large_text_and_json_are_bounded_with_explicit_truncation(store):
    turn = store.begin_turn("chat", "alice", "message", "问" * 20000)
    store.finish_turn(turn, "failed", answer="答" * 20000, error="错" * 5000)
    store.record_turn_event(turn, "tool_result", {"data": '\n"\\中' * 10000})
    row = store.recent_turns("chat", "alice")[0]
    for field, limit in (
        ("question", CONVERSATION_TEXT_LIMIT),
        ("answer", CONVERSATION_TEXT_LIMIT),
        ("error", CONVERSATION_ERROR_LIMIT),
    ):
        assert len(row[field]) == limit
        assert row[field].endswith("[truncated]")
    assert row["events"][0]["data"]["truncated"] is True
    with store.connection() as db:
        serialized = db.execute("SELECT data FROM conversation_events").fetchone()[0]
    assert len(serialized) <= CONVERSATION_EVENT_LIMIT
    assert json.loads(serialized)["preview"]


def test_event_count_is_bounded_and_overflow_is_visible(store):
    turn = store.begin_turn("chat", "alice", "message", "question")
    for index in range(CONVERSATION_MAX_EVENTS + 5):
        store.record_turn_event(turn, "tool_result", {"index": index})
    events = store.recent_turns("chat", "alice")[0]["events"]
    assert len(events) == CONVERSATION_MAX_EVENTS
    assert events[0]["data"] == {"index": 0}
    assert events[-1]["kind"] == "events_truncated"
    assert events[-1]["data"] == {"truncated": True}


def test_pruning_cascades_events_but_preserves_business_records(store, monkeypatch):
    clock = [1_700_000_000.0]
    monkeypatch.setattr("src.interactive.assistant.store.time.time", lambda: clock[0])
    expired = store.begin_turn("chat", "alice", "old", "old")
    store.record_turn_event(expired, "tool_result", {"old": True})
    action = store.create("config", "chat", "alice", {"schedule": "19:00"})
    assert store.receipt("business-receipt")
    clock[0] += 31 * 86400
    retained = store.begin_turn("chat", "alice", "new", "new")
    store.record_turn_event(retained, "tool_result", {"new": True})
    assert store.prune_conversations() == 1
    assert [row["id"] for row in store.recent_turns("chat", "alice")] == [retained]
    assert store.get(action["id"], "chat", "alice")["payload"] == {"schedule": "19:00"}
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_concurrent_duplicate_delivery_and_event_writes_are_atomic(store):
    # Independent instances/connections exercise SQLite locking, not a Python lock.
    stores = [ProposalStore(store.path) for _ in range(8)]

    def write(index):
        instance = stores[index % len(stores)]
        turn = instance.begin_turn("chat", "alice", "same-message", "question")
        instance.record_turn_event(turn, "tool_result", {"index": index})
        return turn

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(32)))
    assert len(set(ids)) == 1
    rows = store.recent_turns("chat", "alice")
    assert len(rows) == 1
    assert {event["data"]["index"] for event in rows[0]["events"]} == set(range(32))


@pytest.mark.parametrize("limit", [0, -1, 51, 2.5, True, "10"])
def test_invalid_query_limits_fail(store, limit):
    with pytest.raises(ValueError):
        store.recent_turns("chat", "alice", limit)


@pytest.mark.parametrize("days", [0, -1, 1.5, True])
def test_invalid_retention_cannot_delete_records(store, days):
    store.begin_turn("chat", "alice", "message", "question")
    with pytest.raises(ValueError):
        store.prune_conversations(days)
    assert len(store.recent_turns("chat", "alice")) == 1


def test_missing_turn_and_invalid_event_do_not_create_orphan_events(store):
    with pytest.raises(ValueError, match="未找到"):
        store.record_turn_event("missing", "tool_result", {})
    with pytest.raises(ValueError, match="未找到"):
        store.finish_turn("missing", "completed")
    turn = store.begin_turn("chat", "alice", "message", "question")
    with pytest.raises(TypeError):
        store.record_turn_event(turn, "tool_result", ["not a dictionary"])
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_events").fetchone()[0] == 0
