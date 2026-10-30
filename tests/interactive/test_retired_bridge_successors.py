"""Shared-path successors for retired bridge behavior, with offline boundaries.

These tests use real admission, execution, conversation and holding stores.
Provider responses and the platform sender are controlled fixtures; no live
Feishu, OCR-accuracy or native-Task acceptance is claimed.
"""

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.interactive.assistant.client import AssistantAPIError
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.personal_monitor import MonitorStore

OWNER = "successor-owner"
CHAT = "successor-chat"
SECRET = "private-successor-provider-key"
BEARER = "private-successor-bearer"


def delivery_rows(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM conversation_deliveries ORDER BY CAST(piece AS INTEGER)"
            )
        ]
    finally:
        db.close()


def close_world(world):
    if not world.closed:
        world.runtime.stop(drain=True, timeout=5)
        world.assistant.stop()
        world.closed = True


@pytest.fixture
def world_factory(tmp_path):
    worlds = []

    def create():
        database = tmp_path / "shared.sqlite3"
        runtime = ExecutionRuntime(ExecutionStore(database))
        client = Mock()
        sender = Mock(return_value=(True, "controlled platform acceptance"))
        loader = Mock(return_value={"image": b"controlled-image-fixture"})
        assistant = FeishuAssistant(
            {
                "storage": {"data_dir": str(tmp_path)},
                "interactive": {
                    "feishu": {
                        "app_id": "successor-test-app",
                        "assistant": {"enabled": True},
                    }
                },
                "llm": {"api_key": SECRET},
            },
            sender,
            project_root=tmp_path,
            store=ProposalStore(database),
            personal_store=MonitorStore(database),
            execution_runtime=runtime,
            client=client,
            image_loader=loader,
            personal_quote_loader=Mock(
                side_effect=AssertionError("unexpected quote request")
            ),
            start_workers=False,
            capabilities={"personal"},
            configuration=Mock(),
            research=Mock(),
        )
        world = SimpleNamespace(
            assistant=assistant,
            runtime=runtime,
            client=client,
            sender=sender,
            loader=loader,
            database=database,
            closed=False,
        )
        worlds.append(world)
        return world

    yield create
    for world in reversed(worlds):
        close_world(world)


def execute_turn(world, message_id, *, attachments=None):
    assert world.assistant.submit(
        CHAT,
        OWNER,
        "请回答这条消息",
        message_id,
        attachments=attachments,
        message_time={
            "create_time": str(int(datetime.now(timezone.utc).timestamp() * 1000))
        },
    )
    runs = world.runtime.store.list_runs(job_id="conversation.turn")
    assert len(runs) == 1
    world.runtime.start()
    result = world.runtime.wait(runs[0]["id"], 10)
    assert result is not None and result["released_at"] is not None
    turns = world.assistant.store.recent_turns(CHAT, OWNER)
    assert len(turns) == 1
    turn = turns[0]
    assert (turn["chat"], turn["sender"], turn["message_id"]) == (
        CHAT,
        OWNER,
        message_id,
    )
    return result, turn


def test_shared_long_reply_is_complete_and_each_piece_is_durable(world_factory):
    world = world_factory()
    answer = "完整回复" * 2500
    world.client.complete.return_value = {"role": "assistant", "content": answer}
    submitted = []

    def send(chat, body):
        # A separate connection must see the committed intent before transport.
        rows = delivery_rows(world.database)
        assert len(rows) == len(submitted) + 1
        assert rows[-1]["state"] == "submitted"
        assert (rows[-1]["route"], rows[-1]["body"]) == (CHAT, body)
        assert all(row["state"] == "sent" for row in rows[:-1])
        submitted.append(body)
        return True, "controlled platform acceptance"

    world.sender.side_effect = send
    result, turn = execute_turn(world, "complete-long-reply")
    assert result["status"] == "succeeded" and turn["status"] == "completed"
    assert len(submitted) == 4 and max(map(len, submitted)) <= 2800
    assert "".join(submitted) == answer
    rows = delivery_rows(world.database)
    assert [row["piece"] for row in rows] == ["0", "1", "2", "3"]
    assert [row["body"] for row in rows] == submitted
    assert all(row["state"] == "sent" and row["turn_id"] == turn["id"] for row in rows)
    replies = [e["data"]["text"] for e in turn["events"] if e["kind"] == "reply"]
    deliveries = [e for e in turn["events"] if e["kind"] == "delivery"]
    assert replies == submitted and len(deliveries) == 4
    assert all(event["data"]["ok"] for event in deliveries)

    close_world(world)
    restarted = world_factory()
    assert delivery_rows(restarted.database) == rows
    assert restarted.assistant.store.recent_turns(CHAT, OWNER) == [turn]
    assert not restarted.assistant.submit(
        CHAT, OWNER, "请回答这条消息", "complete-long-reply"
    )
    restarted.sender.assert_not_called()
    restarted.client.complete.assert_not_called()


