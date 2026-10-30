"""Faults found by independent integration review, no external services."""

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.feishu_app import FeishuApp
from src.interactive.feishu_handler import handle_feishu_message
from src.interactive.ingress import DurableIngress
from tests.interactive.test_shared_conversation import event
from tests.interactive.test_shared_conversation import service as shared_service

service = shared_service


def test_failed_initialization_preserves_published_handler(service, monkeypatch):
    assistant, runtime = service
    previous = runtime._jobs["conversation.turn"].handler
    monkeypatch.setattr(
        "src.interactive.assistant.personal_tools.PersonalTools",
        Mock(side_effect=RuntimeError("resource unavailable")),
    )
    with pytest.raises(RuntimeError, match="resource unavailable"):
        FeishuAssistant(
            assistant._application_config,
            Mock(),
            project_root=assistant.root,
            store=assistant.store,
            execution_runtime=runtime,
            start_workers=False,
            configuration=Mock(),
            research=Mock(),
        )
    assert runtime._jobs["conversation.turn"].handler is previous


def test_retention_scrubs_terminal_bodies_and_keeps_pending(service):
    assistant, runtime = service
    assistant.submit("chat", "owner", "expired question", "old")
    assistant.submit("chat", "owner", "still pending", "pending")
    with assistant.store.connection() as db:
        db.execute(
            "UPDATE conversation_turns SET updated=1,status='completed' WHERE message_id='old'"
        )
        db.execute("UPDATE conversation_turns SET updated=1 WHERE message_id='pending'")
        db.execute(
            "UPDATE execution_runs SET status='succeeded',finished=1 WHERE request_id='inbound:old'"
        )
        turn = db.execute(
            "SELECT id FROM conversation_turns WHERE message_id='old'"
        ).fetchone()[0]
    assistant.store.prepare_reply(turn, "0", "chat", "expired reply")
    assert assistant.store.prune_conversations(1) == 1
    rows = {row["request_id"]: row for row in runtime.store.list_runs()}
    assert rows["inbound:old"]["payload"] == {"redacted": True}
    assert rows["inbound:pending"]["payload"]["text"] == "still pending"
    assert not assistant.submit("chat", "owner", "expired question", "old")
    with assistant.store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM conversation_deliveries").fetchone()[0]
            == 0
        )


def test_stop_claiming_before_assistant_teardown_preserves_queued(service):
    assistant, runtime = service
    entered, release = threading.Barrier(3), threading.Event()

    def handle(chat, owner, text):
        entered.wait(10)
        release.wait(10)

    assistant._handle = handle
    for index in range(3):
        assistant.submit(str(index), "owner", "hello", str(index))
    runtime.start()
    try:
        entered.wait(10)
        runtime.stop(drain=False, timeout=0)
        assistant.stop()
    finally:
        release.set()
    assert sum(row["status"] == "queued" for row in runtime.store.list_runs()) == 1


def test_admission_retry_charges_rate_capacity_once(tmp_path, monkeypatch):
    app = FeishuApp(
        {
            "interactive": {
                "feishu": {
                    "enabled": True,
                    "app_id": "test",
                    "app_secret": "test-secret",
                    "allowed_chat_ids": ["allowed"],
                    "rate_limit_per_minute": 1,
                    "assistant": {"enabled": True},
                }
            }
        }
    )
    assistant = Mock()
    assistant.submit.side_effect = [RuntimeError("db unavailable")] * 3 + [True]
    monkeypatch.setattr(app, "get_assistant", lambda: assistant)
    body = event()
    body["event"]["sender"] = {"sender_type": "user", "sender_id": {"open_id": "owner"}}
    body["event"]["message"]["chat_type"] = "p2p"
    inbox = DurableIngress(tmp_path / "inbox.sqlite3", "test", ["allowed"])
    inbox.accept(body)
    for _ in range(3):
        inbox.dispatch(app, handle_feishu_message)
        assert len(inbox.pending()) == 1
    inbox.dispatch(app, handle_feishu_message)
    assert inbox.pending() == []
    assert assistant.submit.call_count == 4


