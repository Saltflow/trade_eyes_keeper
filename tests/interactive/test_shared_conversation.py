"""Durable real assistant admission and turn ownership, without external I/O."""

import json
import threading
from unittest.mock import Mock

import pytest

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.interactive.assistant.client import DeepSeekAssistantClient
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.interactive.ingress import DurableIngress


@pytest.fixture
def service(tmp_path):
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "interactive": {"feishu": {"app_id": "test", "assistant": {"enabled": True}}},
        "llm": {"api_key": "unit-test-key"},
    }
    runtime = ExecutionRuntime(ExecutionStore(tmp_path / "assistant.sqlite3"))
    assistant = FeishuAssistant(
        config,
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        store=ProposalStore(runtime.store.path),
        execution_runtime=runtime,
        start_workers=False,
        capabilities={"personal"},
        configuration=Mock(),
        research=Mock(),
    )
    yield assistant, runtime
    assistant.stop()
    runtime.stop(timeout=3)


def test_admission_failure_rolls_back_receipt_turn_and_job(service):
    assistant, runtime = service
    with assistant.store.connection() as db:
        db.execute("""CREATE TRIGGER fail_admission BEFORE INSERT ON execution_runs
                   BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END""")
    with pytest.raises(Exception, match="injected commit failure"):
        assistant.submit("chat", "owner", "hello", "message")
    with assistant.store.connection() as db:
        for table in ("receipts", "conversation_turns", "execution_runs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_admission")
    assert assistant.submit("chat", "owner", "hello", "message")
    assert not assistant.submit("chat", "owner", "hello", "message")
    assert len(runtime.store.list_runs()) == 1


def test_queue_capacity_is_atomic_and_does_not_consume_rejected_identity(service):
    assistant, runtime = service
    for index in range(32):
        assert assistant.submit("chat", "owner", "hello", str(index))
    assert not assistant.submit("chat", "owner", "hello", "full")
    with assistant.store.connection() as db:
        assert db.execute("SELECT 1 FROM receipts WHERE id='full'").fetchone() is None
    assert runtime.store.count_pending("conversation.turn") == 32
    first = runtime.store.list_runs(limit=40)[-1]
    runtime.store.cancel_queued(first["id"])
    assert assistant.submit("chat", "owner", "hello", "full")


def test_two_real_clients_harnesses_contexts_and_ocr_diagnostics_are_isolated(service):
    assistant, runtime = service
    entered = {owner: threading.Event() for owner in ("a", "b")}
    release_a = threading.Event()
    observations = {}

    def handle(chat, owner, text):
        context = assistant._turn_context.current
        assert isinstance(assistant.client, DeepSeekAssistantClient)
        assistant.client.last_image_diagnostics.append(owner)
        observations[owner] = {
            "client": assistant.client,
            "harness": assistant.harness,
            "context": context,
            "cancel": assistant.client.stopped,
        }
        entered[owner].set()
        assert entered["a"].wait(3) and entered["b"].wait(3)
        if owner == "a":
            assert release_a.wait(5)
        assert assistant.last_image_diagnostics == [owner]
        assert assistant._turn_context.current["original_text"] == text
        assistant._reply(chat, "answer " + owner)

    assistant._handle = handle
    assert assistant.submit("ca", "a", "question a", "a1")
    assert assistant.submit("cb", "b", "question b", "b1")
    runtime.start()
    try:
        assert entered["a"].wait(3) and entered["b"].wait(3)
        rows = runtime.store.list_runs()
        b = next(row for row in rows if row["payload"]["sender"] == "b")
        assert runtime.wait(b["id"], 5)["status"] == "succeeded"
        assert not observations["a"]["cancel"].is_set()
        assert observations["a"]["client"] is not observations["b"]["client"]
        assert observations["a"]["harness"] is not observations["b"]["harness"]
        assert observations["a"]["context"] is not observations["b"]["context"]
    finally:
        release_a.set()
    for row in runtime.store.list_runs():
        assert runtime.wait(row["id"], 5)["status"] == "succeeded"
    assert "answer a" in assistant.store.recent_turns("ca", "a")[0]["answer"]
    assert assistant.store.recent_turns("ca", "b") == []


def test_same_session_is_fifo_and_queued_turn_survives_new_runtime(service):
    assistant, original = service
    seen = []
    assistant._handle = lambda chat, sender, text: seen.append(text)
    for index in range(3):
        assert assistant.submit("same", "owner", str(index), f"m{index}")
    runtime = ExecutionRuntime(ExecutionStore(original.store.path))
    from src.core.execution import JobSpec, ModuleSpec

    runtime.register(
        ModuleSpec(
            "conversation",
            (
                JobSpec(
                    "conversation.turn", assistant._execute_turn, lane="conversation"
                ),
            ),
        )
    )
    runtime.start()
    try:
        for row in runtime.store.list_runs():
            assert runtime.wait(row["id"], 8)["status"] == "succeeded"
        assert seen == ["0", "1", "2"]
    finally:
        runtime.stop()


def test_reply_intent_unknown_is_not_sent_twice(service):
    assistant, runtime = service
    assistant.send_message.side_effect = TimeoutError("after submission")
    assistant._handle = lambda chat, sender, text: assistant._reply(chat, "once")
    assert assistant.submit("chat", "owner", "hello", "message")
    runtime.start()
    row = runtime.store.list_runs()[0]
    assert runtime.wait(row["id"], 15)["status"] == "unknown"
    assert not assistant.submit("chat", "owner", "hello", "message")
    assert assistant.send_message.call_count == 1
    with assistant.store.connection() as db:
        receipt = db.execute("SELECT * FROM conversation_deliveries").fetchone()
        assert receipt["state"] == "unknown"
        assert not assistant.store.prepare_reply(
            receipt["turn_id"], receipt["piece"], receipt["route"], receipt["body"]
        )[1]


def test_restricted_profile_cannot_call_unadvertised_tool(service):
    assistant, _runtime = service
    with pytest.raises(ValueError, match="未开放"):
        assistant.call_tool("chat", "owner", "plan_research", {})
    assistant.research.plan.assert_not_called()


def event(message_id="message", text="hello"):
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "allowed",
                "message_id": message_id,
                "content": json.dumps({"text": text}),
            }
        },
    }


def test_inbox_survives_lost_wake_and_dispatch_admission_failure(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    first = DurableIngress(path, "test", ["allowed"])
    identity = first.accept(event())
    assert first.accept(event()) == identity
    restarted = DurableIngress(path, "test", ["allowed"])
    assert len(restarted.pending()) == 1
    handler = Mock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError):
        restarted.dispatch(Mock(), handler)
    assert len(restarted.pending()) == 1
    restarted.dispatch(Mock(), Mock(return_value="accepted"))
    assert restarted.pending() == []


def test_legacy_unknown_dispatch_is_never_replayed_and_other_chat_is_filtered(tmp_path):
    inbox = DurableIngress(tmp_path / "inbox.sqlite3", "test", ["allowed"])
    denied = event()
    denied["event"]["message"]["chat_id"] = "forbidden"
    assert inbox.accept(denied) is None
    inbox.accept(event(text="/config set x 1"))
    handler = Mock(side_effect=TimeoutError("side effect may exist"))
    with pytest.raises(TimeoutError):
        inbox.dispatch(Mock(), handler)
    inbox.dispatch(Mock(), handler)
    assert handler.call_count == 1
    with inbox.connection() as db:
        assert (
            db.execute("SELECT state FROM execution_ingress").fetchone()[0] == "unknown"
        )