@pytest.mark.parametrize("stage", ["download", "vision", "validation_or_persistence"])
def test_shared_image_failure_keeps_durable_redacted_stage_after_restart(
    world_factory, monkeypatch, stage
):
    world = world_factory()
    moment = datetime.now(timezone.utc).isoformat()
    store = world.assistant.personal_store
    store.ensure_account(OWNER, "unchanged-holdings")
    store.import_snapshot(
        OWNER,
        "unchanged-holdings",
        [{"instrument": "601088", "quantity": 300}],
        moment,
        expected_revision=0,
        source="manual",
    )
    original_accounts = store.status(OWNER)["accounts"]
    private_error = f"api_key={SECRET} Bearer {BEARER}"
    if stage == "download":
        world.loader.side_effect = ValueError(private_error)
        expected_error = "ValueError"
    elif stage == "vision":

        def fail_vision(*args, **kwargs):
            try:
                json.loads("invalid JSON")
            except json.JSONDecodeError as cause:
                raise AssistantAPIError(private_error) from cause

        world.client.read_position_images.side_effect = fail_vision
        expected_error = "AssistantAPIError"
    else:
        world.client.read_position_images.return_value = {
            "as_of": moment,
            "positions": [
                {
                    "code": "601088",
                    "quantity": 1000,
                    "price": 40,
                    "market_value": 40000,
                    "currency": "CNY",
                }
            ],
        }

        def fail_validation(*args, **kwargs):
            raise ValueError(private_error)

        monkeypatch.setattr(
            "src.interactive.assistant.position_images.validate_position_document",
            fail_validation,
        )
        expected_error = "ValueError"

    message_id = "image-failure-" + stage
    result, turn = execute_turn(
        world,
        message_id,
        attachments=[{"message_id": message_id, "image_key": "controlled-image"}],
    )
    assert result["status"] == "failed" and turn["status"] == "failed"
    assert store.status(OWNER)["accounts"] == original_accounts
    world.client.complete.assert_not_called()
    world.sender.assert_called()
    rows = delivery_rows(world.database)
    assert rows and all(row["state"] == "sent" for row in rows)
    serialized = json.dumps({"turn": turn, "deliveries": rows}, ensure_ascii=False)
    assert SECRET not in serialized and BEARER not in serialized
    diagnostics = [event for event in turn["events"] if "stage" in event["data"]]
    assert len(diagnostics) == 1, "missing one durable image-stage diagnostic"
    diagnostic = diagnostics[0]
    assert diagnostic["kind"] == "image_prepare_error"
    assert diagnostic["data"]["stage"] == stage
    chain = diagnostic["data"]["chain"]
    assert isinstance(chain, list) and 1 <= len(chain) <= 8
    assert chain[0] == expected_error
    assert all(isinstance(name, str) for name in chain)
    if stage == "vision":
        assert chain[:2] == ["AssistantAPIError", "JSONDecodeError"]
    assert "持仓未更新" in "".join(row["body"] for row in rows)

    close_world(world)
    restarted = world_factory()
    assert restarted.assistant.store.recent_turns(CHAT, OWNER) == [turn]
    assert delivery_rows(restarted.database) == rows
    assert (
        restarted.assistant.personal_store.status(OWNER)["accounts"]
        == original_accounts
    )
    assert restarted.assistant.store.recent_turns("foreign-chat", OWNER) == []
    assert restarted.assistant.store.recent_turns(CHAT, "foreign-owner") == []
    assert not restarted.assistant.submit(CHAT, OWNER, "请回答这条消息", message_id)
    restarted.loader.assert_not_called()
    restarted.sender.assert_not_called()
    restarted.client.read_position_images.assert_not_called()
    restarted.client.complete.assert_not_called()
