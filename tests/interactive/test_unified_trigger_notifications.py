"""One notification entry: authenticated conversation -> durable report job.

The model decision is scripted; these tests prove the host contract, not live
model accuracy. No report is generated and every external transport is blocked.
"""

import smtplib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import yaml

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.core.report_execution import runtime_path
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.triggers.runtime import TriggerRuntime

CREATED = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 9, 1, 50, tzinfo=timezone.utc)
RETIRED_ACTIONS = (
    "create_reminder",
    "edit_reminder",
    "cancel_reminder",
    "rule",
    "exception",
    "reset",
    "resend",
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("external transport forbidden"))
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setenv("SKIP_NOTIFICATIONS", "true")
    config = {
        "storage": {"data_dir": str(tmp_path)},
        # Synthetic configured universe; no financial report/HTML is produced.
        "stocks": ["01339", "601088", "VOO", "D05.SI"],
        "notification": {
            "email": {"enabled": True},
            "feishu": {"enabled": True},
            "telegram": {"enabled": True},
        },
        "interactive": {"feishu": {"enabled": True, "allowed_chat_ids": ["chat"]}},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    execution = ExecutionRuntime(ExecutionStore(runtime_path(config)))
    trigger = TriggerRuntime(
        config,
        execution,
        config_path=config_path,
        clock=lambda: DUE,
        fact_loader=forbidden,
    )
    assistant = FeishuAssistant(
        config,
        Mock(return_value=(True, "synthetic conversation acknowledgement")),
        project_root=tmp_path,
        configuration=Mock(),
        research=Mock(),
        client=Mock(),
        store=ProposalStore(execution.store.path),
        execution_runtime=execution,
        trigger_runtime=trigger,
        capabilities={"personal", "triggers"},
        start_workers=False,
    )
    assistant.harness.run = Mock(return_value="仅回答，未创建。")
    yield SimpleNamespace(
        assistant=assistant,
        execution=execution,
        trigger=trigger,
        config=config,
        forbidden=forbidden,
    )
    assistant.stop()
    execution.stop(timeout=3)
    forbidden.assert_not_called()


def send(app, text, message_id):
    app.assistant.handle(
        "chat",
        "owner",
        text,
        message_id,
        message_time={"create_time": str(int(CREATED.timestamp() * 1000))},
    )


def assert_no_legacy_notifications(app):
    with app.assistant.personal_store.connection() as db:
        for table in ("reminders", "outbox", "rules"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_model_has_one_notification_tool_and_keeps_holdings_operations(app):
    names = {item["function"]["name"] for item in app.assistant.tool_schema()}
    assert "trigger_manage" in names
    assert names.isdisjoint("personal_monitor_" + name for name in RETIRED_ACTIONS)
    assert {
        "personal_monitor_status",
        "personal_monitor_account",
        "personal_monitor_import_manual",
        "personal_monitor_import_image",
        "personal_monitor_undo_positions",
        "personal_monitor_redo_positions",
    } <= names


@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_stale_or_injected_old_tool_cannot_create_second_notification_path(app, action):
    attempts = []

    def stale_model(messages, tools, invoke, *args, **kwargs):
        with pytest.raises(ValueError, match="trigger_manage"):
            invoke("personal_monitor_" + action, {})
        attempts.append(action)
        return "旧通知工具已移除。"

    app.assistant.harness.run.side_effect = stale_model
    send(app, "11月9日提醒我", "retired-" + action)
    assert attempts == [action]
    assert app.assistant.trigger_store.list("owner") == []
    assert_no_legacy_notifications(app)


def test_two_turn_dividend_date_enters_daily_queue_once_with_whole_universe(app):
    app.assistant.harness.run.return_value = "请补充希望通知的日期。"
    send(app, "那01339下次派息到账日提醒我 一次性到时提醒", "dividend-request")
    assert app.assistant.trigger_store.list("owner") == []

    def create_report_trigger(messages, tools, invoke, *args, **kwargs):
        names = {item["function"]["name"] for item in tools}
        assert "personal_monitor_create_reminder" not in names
        result = invoke(
            "trigger_manage",
            {
                "action": "create",
                "spec": {
                    "label": "01339 派息核对",
                    "schedule": {"at": "2026-11-09T09:50:00+08:00"},
                    "match_policy": "once",
                    "actions": [
                        {
                            "kind": "daily",
                            "payload": {"highlight_symbols": ["01339"]},
                        }
                    ],
                },
            },
        )
        assert result["status"] == "created"
        return "模型文字不能代替真实回执。"

    app.assistant.harness.run.side_effect = create_report_trigger
    send(app, "11月9日提醒我", "dividend-date")
    stored = app.assistant.trigger_store.list("owner")
    assert len(stored) == 1
    assert stored[0]["spec"]["actions"][0]["kind"] == "daily"
    assert "已保存触发器" in app.assistant.send_message.call_args.args[1]
    assert "模型文字" not in app.assistant.send_message.call_args.args[1]
    assert_no_legacy_notifications(app)

    app.trigger.evaluator.evaluate(now=DUE - timedelta(seconds=1))
    app.trigger._submit_pending(app.config)
    assert app.trigger.store.actions("owner") == []
    for _ in range(2):
        app.trigger.evaluator.evaluate(now=DUE)
        app.trigger._submit_pending(app.config)
    actions = app.trigger.store.actions("owner")
    assert len(actions) == 1
    assert actions[0]["kind"] == "daily"
    assert actions[0]["state"] == "submitted"
    job = app.execution.store.get(actions[0]["run_id"])
    assert job["job_id"] == "reports.daily"
    snapshot = job["payload"]["config_snapshot"]
    assert snapshot["stocks"] == app.config["stocks"]
    assert snapshot["notification"] == app.config["notification"]
    assert snapshot["_trigger_context"]["owner"] == "owner"
    assert snapshot["_trigger_context"]["chat_id"] == "chat"
    assert_no_legacy_notifications(app)


def test_retiring_notifications_keeps_authenticated_holdings_write(app):
    operations = []

    def create_account(messages, tools, invoke, *args, **kwargs):
        operations.append(invoke("personal_monitor_account", {"account": "验收账户"}))
        return "已处理。"

    app.assistant.harness.run.side_effect = create_account
    send(app, "新建账户“验收账户”", "account-create")
    assert operations[0]["result"]["account"] == "验收账户"

    def import_holdings(messages, tools, invoke, *args, **kwargs):
        operations.append(
            invoke(
                "personal_monitor_import_manual",
                {
                    "account": "验收账户",
                    "rows": [{"instrument": "01339", "quantity": 100}],
                    "expected_revision": 0,
                },
            )
        )
        return "已处理。"

    app.assistant.harness.run.side_effect = import_holdings
    send(app, "验收账户现在持有01339 100股", "holdings-import")
    assert len(operations) == 2
    account = app.assistant.personal_store.status("owner")["accounts"][0]
    assert account["account"] == "验收账户"
    assert len(account["positions"]) == 1
    assert account["positions"][0]["quantity"] == 100
    assert_no_legacy_notifications(app)


@pytest.mark.parametrize("text,blocked", [
    ("28天后提醒我决定是否滚存", False),
    ("11月9日提醒我核对是否到账", False),
    ("是否要在11月9日提醒我", True),
    ("我想知道是否可以提醒我", True),
    ("提醒我这个功能是否支持11月9日", True),
    ("11月9日提醒我，这样是否可以", True),
    ("解释11月9日提醒我是否到账", True),
    ("不要创建，11月9日提醒我是否到账", True),
    ("11月9日提醒我是否到账？", True),
])
def test_reminder_subject_does_not_override_request_authority(text, blocked):
    assert FeishuAssistant._trigger_meta_text(text) is blocked


@pytest.mark.parametrize("text", [
    "提醒我这个功能是否支持11月9日",
    "11月9日提醒我，这样是否可以",
])
def test_reminder_capability_questions_cannot_create_trigger(app, text):
    def attempt(messages, tools, invoke, *args, **kwargs):
        with pytest.raises(ValueError, match="不能授权"):
            invoke("trigger_manage", {
                "action": "create",
                "spec": {"schedule": {"at": DUE.isoformat()},
                         "actions": [{"kind": "daily"}]},
            })
        return "仅说明功能，没有创建。"

    app.assistant.harness.run.side_effect = attempt
    send(app, text, "capability-question")
    assert app.assistant.trigger_store.list("owner") == []
    assert_no_legacy_notifications(app)
