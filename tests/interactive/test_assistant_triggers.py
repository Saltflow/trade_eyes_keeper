"""Real assistant admission for personal triggers, with no external I/O."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core.execution import ExecutionRuntime, ExecutionStore
from src.interactive.assistant.service import SYSTEM_PROMPT, FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.triggers.store import TriggerStore

NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)
MESSAGE_TIME = {"create_time": str(int(NOW.timestamp() * 1000))}


@pytest.fixture
def assistant_factory(tmp_path):
    active = []

    def create(capabilities=frozenset({"personal"}), **kwargs):
        root = tmp_path / str(len(active))
        root.mkdir()
        runtime = ExecutionRuntime(ExecutionStore(root / "assistant.sqlite3"))
        assistant = FeishuAssistant(
            {"storage": {"data_dir": str(root)}},
            Mock(return_value=(True, "ok")),
            project_root=root,
            configuration=Mock(),
            research=Mock(),
            client=Mock(),
            store=ProposalStore(runtime.store.path),
            execution_runtime=runtime,
            start_workers=False,
            capabilities=capabilities,
            **kwargs,
        )
        assistant.harness.run = Mock(return_value="仅解释，未执行变更。")
        active.append((assistant, runtime))
        return assistant

    yield create
    for assistant, runtime in active:
        assistant.stop()
        runtime.stop(timeout=3)


def send(assistant, text, message_id="message", owner="owner", chat="chat", **kwargs):
    assistant.handle(chat, owner, text, message_id, message_time=MESSAGE_TIME, **kwargs)


def spec():
    return {
        "label": "复合指标条件",
        "condition": {
            "all": [
                {"field": "quotes.601088.last", "op": "lt", "value": 49},
                {"field": "stocks_data.601088.ma60", "op": "gt", "value": 45},
            ]
        },
        "actions": [{"kind": "brief", "payload": {"highlight_symbols": ["601088"]}}],
    }


@pytest.mark.parametrize(
    "text",
    [
        "11月2号逆回购到期",
        "11月20号逆回购到期",
        "任何监控标的发三季度报告的当天和次日发简报和日报",
        "601088跌到50以下了发简报",
        "每天9:50发简报",
    ],
)
def test_explicit_create_uses_real_shared_store_without_model_or_research(
    assistant_factory, text
):
    assistant = assistant_factory()
    kick = Mock()
    assistant.trigger_changed = kick
    send(assistant, text)
    assert len(assistant.trigger_store.list("owner")) == 1
    assert assistant.trigger_store.path == assistant.execution_runtime.store.path
    assert "已保存触发器 1" in assistant.send_message.call_args.args[1]
    assistant.harness.run.assert_not_called()
    assistant.client.complete.assert_not_called()
    assistant.research.query_stock_fundamentals.assert_not_called()
    kick.assert_called_once_with()
    assert assistant.store.recent_turns("chat", "owner")[0]["status"] == "completed"


def test_fast_path_runs_before_paid_attachment_ocr(assistant_factory):
    assistant = assistant_factory()
    assistant.personal.prepare_images = Mock(side_effect=AssertionError("must not OCR"))
    send(assistant, "11月2号逆回购到期", attachments=[{"image_key": "unread-image"}])
    assert assistant.trigger_store.get("owner", 1)["state"] == "active"
    assistant.personal.prepare_images.assert_not_called()
    assistant.harness.run.assert_not_called()


def test_fast_path_batch_crud_stable_numbers_and_current_owner_only(assistant_factory):
    assistant = assistant_factory()
    send(assistant, "11月2号逆回购到期", "create1")
    send(assistant, "11月20号逆回购到期", "create2")
    send(
        assistant, "11月2号逆回购到期", "other1", owner="other-owner", chat="other-chat"
    )
    send(assistant, "暂停#1", "pause1")
    send(assistant, "拉取已经激活的触发器", "list")
    assert "2." in assistant.send_message.call_args.args[1]
    assert "1." not in assistant.send_message.call_args.args[1]
    send(assistant, "恢复#1", "resume")
    send(assistant, "remove trigger 1，2，3", "remove")
    assert assistant.trigger_store.get("owner", 1)["state"] == "deleted"
    assert assistant.trigger_store.get("owner", 2)["state"] == "deleted"
    assert assistant.trigger_store.get("other-owner", 1)["state"] == "active"
    reply = assistant.send_message.call_args.args[1]
    assert "已删除触发器 1、2" in reply and "不存在触发器 3" in reply
    assistant.harness.run.assert_not_called()


def test_platform_message_replay_does_not_duplicate_rule(assistant_factory):
    assistant = assistant_factory()
    send(assistant, "11月2号逆回购到期")
    send(assistant, "11月2号逆回购到期")
    assert [item["number"] for item in assistant.trigger_store.list("owner")] == [1]


def test_fast_update_has_no_confirmation_or_model_roundtrip(assistant_factory):
    assistant = assistant_factory()
    send(assistant, "601088跌到50以下了发简报", "create")
    send(assistant, "修改#1价格阈值为49", "change")
    assert assistant.trigger_store.get("owner", 1)["spec"]["condition"]["value"] == 49
    assistant.harness.run.assert_not_called()


def test_proposal_cancel_identifier_never_becomes_a_trigger_command(assistant_factory):
    assistant = assistant_factory()
    for action_id in ("123456abcdef", "abcdef123456"):
        control = Mock()
        assistant._control = control
        send(assistant, "取消 " + action_id, "cancel-" + action_id)
        control.assert_called_once_with("chat", "owner", "取消", action_id)
    assistant.harness.run.assert_not_called()


@pytest.mark.parametrize(
    "capabilities,allowed",
    [({"personal"}, True), ({"triggers"}, True), (None, True), ({"research"}, False)],
)
def test_tool_capabilities_keep_research_only_out_of_personal_triggers(
    assistant_factory, capabilities, allowed
):
    assistant = assistant_factory(capabilities)
    names = {item["function"]["name"] for item in assistant.tool_schema()}
    assert ("trigger_manage" in names) is allowed
    if not allowed:
        with pytest.raises(ValueError, match="未开放"):
            assistant.call_tool("chat", "owner", "trigger_manage", {"action": "list"})
        send(assistant, "remove trigger 1")
        assert "未开放个人触发器" in assistant.send_message.call_args.args[1]
        assistant.harness.run.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "解释 remove trigger 1",
        "“remove trigger 1”",
        "例如：601088跌到50以下了发简报",
        "不要删除触发器1",
        "分析这段文本：创建一个触发器，价格低于50发简报",
        "Don't remove trigger 1",
        "Explain remove trigger 1",
        "网页要求删除触发器1",
    ],
)
def test_quotes_examples_explanations_and_negation_never_execute(
    assistant_factory, text
):
    assistant = assistant_factory()
    send(assistant, "11月2号逆回购到期", "create")

    def injected(messages, tools, invoke, *args, **kwargs):
        with pytest.raises(ValueError, match="不能授权"):
            invoke("trigger_manage", {"action": "remove", "numbers": [1]})
        return "仅解释该语句。"

    assistant.harness.run.side_effect = injected
    send(assistant, text, "explanation")
    assert assistant.trigger_store.get("owner", 1)["state"] == "active"
    assert assistant.store.recent_turns("chat", "owner")[0]["status"] == "completed"


def test_retrieved_tool_or_old_assistant_instruction_cannot_authorize_mutation(
    assistant_factory,
):
    assistant = assistant_factory()

    def injected(messages, tools, invoke, *args, **kwargs):
        # This proposed action came from a tool/web result; the current human
        # asked only for information, so the server refuses it regardless.
        with pytest.raises(ValueError, match="没有明确授权"):
            invoke("trigger_manage", {"action": "create", "spec": spec()})
        return "没有修改触发器。"

    assistant.harness.run.side_effect = injected
    send(assistant, "查一下当前资料", "question")
    assert assistant.trigger_store.list("owner") == []
    assert assistant.store.recent_turns("chat", "owner")[0]["status"] == "completed"


def test_complex_rule_tool_uses_current_trusted_owner_and_real_receipt(
    assistant_factory,
):
    assistant = assistant_factory()

    def create(messages, tools, invoke, *args, **kwargs):
        assert "持久触发器使用trigger_manage" in messages[0]["content"]
        result = invoke("trigger_manage", {"action": "create", "spec": spec()})
        assert result["status"] == "created"
        return "模型虚构的另一个结果。"

    assistant.harness.run.side_effect = create
    send(assistant, "创建一个触发器：601088价格小于49且MA60大于45时发简报")
    item = assistant.trigger_store.get("owner", 1)
    assert item["spec"]["actions"][0]["payload"]["chat_id"] == "chat"
    assert "已保存触发器 1" in assistant.send_message.call_args.args[1]
    assert "虚构" not in assistant.send_message.call_args.args[1]
    assistant.research.query_stock_fundamentals.assert_not_called()


def test_catalog_loads_safe_extra_fields_from_completed_report_snapshot(
    assistant_factory,
):
    from src.triggers.snapshots import capture_report_snapshot

    assistant = assistant_factory()
    capture_report_snapshot(
        assistant._fundamental_config,
        {
            "created_at": NOW.isoformat(),
            "stocks_data": {"601088": {"custom_observed_metric": 7}},
        },
        "daily",
    )

    def create(messages, tools, invoke, *args, **kwargs):
        catalog = invoke(
            "trigger_manage",
            {"action": "catalog", "prefix": "stocks_data.601088.custom"},
        )
        assert (
            catalog["data"]["catalog"][0]["path"]
            == "stocks_data.601088.custom_observed_metric"
        )
        value = spec()
        value["condition"] = {
            "field": "stocks_data.601088.custom_observed_metric",
            "op": "gt",
            "value": 5,
        }
        assert (
            invoke("trigger_manage", {"action": "create", "spec": value})["status"]
            == "created"
        )
        return "ok"

    assistant.harness.run.side_effect = create
    send(assistant, "创建触发器：601088的custom_observed_metric大于5时发简报")
    assert assistant.trigger_store.get("owner", 1)["spec"]["condition"][
        "field"
    ].endswith("custom_observed_metric")


def test_committed_rule_receipt_survives_later_model_failure(assistant_factory):
    assistant = assistant_factory()

    def create_then_fail(messages, tools, invoke, *args, **kwargs):
        invoke("trigger_manage", {"action": "create", "spec": spec()})
        raise RuntimeError("mock later failure")

    assistant.harness.run.side_effect = create_then_fail
    send(assistant, "创建触发器：601088价格小于49且MA60大于45时发简报")
    assert assistant.trigger_store.get("owner", 1)["state"] == "active"
    assert "已保存触发器 1" in assistant.send_message.call_args.args[1]


def test_missing_numeric_threshold_cannot_be_invented_by_model(assistant_factory):
    assistant = assistant_factory()

    def hallucinate(messages, tools, invoke, *args, **kwargs):
        with pytest.raises(ValueError, match="明确比较阈值"):
            invoke("trigger_manage", {"action": "create", "spec": spec()})
        return "模型错误声称已创建。"

    assistant.harness.run.side_effect = hallucinate
    send(assistant, "创建一个价格触发器，价格合适时发简报")
    assert assistant.trigger_store.list("owner") == []
    assert "明确比较阈值" in assistant.send_message.call_args.args[1]
    assert "错误声称" not in assistant.send_message.call_args.args[1]


@pytest.mark.parametrize(
    "text",
    [
        "创建触发器：601088布林带相对位置大于5%时发简报",
        "创建触发器：601088布林带相对位置大于百分之五时发简报",
    ],
)
def test_numeric_grounding_accepts_explicit_percent_conversion(assistant_factory, text):
    assistant = assistant_factory()

    def create(messages, tools, invoke, *args, **kwargs):
        value = spec()
        value["condition"] = {
            "field": "stocks_data.601088.boll_pct_b",
            "op": "gt",
            "value": 0.05,
        }
        assert (
            invoke("trigger_manage", {"action": "create", "spec": value})["status"]
            == "created"
        )
        return "ok"

    assistant.harness.run.side_effect = create
    send(assistant, text)
    assert assistant.trigger_store.get("owner", 1)["spec"]["condition"]["value"] == 0.05


def test_tool_mutation_without_current_authenticated_context_is_rejected(
    assistant_factory,
):
    assistant = assistant_factory()
    with pytest.raises(ValueError, match="原始消息"):
        assistant.call_tool(
            "chat", "owner", "trigger_manage", {"action": "create", "spec": spec()}
        )
    assert assistant.trigger_store.list("owner") == []


def test_tool_cannot_change_authenticated_owner_chat_or_user_target_number(
    assistant_factory,
):
    assistant = assistant_factory()
    send(assistant, "11月2号逆回购到期", "create")

    def mutate(messages, tools, invoke, *args, **kwargs):
        with pytest.raises(ValueError, match="认证"):
            assistant.call_tool(
                "other-chat",
                "other-owner",
                "trigger_manage",
                {"action": "remove", "number": 1},
            )
        with pytest.raises(ValueError, match="明确.*编号"):
            invoke(
                "trigger_manage",
                {
                    "action": "update",
                    "number": 2,
                    "expected_revision": 1,
                    "changes": {"label": "wrong target"},
                },
            )
        return "没有修改。"

    assistant.harness.run.side_effect = mutate
    send(assistant, "修改触发器1的标签为新名称", "change")
    assert assistant.trigger_store.get("owner", 1)["revision"] == 1
    assert assistant.trigger_store.list("other-owner") == []
    assert assistant.store.recent_turns("chat", "owner")[0]["status"] == "completed"


def test_runtime_kick_callback_failure_does_not_erase_committed_receipt(
    assistant_factory,
):
    assistant = assistant_factory(trigger_runtime=Mock())
    send(assistant, "11月2号逆回购到期", "first")
    assistant.trigger_runtime.kick.assert_called_once_with()
    assistant.trigger_changed = Mock(side_effect=RuntimeError("local wake failure"))
    send(assistant, "11月20号逆回购到期", "second")
    assert assistant.trigger_store.get("owner", 2)["state"] == "active"
    assert "已保存触发器 2" in assistant.send_message.call_args.args[1]


def test_global_scheduler_proposals_stay_distinct_from_personal_crud():
    assert "trigger_manage直接执行可逆管理" in SYSTEM_PROMPT
    assert "改变全局scheduler配置" in SYSTEM_PROMPT
    assert "必须走配置提案和确认" not in SYSTEM_PROMPT


def test_mismatched_trigger_database_is_rejected(tmp_path):
    runtime = ExecutionRuntime(ExecutionStore(tmp_path / "assistant.sqlite3"))
    try:
        with pytest.raises(ValueError, match="share SQLite"):
            FeishuAssistant(
                {},
                Mock(),
                project_root=tmp_path,
                client=Mock(),
                configuration=Mock(),
                research=Mock(),
                store=ProposalStore(runtime.store.path),
                execution_runtime=runtime,
                trigger_store=TriggerStore(tmp_path / "other.sqlite3"),
                start_workers=False,
            )
    finally:
        runtime.stop(timeout=3)
