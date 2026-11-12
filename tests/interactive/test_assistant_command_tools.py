"""Natural-language command tools reuse real previews, claims and dispatch."""

import json
from unittest.mock import Mock, patch

import pytest

from src.core.config_store import ConfigStore
from src.instruments.resolver import InstrumentResolver, SymbolResolutionError
from src.interactive.assistant.command_tools import prepare_command
from src.interactive.assistant.config_tools import (
    ConfigurationConflict,
    ConfigurationTools,
)
from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore
from src.interactive.command_dispatcher import CommandExecutor
from src.interactive.command_parser import BriefCommand


def resolver(*, ambiguous=False):
    sgx = Mock()
    sgx.security_identity.side_effect = lambda code: (
        {
            "code": code,
            "name": "DBS",
            "market": "sg",
            "currency": "SGD",
            "exchange": "SGX",
            "source": "sgx_public_directory",
        }
        if code == "D05.SI"
        else None
    )
    quote = Mock(
        return_value={"name": "Different issuer", "close": 12} if ambiguous else None
    )
    return InstrumentResolver({}, quote=quote, sgx=sgx)


@pytest.fixture
def tools(tmp_path):
    path = tmp_path / "config/config.yaml"
    path.parent.mkdir()
    path.write_text(
        "stocks: ['600036', 'VOO']\nllm:\n  api_key: private-secret\n", encoding="utf-8"
    )
    (path.parent / "optimizer_constraints.yaml").write_text("{}", encoding="utf-8")
    return ConfigurationTools(tmp_path, symbol_resolver=resolver())


@pytest.fixture
def bot(tmp_path, tools):
    executor = Mock()
    service = FeishuAssistant(
        {},
        Mock(return_value=(True, "ok")),
        project_root=tmp_path,
        configuration=tools,
        research=Mock(),
        client=Mock(),
        store=ProposalStore(tmp_path / "conversation.sqlite3"),
        command_executor=executor,
        start_workers=False,
    )
    yield service
    service.stop()


def raw(tools):
    return ConfigStore(tools.project_root / "config/config.yaml").load_raw()


def test_bare_symbol_is_verified_and_market_suffix_is_canonical():
    assert resolver().resolve("d05")["code"] == "D05.SI"
    with pytest.raises(SymbolResolutionError) as error:
        resolver(ambiguous=True).resolve("D05")
    assert {x["code"] for x in error.value.candidates} == {"D05", "D05.SI"}
    assert resolver(ambiguous=True).resolve("D05", "sg")["code"] == "D05.SI"


@pytest.mark.parametrize("code", ["5032", "5032.T", "D05.DE", "bad;cmd"])
def test_unsupported_or_unsafe_symbols_never_fall_through_to_us(code):
    with pytest.raises(ValueError):
        resolver().resolve(code)


def test_stock_proposals_cannot_bypass_identity_resolution(tools):
    before = raw(tools)
    (proposal,) = tools.propose(
        [{"key": "stocks", "value": before["stocks"] + ["D05"]}]
    )
    assert raw(tools) == before
    assert proposal["changes"][0]["value"] == ["600036", "VOO", "D05.SI"]
    assert proposal["diff"][0]["resolved_instruments"][0]["name"] == "DBS"
    tools.apply(proposal)
    assert raw(tools)["stocks"] == ["600036", "VOO", "D05.SI"]
    assert raw(tools)["llm"] == before["llm"]


def test_add_remove_are_incremental_idempotent_and_revision_bound(tools):
    (proposal,) = tools.propose_watchlist("add", ["D05"])
    store = ConfigStore(tools.project_root / "config/config.yaml")
    store.update(lambda c: c["stocks"].append("00883"))
    with pytest.raises(ConfigurationConflict):
        tools.apply(proposal)
    assert raw(tools)["stocks"] == ["600036", "VOO", "00883"]

    (proposal,) = tools.propose_watchlist("add", ["D05"])
    tools.apply(proposal)
    assert tools.propose_watchlist("add", ["d05", "D05.SI"]) == []
    (proposal,) = tools.propose_watchlist("remove", ["D05"])
    tools.apply(proposal)
    assert raw(tools)["stocks"] == ["600036", "VOO", "00883"]


