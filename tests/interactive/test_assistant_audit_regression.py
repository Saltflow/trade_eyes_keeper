"""Regression checks for structured redaction and persistent turn diagnostics."""

import json
import threading
from unittest.mock import Mock

import pytest

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore


@pytest.fixture
def service(tmp_path):
    value = FeishuAssistant(
        {},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=Mock(),
        research=Mock(),
        client=Mock(),
        store=ProposalStore(tmp_path / "assistant.sqlite3"),
        start_workers=False,
    )
    yield value
    value.stop()


def test_research_result_redaction_does_not_corrupt_serialized_json(service, tmp_path):
    item = service.store.create("research", "chat", "owner", {"job_dir": str(tmp_path)})
    service.store.activate(item["id"])
    item = service.store.claim(item["id"], "chat", "owner")
    service.research.run.return_value = {
        "status": "completed",
        "summary": "Generated script correctly rejected password=unknown-secret",
        "artifacts": [],
        "exit_code": 0,
    }
    service._jobs.put((item, threading.Event()))
    worker = threading.Thread(target=service._research_worker, daemon=True)
    worker.start()
    service._jobs.join()
    service._stop.set()
    worker.join(2)
    saved = service.store.get(item["id"], "chat", "owner")
    assert saved["status"] == "succeeded"
    assert "unknown-secret" not in json.dumps(saved["result"])
    assert saved["result"]["artifacts"] == []
    assert saved["result"]["exit_code"] == 0


def test_document_json_remains_parseable_when_it_describes_credential_settings(service):
    (service.root / "README.md").write_text(
        "# Quickstart\nSet API_KEY=example-secret\nThe solver is configured separately.\n",
        encoding="utf-8",
    )
    service.client.complete.return_value = {"role": "assistant", "content": "answer"}
    service.handle("chat", "owner", "solver configured")
    messages = service.client.complete.call_args.args[0]
    context = next(
        message["content"] for message in messages if message["role"] == "tool"
    )
    result = json.loads(context)
    assert result["results"][0]["path"] == "README.md"
    assert "solver is configured separately" in result["results"][0]["text"]
    assert "example-secret" not in context


def test_empty_exception_message_still_records_failed_turn(service):
    service.client.complete.side_effect = RuntimeError()
    service.handle("chat", "owner", "question")
    turn = service.store.recent_turns("chat", "owner")[0]
    assert turn["status"] == "failed"
    assert any(event["kind"] == "error" for event in turn["events"])


def test_failed_delivery_is_not_reintroduced_as_successful_in_memory_context(service):
    service.client.complete.return_value = {
        "role": "assistant",
        "content": "unseen answer",
    }
    service.send_message.return_value = (False, "delivery rejected")
    service.handle("chat", "owner", "old question")
    service.send_message.return_value = (True, "ok")
    service.client.complete.return_value = {
        "role": "assistant",
        "content": "new answer",
    }
    service.handle("chat", "owner", "new question")
    messages = service.client.complete.call_args.args[0]
    assert all(message.get("content") != "unseen answer" for message in messages)
