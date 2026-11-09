"""Confirmed reports enqueue the current verified config, across Bot profiles."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
import yaml

from src.core.config_store import ConfigStore
from src.core.execution import ExecutionRuntime, ExecutionStore
from src.interactive.assistant.report_tools import ReportTools
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore


@pytest.fixture(params=[None, {"research"}], ids=["default", "research-profile"])
def bot(tmp_path, monkeypatch, request):
    blocked = Mock(side_effect=AssertionError("external side effects forbidden"))
    monkeypatch.setattr("requests.sessions.Session.request", blocked)
    monkeypatch.setattr("smtplib.SMTP", blocked)
    monkeypatch.setattr("smtplib.SMTP_SSL", blocked)
    config = {
        "stocks": ["OLD_FIXTURE"],
        "storage": {"data_dir": str(tmp_path / "data")},
        "notification": {"feishu": {"enabled": True}},
    }
    path = tmp_path / "config/config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    runtime = ExecutionRuntime(ExecutionStore(tmp_path / "runtime.sqlite3"))
    monkeypatch.setattr(runtime, "start_if_unowned", Mock(return_value=False))
    assistant = FeishuAssistant(
        config,
        Mock(return_value=(True, "offline fixture")),
        project_root=tmp_path,
        store=ProposalStore(runtime.store.path),
        execution_runtime=runtime,
        capabilities=request.param,
        configuration=Mock(),
        research=Mock(),
        client=Mock(),
        start_workers=False,
    )
    yield assistant, ConfigStore(path)
    assistant.stop()
    runtime.stop(drain=False)
    blocked.assert_not_called()


def propose(assistant):
    return assistant.call_tool("chat", "owner", "report_request", {"kind": "daily"})[
        "proposal_ids"
    ][0]


def confirm(assistant, action_id):
    assistant._control("chat", "owner", "确认", action_id)
    return assistant.store.get(action_id, "chat", "owner")


def test_report_uses_current_config_after_configuration_update(bot):
    assistant, store = bot
    startup = deepcopy(assistant._fundamental_config)
    store.update(
        lambda value: value.update(
            stocks=["NEW_FIXTURE"],
            notification={"feishu": {"enabled": True}, "telegram": {"enabled": True}},
        )
    )
    action = confirm(assistant, propose(assistant))
    assert action["status"] == "succeeded"
    run = assistant.execution_runtime.store.get(action["result"]["run_id"])
    assert run["status"] == "queued"
    snapshot = run["payload"]["config_snapshot"]
    assert snapshot["stocks"] == ["NEW_FIXTURE"]
    assert snapshot["notification"] == store.load_raw()["notification"]
    assert assistant._fundamental_config == startup
    if assistant.capabilities is not None:
        # Explicit personal/profile views retain their existing isolation policy.
        assert assistant._runtime_config() == startup


def test_config_change_after_preview_prevents_enqueue(bot):
    assistant, store = bot
    action_id = propose(assistant)
    store.update(lambda value: value.update(stocks=["AFTER_PREVIEW"]))
    action = confirm(assistant, action_id)
    assert action["status"] == "failed"
    assert "配置已变化" in action["note"]
    assert assistant.execution_runtime.store.list_runs(job_id="reports.daily") == []


def test_external_edit_during_config_read_prevents_enqueue(bot, monkeypatch):
    assistant, store = bot
    action_id = propose(assistant)
    original_load = ConfigStore.load_runtime

    def changing_read(config_store):
        snapshot = original_load(config_store)
        if config_store.path == store.path:
            changed = {**snapshot, "stocks": ["CHANGED_DURING_READ"]}
            config_store.path.write_text(yaml.safe_dump(changed), encoding="utf-8")
        return snapshot

    monkeypatch.setattr(ConfigStore, "load_runtime", changing_read)
    action = confirm(assistant, action_id)
    assert action["status"] == "failed"
    assert "配置已变化" in action["note"]
    assert assistant.execution_runtime.store.list_runs(job_id="reports.daily") == []


def test_config_lock_is_held_through_real_enqueue(bot, monkeypatch):
    assistant, store = bot
    action_id = propose(assistant)
    original_submit = ReportTools.submit
    attempted = []

    def submit_while_writer_contends(tools, **kwargs):
        with pytest.raises(TimeoutError, match="configuration is busy"):
            store.update(
                lambda value: value.update(stocks=["RACING_WRITE"]), lock_timeout=0
            )
        attempted.append(True)
        return original_submit(tools, **kwargs)

    monkeypatch.setattr(ReportTools, "submit", submit_while_writer_contends)
    action = confirm(assistant, action_id)
    assert action["status"] == "succeeded"
    assert attempted == [True]
    run = assistant.execution_runtime.store.get(action["result"]["run_id"])
    assert run["payload"]["config_snapshot"]["stocks"] == ["OLD_FIXTURE"]
    assert store.load_raw()["stocks"] == ["OLD_FIXTURE"]