def test_market_hint_cannot_remove_a_different_market_or_partially_add(tools):
    (proposal,) = tools.propose_watchlist("add", ["D05"])
    tools.apply(proposal)
    assert tools.propose_watchlist("remove", ["D05"], "us") == []
    with pytest.raises(ValueError, match="市场"):
        tools.propose_watchlist("remove", ["D05.SI"], "us")
    before = raw(tools)
    with pytest.raises(ValueError):
        tools.propose_watchlist("add", ["D05", "NOTREAL"])
    assert raw(tools) == before


def test_existing_yaml_numeric_codes_keep_their_values_and_types(tools, monkeypatch):
    store = ConfigStore(tools.project_root / "config/config.yaml")
    store.update(lambda c: c.update(stocks=[512810, "00883", "VOO"]))
    (proposal,) = tools.propose_watchlist("add", ["D05"])
    tools.apply(proposal)
    assert raw(tools)["stocks"] == [512810, "00883", "VOO", "D05.SI"]
    assert tools.propose_watchlist("add", ["512810"]) == []
    (proposal,) = tools.propose_watchlist("remove", ["D05"])
    tools.apply(proposal)
    assert raw(tools)["stocks"] == [512810, "00883", "VOO"]
    from src.interactive.commands import handlers

    monkeypatch.setattr(handlers, "CONFIG_PATH", tools.project_root / "config/config.yaml")
    assert "512810" in handlers.handle_remove(["512810"])
    assert raw(tools)["stocks"] == ["00883", "VOO"]


def test_schedule_tool_uses_existing_preview_without_enabling_reports(tools):
    store = ConfigStore(tools.project_root / "config/config.yaml")
    store.update(
        lambda c: c.update(
            scheduler={
                "daily_enabled": False,
                "run_time": "19:00",
                "data_collection_mode": "three_daily",
            }
        )
    )
    with pytest.raises(ValueError, match="trigger_manage"):
        prepare_command(tools.project_root, tools, "/schedule daily 20:00")
    assert raw(tools)["scheduler"]["run_time"] == "19:00"
    assert raw(tools)["scheduler"] == {
        "daily_enabled": False,
        "run_time": "19:00",
        "data_collection_mode": "three_daily",
    }


def test_tool_exposure_list_and_owner_bound_confirmation(bot, tools):
    assert any(x["function"]["name"] == "bot_command" for x in bot.tool_schema())
    assert bot.call_tool("chat", "owner", "watchlist_manage", {"action": "list"})[
        "stocks"
    ] == ["600036", "VOO"]
    result = bot.call_tool("chat", "owner", "watchlist_manage", {"action": "add", "codes": ["D05"]})
    assert "DBS D05.SI SGX SGD" in result["previews"][0]
    assert "600036" not in result["previews"][0]
    assert raw(tools)["stocks"] == ["600036", "VOO"]
    action_id = result["proposal_ids"][0]
    with pytest.raises(ValueError):
        bot._control("chat", "other", "确认", action_id)
    bot._control("chat", "owner", "确认", action_id)
    assert raw(tools)["stocks"] == ["600036", "VOO", "D05.SI"]
    with pytest.raises(ValueError):
        bot._control("chat", "owner", "确认", action_id)
    assert "private-secret" not in str(bot.send_message.call_args_list)


def test_ambiguous_tool_returns_choices_without_writing(bot, tools):
    tools.symbol_resolver = resolver(ambiguous=True)
    result = bot.call_tool("chat", "owner", "watchlist_manage", {"action": "add", "codes": ["D05"]})
    assert result["status"] == "needs_clarification"
    assert len(result["candidates"]) == 2
    assert bot.store.recent("chat", "owner") == []
    assert raw(tools)["stocks"] == ["600036", "VOO"]


def test_unavailable_alternative_market_requires_a_choice():
    lookup = resolver(ambiguous=True)
    lookup._sgx.security_identity.side_effect = TimeoutError("source unavailable")
    with pytest.raises(SymbolResolutionError) as error:
        lookup.resolve("D05")
    assert error.value.candidates[0]["code"] == "D05"