def test_parallel_turns_keep_account_writes_and_tool_traces_with_the_owner(service):
    assistant, runtime = service
    entered = threading.Barrier(2)
    facts = {"a": ("601088", 100), "b": ("600900", 200)}

    def handle(chat, sender, text):
        code, quantity = facts[sender]
        assistant.call_tool(
            chat, sender, "personal_monitor_account", {"account": "same"}
        )
        entered.wait(10)
        assistant.call_tool(
            chat,
            sender,
            "personal_monitor_import_manual",
            {
                "account": "same",
                "as_of": datetime.now(timezone.utc).isoformat(),
                "expected_revision": 0,
                "rows": [{"instrument": code, "quantity": quantity}],
            },
        )
        assistant._reply(chat, "已记录 " + code)

    assistant._handle = handle
    for owner, (code, quantity) in facts.items():
        assert assistant.submit(
            owner, owner, f"新建账户same，记录{code}持有{quantity}股", "write-" + owner
        )
    runtime.start()
    for run in runtime.store.list_runs():
        assert runtime.wait(run["id"], 15)["status"] == "succeeded"
    for owner, (code, quantity) in facts.items():
        account = assistant.personal_store.status(owner, "same")["accounts"][0]
        assert account["revision"] == 1
        assert [(p["instrument"], p["quantity"]) for p in account["positions"]] == [
            (code, quantity)
        ]
        turn = assistant.store.recent_turns(owner, owner)[0]
        results = [e["data"] for e in turn["events"] if e["kind"] == "tool_result"]
        assert len(results) == 2
        assert results[1]["result"]["result"]["applied"] == [code]
        assert code in turn["answer"]
        assert assistant.store.recent_turns(owner, "b" if owner == "a" else "a") == []


def test_process_exit_after_business_commit_keeps_one_reminder_without_replay(tmp_path):
    from src.core.execution import ExecutionStore

    child = r"""
import os, sys
from pathlib import Path
from unittest.mock import Mock
from src.interactive.assistant.service import FeishuAssistant
directory=Path(sys.argv[1])
assistant=FeishuAssistant(
    {"storage":{"data_dir":str(directory)},"interactive":{"feishu":{"assistant":{"enabled":True}}}},
    Mock(), project_root=directory, client=Mock(), configuration=Mock(), research=Mock(),
    capabilities={"personal"}, start_workers=False,
)
def commit_then_exit(chat, owner, text):
    assistant.call_tool(chat,owner,"personal_monitor_create_reminder",
                        {"after_days":28,"text":"到期决定是否滚存"})
    os._exit(23)
assistant._handle=commit_then_exit
assistant.submit("chat","owner","28天后提醒我决定是否滚存","commit-then-exit")
assistant.execution_runtime.start()
assistant.execution_runtime.wait(assistant.execution_runtime.store.list_runs()[0]["id"],15)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", child, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    store = ExecutionStore(tmp_path / "runtime" / "feishu_assistant.sqlite3")
    assert store.recover_offline() == 1
    run = store.list_runs()[0]
    assert run["status"] == "unknown"
    config = {
        "storage": {"data_dir": str(tmp_path)},
        "interactive": {"feishu": {"assistant": {"enabled": True}}},
    }
    restarted = FeishuAssistant(
        config,
        Mock(),
        project_root=tmp_path,
        client=Mock(),
        configuration=Mock(),
        research=Mock(),
        capabilities={"personal"},
        start_workers=False,
    )
    try:
        assert not restarted.submit(
            "chat", "owner", "28天后提醒我决定是否滚存", "commit-then-exit"
        )
        assert store.count_pending("conversation.turn") == 0
        reminders = restarted.personal_store.status("owner")["reminders"]
        assert len(reminders) == 1
        assert reminders[0]["state"] == "pending"
        turn = restarted.store.recent_turns("chat", "owner")[0]
        assert turn["status"] == "interrupted"
        assert turn["error"] == "execution_result_unknown"
        assert any(
            json.loads(e["data"]) if isinstance(e["data"], str) else e["data"]
            for e in turn["events"]
            if e["kind"] == "tool_request"
        )
    finally:
        restarted.stop()
