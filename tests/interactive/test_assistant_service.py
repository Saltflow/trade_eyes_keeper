"""Proposal-to-confirmation integration and actual runner result vocabulary."""

import json
import threading
from unittest.mock import Mock

import pytest

from src.interactive.assistant.service import FeishuAssistant
from src.interactive.assistant.store import ProposalStore


@pytest.fixture
def assistant(tmp_path):
    configuration = Mock()
    configuration.describe.return_value = {
        "fields": [{"key": "stocks", "value": ["600036"]}]
    }
    configuration.propose.return_value = [
        {
            "target": "config/config.yaml",
            "revision": "v1",
            "changes": [{"key": "scheduler.daily_report_frequency", "value": "weekly"}],
            "diff": [{"before": "daily", "after": "weekly"}],
            "effect": "下次日报生效",
            "affected_markets": [],
        }
    ]
    configuration.apply.return_value = {"status": "applied", "effect": "下次日报生效"}
    research = Mock()
    research.describe.return_value = {"scripts": ["current_strategy"]}
    client = Mock()
    send = Mock(return_value=(True, "ok"))
    upload = Mock(return_value=(True, "ok"))
    service = FeishuAssistant(
        {
            "interactive": {"feishu": {"assistant": {"enabled": True}}},
            "llm": {"api_key": "very-secret-provider-key"},
        },
        send,
        upload,
        project_root=tmp_path,
        configuration=configuration,
        research=research,
        client=client,
        store=ProposalStore(tmp_path / "assistant.sqlite3"),
        start_workers=False,
    )
    yield service
    service.stop()