def test_failed_preview_cannot_be_confirmed(bot):
    bot.send_message.return_value = (False, "offline")
    result = bot.call_tool("chat", "owner", "watchlist_manage", {"action": "add", "codes": ["D05"]})
    assert result["state"] == "preview_failed"
    with pytest.raises(ValueError):
        bot._control("chat", "owner", "确认", result["proposal_ids"][0])


def test_generic_tool_cannot_accidentally_replace_the_whole_watchlist(bot):
    with pytest.raises(ValueError, match="增量"):
        bot.call_tool(
            "chat",
            "owner",
            "propose_configuration",
            {"changes": [{"key": "stocks", "value": ["D05"]}]},
        )


def test_reports_use_real_queue_and_keep_run_id_after_confirmation(bot, monkeypatch):
    monkeypatch.setattr(bot.execution_runtime, "start_if_unowned", lambda: False)
    result = bot.call_tool("chat", "owner", "report_request", {"kind": "brief"})
    assert not bot.execution_runtime.store.list_runs(job_id="reports.brief")
    action_id = result["proposal_ids"][0]
    assert bot.store.get(action_id, "chat", "owner")["kind"] == "report"
    bot._control("chat", "owner", "确认", action_id)
    item = bot.store.get(action_id, "chat", "owner")
    assert item["status"] == "succeeded"
    run = bot.execution_runtime.store.get(item["result"]["run_id"])
    assert run["status"] == "queued"
    assert run["request_id"] == "proposal:" + action_id
    assert run["payload"]["kind"] == "brief"
    bot.command_executor.execute.assert_not_called()
    with pytest.raises(ValueError):
        bot._control("chat", "owner", "确认", action_id)
    assert len(bot.execution_runtime.store.list_runs(job_id="reports.brief")) == 1


def test_changed_config_or_cancelled_preview_prevents_dispatch(bot, tools):
    result = bot.call_tool("chat", "owner", "report_request", {"kind": "daily"})
    ConfigStore(tools.project_root / "config/config.yaml").update(
        lambda c: c.update(extra=True)
    )
    bot._control("chat", "owner", "确认", result["proposal_ids"][0])
    bot.command_executor.execute.assert_not_called()
    result = bot.call_tool("chat", "owner", "configuration_save", {})
    action_id = result["proposal_ids"][0]
    bot._control("chat", "owner", "取消", action_id)
    with pytest.raises(ValueError):
        bot._control("chat", "owner", "确认", action_id)
    bot.command_executor.execute.assert_not_called()


@pytest.mark.parametrize(
    "question,arguments", [("加D05", {"action": "add", "codes": ["D05"]}), ("看看监控列表", {"action": "list"})]
)
def test_natural_management_uses_real_pi_tool_loop_without_model_payment(
    bot, tools, real_pi_bridge, question, arguments
):
    bot.client.complete.side_effect = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "add",
                    "type": "function",
                    "function": {
                        "name": "watchlist_manage",
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "已核实为SGX的DBS，代码D05.SI，请确认预览。"},
    ]
    # This fixture binds the already-created harness as well as later clones.
    from src.interactive.assistant.harness import ConversationHarness

    harness = ConversationHarness(bot.client, bot.settings, bot.root, bot._stop)
    bot.harness = harness
    bot.handle("chat", "owner", question, "natural-management")
    items = bot.store.recent("chat", "owner")
    if arguments["action"] == "add":
        assert len(items) == 1
        item = bot.store.get(items[0]["id"], "chat", "owner")
        assert item["payload"]["changes"][0]["value"][-1] == "D05.SI"
    else:
        assert items == []
    assert bot.store.recent_turns("chat", "owner")[0]["status"] == "completed"
    assert raw(tools)["stocks"] == ["600036", "VOO"]


def test_command_errors_and_native_add_share_the_same_guard(tools, monkeypatch):
    with pytest.raises(ValueError):
        prepare_command(tools.project_root, tools, "/list\n/save")
    with pytest.raises(ValueError):
        prepare_command(tools.project_root, tools, "/optimize")
    from src.interactive.commands import handlers

    monkeypatch.setattr(
        handlers, "CONFIG_PATH", tools.project_root / "config/config.yaml"
    )
    resolve = resolver().resolve
    monkeypatch.setattr(
        InstrumentResolver,
        "resolve",
        lambda self, code, market=None: resolve(code, market),
    )
    assert "D05.SI" in handlers.handle_add(["D05"])
    assert raw(tools)["stocks"][-1] == "D05.SI"


def test_command_executor_keeps_callback_receipt_and_stopped_state():
    done = Mock()
    executor = CommandExecutor(Mock())
    with patch(
        "src.interactive.command_dispatcher.dispatch_command", return_value="已排队"
    ):
        executor.execute("chat", BriefCommand(), on_result=done)
    done.assert_called_once_with("已排队")
    executor.stop()
    executor.execute("chat", BriefCommand(), on_result=done)
    assert done.call_args.args[0].startswith("❌")


@pytest.mark.parametrize("command", ["/daily", "/brief"])
def test_old_report_tool_branch_is_retired(bot, command):
    with pytest.raises(ValueError, match="report_request"):
        bot.call_tool("chat", "owner", "bot_command", {"command": command})
    bot.command_executor.execute.assert_not_called()


@pytest.mark.parametrize("unreadable", [False, True])
def test_report_submission_receipt_loss_never_causes_replay(bot, monkeypatch, unreadable):
    from src.interactive.assistant.report_tools import ReportTools

    monkeypatch.setattr(bot.execution_runtime, "start_if_unowned", lambda: False)
    action = bot.call_tool("chat", "owner", "report_request", {"kind": "daily"})["proposal_ids"][0]
    monkeypatch.setattr(ReportTools, "get", Mock(side_effect=OSError("receipt read failed")))
    if unreadable:
        monkeypatch.setattr(ReportTools, "for_request", Mock(side_effect=OSError("store unavailable")))
    bot._control("chat", "owner", "确认", action)
    item = bot.store.get(action, "chat", "owner")
    runs = bot.execution_runtime.store.list_runs(job_id="reports.daily")
    assert len(runs) == 1
    if unreadable:
        assert item["status"] == "interrupted"
        assert item["result"]["submission_status"] == "unknown"
    else:
        assert item["status"] == "succeeded"
        assert item["result"]["run_id"] == runs[0]["id"]
    with pytest.raises(ValueError):
        bot._control("chat", "owner", "确认", action)
    assert len(bot.execution_runtime.store.list_runs(job_id="reports.daily")) == 1


def test_restart_query_finds_committed_report_without_replaying_submission(bot, monkeypatch):
    monkeypatch.setattr(bot.execution_runtime, "start_if_unowned", lambda: False)
    action = bot.call_tool("chat", "owner", "report_request", {"kind": "daily"})["proposal_ids"][0]
    bot.store.claim(action, "chat", "owner")
    bot.store.transition(action, "applying", ("queued",))
    run = bot._report_tools_for("chat", "owner").submit(kind="daily", force=True, request_id="proposal:" + action)
    bot.store.recover()
    assert bot.store.get(action, "chat", "owner")["result"] is None
    detail = bot.call_tool("chat", "owner", "query_tasks", {"action_id": action})
    assert detail["status"] == "interrupted"
    assert detail["execution"]["run_id"] == run["run_id"]
    assert len(bot.execution_runtime.store.list_runs(job_id="reports.daily")) == 1


def test_report_request_completes_real_conversation_without_extra_retrieval(
    bot, real_pi_bridge
):
    from src.interactive.assistant.harness import ConversationHarness

    bot.client.complete.side_effect = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "report-preview",
                "type": "function",
                "function": {
                    "name": "report_request",
                    "arguments": json.dumps({"kind": "daily"}),
                },
            }],
        },
        {"role": "assistant", "content": "报告预览已生成，请确认后执行。"},
    ]
    bot.harness = ConversationHarness(bot.client, bot.settings, bot.root, bot._stop)
    bot.handle("chat", "owner", "现在生成完整日报", "natural-report")
    assert bot.store.recent_turns("chat", "owner")[0]["status"] == "completed"
    actions = bot.store.recent("chat", "owner")
    assert len(actions) == 1
    assert actions[0]["kind"] == "report"
    assert not bot.execution_runtime.store.list_runs(job_id="reports.daily")
    assert bot.client.complete.call_count == 2