def call(name, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "tool-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_project_docs_are_supplied_and_full_turn_is_persisted(assistant):
    (assistant.root / "README.md").write_text(
        "# 项目说明\n公告栏按类型和日期筛选\n", encoding="utf-8"
    )
    assistant.client.complete.return_value = {
        "role": "assistant",
        "content": "按类型筛选，见 README.md:L1-L2。",
    }
    assistant.handle("chat", "owner", "公告栏怎么筛选", "message-1")
    messages = assistant.client.complete.call_args.args[0]
    docs = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
    assert docs["results"][0]["path"] == "README.md"
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert turn["message_id"] == "message-1"
    assert turn["question"] == "公告栏怎么筛选"
    assert turn["status"] == "completed"
    assert "README.md" in turn["answer"]
    assert {e["kind"] for e in turn["events"]} >= {
        "document_search",
        "model_request",
        "model_response",
        "reply",
        "delivery",
    }
    assert assistant.store.recent_turns("chat", "other") == []


def test_stock_code_is_looked_up_before_model_and_result_is_in_context(assistant):
    assistant.research.query_stock_fundamentals.return_value = {
        "code": "600150",
        "datasets_checked": 2,
        "selected": {"dataset_id": "ds_sample", "status": "available"},
    }
    assistant.client.complete.return_value = {
        "role": "assistant",
        "content": "本地索引找到该标的基本面记录。",
    }

    assistant.handle("chat", "owner", "我在仓库文档看到600150，数据文件如何查找？")

    assistant.research.query_stock_fundamentals.assert_called_once_with(
        "600150", _allow_backfill=False
    )
    messages = assistant.client.complete.call_args.args[0]
    lookup_index = next(
        index
        for index, item in enumerate(messages)
        if item.get("role") == "assistant"
        and any(
            call["function"]["name"] == "query_stock_fundamentals"
            for call in item.get("tool_calls", [])
        )
    )
    result = json.loads(messages[lookup_index + 1]["content"])
    assert result["selected"]["dataset_id"] == "ds_sample"
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert any(event["kind"] == "automatic_stock_lookup" for event in turn["events"])


def test_simple_stock_fundamental_question_uses_grounded_direct_answer(assistant):
    assistant.research.query_stock_fundamentals.return_value = {
        "code": "600150",
        "as_of": "2026-10-01",
        "datasets_checked": 1,
        "selected": {
            "dataset_id": "ds_sample",
            "price_date": "2026-09-30",
            "raw_close": 12.5,
            "statement_published_at": "2026-08-29",
            "currency": "CNY",
            "metrics": {
                "pb": {"value": 1.2, "status": "derived"},
                "roe_ttm": {"value": 8.4, "status": "observed"},
            },
        },
        "automatic_backfill": {
            "status": "completed",
            "market_status": "success",
            "market_end": "2026-09-30",
            "market_source": "fixture-provider",
            "fundamental_status": "success",
        },
    }

    assistant.handle("chat", "owner", "查一下 600150 的基本面指标")

    assistant.client.complete.assert_not_called()
    answer = assistant.send_message.call_args.args[1]
    assert "ds_sample" in answer
    assert "自动调用单标的时点补数链路" in answer
    assert "fixture-provider" in answer
    assert "市净率 PB：1.2（derived）" in answer
    assert "净资产收益率 TTM：8.4（observed）" in answer


def test_missing_stock_data_attempts_web_and_reports_network_failure(assistant):
    assistant.research.query_stock_fundamentals.return_value = {
        "code": "600150",
        "datasets_checked": 2,
        "selected": None,
        "dataset_results": [
            {"dataset_id": "ds_meta", "status": "not_materialized", "reason": "metadata only"}
        ],
        "matched_file_inventory": [
            {
                "dataset_id": "ds_meta",
                "relative_path": "reference_batches/codes/600150.json",
                "kind": "reference_metadata",
            }
        ],
    }
    assistant.call_tool = Mock(side_effect=OSError("Network is unreachable"))

    assistant.handle("chat", "owner", "查一下 600150 的基本面指标")

    assistant.client.complete.assert_not_called()
    assistant.call_tool.assert_called_once_with(
        "chat",
        "owner",
        "search_web",
        {"query": "600150 公司基本面 最新年报 财务指标 公告", "limit": 5},
    )
    answer = assistant.send_message.call_args.args[1]
    assert "reference_batches/codes/600150.json" in answer
    assert "网络出口不可达" in answer
    assert "我没有用模型记忆补造财务数据" in answer


def test_audit_redacts_question_reply_and_tool_events(assistant):
    secret = "very-secret-provider-key"
    assistant.client.complete.side_effect = [
        call("read_configuration", {}),
        {"role": "assistant", "content": "结果 " + secret},
    ]
    assistant.configuration.describe.return_value = {"note": secret}
    assistant.handle("chat", "owner", "测试 " + secret)
    serialized = json.dumps(assistant.store.recent_turns("chat", "owner"))
    assert secret not in serialized
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert {e["kind"] for e in turn["events"]} >= {"tool_request", "tool_result"}


def test_failed_delivery_keeps_generated_answer(assistant):
    assistant.client.complete.return_value = {
        "role": "assistant",
        "content": "有效回复",
    }
    assistant.send_message.return_value = (False, "platform rejected")
    assistant.handle("chat", "owner", "问题")
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert turn["status"] == "delivery_failed"
    assert turn["answer"] == "有效回复"
    assert turn["events"][-1]["data"]["ok"] is False


def test_model_failure_is_persisted_with_trace(assistant):
    from src.interactive.assistant.client import AssistantAPIError

    assistant.client.complete.side_effect = AssistantAPIError("服务超时")
    assistant.handle("chat", "owner", "问题")
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert turn["status"] == "failed"
    assert turn["error"] == "服务超时"
    assert any(e["kind"] == "model_error" for e in turn["events"])


def test_persisted_history_restores_only_owner_and_clear_is_a_barrier(assistant):
    assistant.client.complete.return_value = {"role": "assistant", "content": "旧回答"}
    assistant.handle("chat", "owner", "公告栏旧问题")
    assistant._history.clear()
    assistant.client.complete.return_value = {"role": "assistant", "content": "新回答"}
    assistant.handle("chat", "owner", "那时效性呢")
    messages = assistant.client.complete.call_args.args[0]
    assert any(m.get("content") == "公告栏旧问题" for m in messages)
    assistant._history.clear()
    assistant.handle("chat", "other", "时效性")
    assert all(
        m.get("content") != "公告栏旧问题"
        for m in assistant.client.complete.call_args.args[0]
    )
    assistant.handle("chat", "owner", "清空对话")
    assistant._history.clear()
    assistant.handle("chat", "owner", "新话题")
    assert all(
        m.get("content") != "公告栏旧问题"
        for m in assistant.client.complete.call_args.args[0]
    )


def test_submit_receipt_and_queue_share_one_trace(assistant):
    assistant.client.complete.return_value = {"role": "assistant", "content": "答复"}
    assert assistant.submit("chat", "owner", "问题", "one-message")
    assert not assistant.submit("chat", "owner", "问题", "one-message")
    assistant.handle(*assistant._messages.get_nowait())
    turns = assistant.store.recent_turns("chat", "owner")
    assert len(turns) == 1
    assert turns[0]["status"] == "completed"


def test_repository_tool_loop_reads_real_source_and_numbered_evidence(assistant):
    source = assistant.root / "src/example.py"
    source.parent.mkdir()
    source.write_text("def daily_report():\n    return 'scheduled'\n", encoding="utf-8")
    assistant.client.complete.side_effect = [
        call("read_repository_file", {"path": "src/example.py", "line_count": 2}),
        {
            "role": "assistant",
            "content": "入口 daily_report，见 src/example.py:L1-L2。",
        },
    ]
    assistant.handle("chat", "owner", "日报入口是什么")
    request = assistant.client.complete.call_args.args[0]
    assert "L1: def daily_report" in request[-1]["content"]
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert turn["status"] == "completed"
    assert "src/example.py:L1-L2" in turn["answer"]
    assert any(event["kind"] == "harness_finished" for event in turn["events"])


def test_unseen_citation_is_repaired_before_feishu_reply(assistant):
    (assistant.root / "README.md").write_text("# 项目\n公告筛选\n", encoding="utf-8")
    assistant.client.complete.side_effect = [
        {"role": "assistant", "content": "公告筛选，见 README.md:L52-L54。"},
        {"role": "assistant", "content": "公告筛选，见 README.md:L1-L2。"},
    ]
    assistant.handle("chat", "owner", "公告怎么筛选")
    assert assistant.send_message.call_count == 1
    assert "L52" not in assistant.send_message.call_args.args[1]
    assert "L1-L2" in assistant.send_message.call_args.args[1]
    turn = assistant.store.recent_turns("chat", "owner")[0]
    assert turn["status"] == "completed"
    assert any(event["kind"] == "citation_rejected" for event in turn["events"])


def test_natural_language_change_requires_separate_owner_confirmation(assistant):
    assistant.client.complete.side_effect = [
        call(
            "propose_configuration",
            {
                "changes": [
                    {"key": "scheduler.daily_report_frequency", "value": "weekly"}
                ]
            },
        ),
        {"role": "assistant", "content": "请检查提案并确认。"},
    ]
    assistant.handle("chat", "owner", "把日报改为每周")
    assistant.configuration.apply.assert_not_called()
    item = assistant.store.recent("chat", "owner")[0]
    assert item["status"] == "pending"
    assistant.handle("chat", "outsider", "确认 " + item["id"])
    assistant.configuration.apply.assert_not_called()
    assistant.handle("chat", "owner", "确认 " + item["id"])
    assistant.handle("chat", "owner", "确认 " + item["id"])
    assistant.configuration.apply.assert_called_once()
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "succeeded"


def test_model_has_no_confirm_tool_and_queries_are_owner_scoped(assistant):
    item = assistant.store.create("config", "chat", "owner", {})
    assistant.store.activate(item["id"])
    for name in ("confirm", "execute", "run_shell"):
        with pytest.raises(ValueError, match="权限"):
            assistant.call_tool("chat", "owner", name, {"action_id": item["id"]})
    with pytest.raises(ValueError):
        assistant.call_tool("chat", "other", "query_tasks", {"action_id": item["id"]})
    assistant.configuration.apply.assert_not_called()


def test_config_runtime_reload_failure_is_not_reported_as_success(assistant):
    assistant.configuration.apply.return_value = {
        "status": "applied_reload_failed",
        "effect": "配置已保存，运行时刷新失败",
        "runtime_refresh_error": "scheduler unavailable",
    }
    item = assistant.store.create("config", "chat", "owner", {})
    assistant.store.activate(item["id"])
    assistant.handle("chat", "owner", "确认 " + item["id"])
    result = assistant.store.get(item["id"], "chat", "owner")
    assert result["status"] == "failed"
    assert result["result"]["status"] == "applied_reload_failed"


def test_failed_script_preview_blocks_execution_and_can_be_delivered_again(
    assistant, tmp_path
):
    script = tmp_path / "script.py"
    script.write_text("print('approved script')", encoding="utf-8")
    assistant.research.prepare.return_value = {
        "job_dir": str(tmp_path),
        "preview": "运行已批准的脚本",
        "preview_files": [str(script)],
    }
    assistant.send_file.return_value = (False, "permission denied")
    outcome = assistant.call_tool("chat", "owner", "prepare_research", {})
    action_id = outcome["proposal_id"]
    assert assistant.store.get(action_id, "chat", "owner")["status"] == "preview_failed"
    assistant.handle("chat", "owner", "确认 " + action_id)
    assert assistant._jobs.empty()
    assistant.send_file.return_value = (True, "ok")
    assistant.handle("chat", "owner", "获取 " + action_id)
    assert assistant.store.get(action_id, "chat", "owner")["status"] == "pending"


def test_cancel_bypasses_pending_llm_queue_and_preserves_terminal_states(assistant):
    item = assistant.store.create("research", "chat", "owner", {})
    assistant.store.update(item["id"], "running")
    cancel = threading.Event()
    assistant._cancels[item["id"]] = cancel
    assistant._messages.put(("chat", "owner", "一个很慢的模型请求"))
    assert assistant.submit("chat", "owner", "取消 " + item["id"], "cancel-message")
    assert cancel.is_set()
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "cancelling"
    assert not assistant.store.transition(
        item["id"], "running", ("preparing", "running")
    )
    assistant.store.update(item["id"], "succeeded")
    assistant.handle("chat", "owner", "取消 " + item["id"])
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "succeeded"


def test_result_vocabulary_completed_is_normalized_to_success(assistant, tmp_path):
    # Use the actual runner's JobResult serialized output, not a fake success spelling.
    from src.interactive.assistant.research import JobResult

    item = assistant.store.create(
        "research", "chat", "owner", {"job_dir": str(tmp_path)}
    )
    assistant.store.activate(item["id"])
    item = assistant.store.claim(item["id"], "chat", "owner")
    result = JobResult(
        job_id=item["id"],
        status="completed",
        summary="actual metrics",
        artifacts=[],
        exit_code=0,
    )
    assistant.research.run.return_value = (
        result.dict() if hasattr(result, "dict") else result
    )
    assistant._jobs.put((item, threading.Event()))
    worker = threading.Thread(target=assistant._research_worker, daemon=True)
    worker.start()
    assistant._jobs.join()
    assistant._stop.set()
    worker.join(2)
    assert assistant.store.get(item["id"], "chat", "owner")["status"] == "succeeded"


def test_secret_bearing_or_outside_artifacts_are_never_uploaded(assistant, tmp_path):
    secret_log = tmp_path / "prepare.log"
    secret_log.write_text("error: very-secret-provider-key", encoding="utf-8")
    assert not assistant._files("chat", [str(secret_log)], tmp_path)
    safe = tmp_path / "result.json"
    safe.write_text('{"status":"ok"}', encoding="utf-8")
    assert not assistant._files("chat", [str(safe)], tmp_path / "other")
    assistant.send_file.assert_not_called()
    assert assistant._files("chat", [str(safe)], tmp_path)


def test_context_does_not_leak_between_senders(assistant):
    assistant.client.complete.return_value = {"role": "assistant", "content": "说明"}
    assistant.handle("chat", "owner", "独有的策略问题")
    assistant.handle("chat", "other", "新的问题")
    request = assistant.client.complete.call_args.args[0]
    assert all("独有的策略问题" not in str(message) for message in request)


def test_startup_reconciles_sealed_result_without_rerunning(tmp_path):
    store = ProposalStore(tmp_path / "assistant.sqlite3")
    item = store.create("research", "chat", "owner", {"job_id": "123456abcdef"})
    store.activate(item["id"])
    store.claim(item["id"], "chat", "owner")
    store.update(item["id"], "running")
    runner = Mock()
    runner.recover_result.return_value = {
        "status": "completed",
        "summary": "已核验保存的实际结果",
        "artifacts": [],
    }
    restarted = FeishuAssistant(
        {},
        Mock(),
        project_root=tmp_path,
        configuration=Mock(),
        research=runner,
        client=Mock(),
        store=store,
        start_workers=False,
    )
    try:
        recovered = store.get(item["id"], "chat", "owner")
        assert recovered["status"] == "succeeded"
        assert recovered["result"]["status"] == "succeeded"
        runner.recover_result.assert_called_once_with(item["payload"])
        runner.run.assert_not_called()
        assert restarted._jobs.empty()
    finally:
        restarted.stop()


@pytest.mark.parametrize("decision", ["确认", "取消"])
def test_announcement_real_config_requires_owner_confirmation(tmp_path, decision):
    from unittest.mock import patch

    import yaml

    from src.core.config_store import ConfigStore
    from src.interactive.assistant.config_tools import ConfigurationTools

    directory = tmp_path / "config"
    directory.mkdir()
    path = directory / "config.yaml"
    path.write_text(
        "stocks: ['600036']\nannouncements:\n  days: 7\n"
        "llm:\n  api_key: isolated-private-secret\n",
        encoding="utf-8",
    )
    (directory / "optimizer_constraints.yaml").write_text("{}", encoding="utf-8")
    original = path.read_bytes()
    configuration = ConfigurationTools(tmp_path)
    client = Mock()
    client.complete.side_effect = [
        call("read_configuration", {"prefix": "announcements"}),
        call(
            "propose_configuration",
            {"changes": [{"key": "announcements.days", "value": 8}]},
        ),
        {"role": "assistant", "content": "已生成预览，请确认编号后保存。"},
    ]
    send = Mock(return_value=(True, "ok"))
    service = FeishuAssistant(
        {},
        send,
        project_root=tmp_path,
        configuration=configuration,
        research=Mock(),
        client=client,
        store=ProposalStore(tmp_path / "assistant.sqlite3"),
        start_workers=False,
    )
    try:
        service.handle("chat", "owner", "把普通公告回看窗口改为8天", "request")
        assert path.read_bytes() == original
        (proposal,) = service.store.recent("chat", "owner")
        action_id = proposal["id"]
        item = service.store.get(action_id, "chat", "owner")
        assert item["status"] == "pending"
        assert item["payload"]["diff"][0]["old"] == 7
        assert item["payload"]["diff"][0]["new"] == 8
        assert "下次任务生效" in item["payload"]["effect"]
        assert any("待确认提案" in args.args[1] for args in send.call_args_list)
        assert "isolated-private-secret" not in str(send.call_args_list)
        service.handle("chat", "other", f"确认 {action_id}", "foreign-confirm")
        assert path.read_bytes() == original
        with patch.object(configuration, "apply", wraps=configuration.apply) as apply:
            service.handle("chat", "owner", f"{decision} {action_id}", "decision")
            service.handle("chat", "owner", f"确认 {action_id}", "repeat")
            assert apply.call_count == (1 if decision == "确认" else 0)
        expected = yaml.safe_load(original)
        if decision == "确认":
            expected["announcements"]["days"] = 8
        assert ConfigStore(path).load_raw() == expected
        assert service.store.get(action_id, "chat", "owner")["status"] == (
            "succeeded" if decision == "确认" else "cancelled"
        )
    finally:
        service.stop()
