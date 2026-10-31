"""Authenticated trigger UX using temporary SQLite and data-only fixtures."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.interactive.assistant.trigger_tools import (
    TRIGGER_PROMPT,
    TriggerTools,
    trigger_tool_schema,
)
from src.triggers.catalog import build_report_field_catalog
from src.triggers.store import TriggerStore

NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)
Q3 = "任何监控标的发三季度报告的当天和次日发简报和日报"


@pytest.fixture
def tools(tmp_path):
    return TriggerTools(
        TriggerStore(tmp_path / "assistant.sqlite3"),
        "feishu:owner-A",
        "chat-A",
        message_id="message-1",
        now=NOW,
        catalog=build_report_field_catalog(),
    )


def price_spec():
    return {
        "label": "价格低于阈值",
        "condition": {"field": "quotes.601088.last", "op": "lt", "value": 50},
        "actions": [{"kind": "brief", "payload": {"highlight_symbols": ["601088"]}}],
    }


@pytest.mark.parametrize("day", [2, 20])
def test_reminder_defaults_echo_complete_date_and_timezone(tools, day):
    reply = tools.handle_text(f"11月{day}号逆回购到期")
    assert reply["status"] == "created"
    assert f"2026年11月{day:02d}日 09:50" in reply["message"]
    assert "Asia/Shanghai" in reply["message"]
    item = tools.store.get(tools.owner, reply["data"]["trigger"]["number"])
    assert item["spec"]["schedule"]["times"] == [f"2026-11-{day:02d}T01:50:00+00:00"]
    assert item["spec"]["actions"] == [
        {"kind": "reminder", "payload": {"text": "逆回购到期", "chat_id": "chat-A"}}
    ]
    assert "chat-A" not in json.dumps(reply, ensure_ascii=False)
    assert "feishu:owner-A" not in json.dumps(reply, ensure_ascii=False)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("二〇二六年十一月二十号逆回购到期", "2026年11月20日 09:50"),
        ("2027年1月2日10:05国债逆回购到期", "2027年01月02日 10:05"),
        ("提醒我在11月2号10点逆回购到期", "2026年11月02日 10:00"),
    ],
)
def test_explicit_or_chinese_reminder_dates(tools, text, expected):
    result = tools.handle_text(text)
    assert result["status"] == "created"
    assert expected in result["message"]


@pytest.mark.parametrize(
    "text",
    [
        "2月30号逆回购到期",
        "11月2号25:00逆回购到期",
        "1月2号逆回购到期",
        "2025年11月2号逆回购到期",
    ],
)
def test_invalid_or_past_date_never_rolls_year_or_creates(tools, text):
    assert tools.handle_text(text)["status"] == "needs_clarification"
    assert tools.store.list(tools.owner, include_inactive=True) == []


def test_creation_replay_is_idempotent_but_distinct_dates_are_independent(tools):
    first = tools.handle_text("11月2号逆回购到期")
    replay = tools.handle_text("11月2号逆回购到期")
    second = tools.handle_text("11月20号逆回购到期")
    assert (
        first["data"]["trigger"]["number"] == replay["data"]["trigger"]["number"] == 1
    )
    assert second["data"]["trigger"]["number"] == 2


def test_price_shortcut_uses_fresh_last_not_day_low_and_preserves_full_pool(tools):
    result = tools.handle_text("601088跌到50以下了发简报")
    assert result["status"] == "created"
    spec = tools.store.get(tools.owner, 1)["spec"]
    assert spec["condition"] == {
        "field": "quotes.601088.last",
        "op": "lt",
        "value": 50.0,
        "max_age_seconds": 120.0,
        "quantifier": "any",
    }
    assert spec["match_policy"] == "edge"
    payload = spec["actions"][0]["payload"]
    assert payload["highlight_symbols"] == ["601088"]
    assert set(payload) == {"highlight_symbols", "trigger_reason", "chat_id"}


def test_q3_event_matches_every_year_all_monitored_and_both_report_actions(tools):
    result = tools.handle_text(Q3)
    assert result["status"] == "created"
    spec = tools.store.get(tools.owner, 1)["spec"]
    assert spec["event"]["kind"] == "announcement.q3_report"
    assert spec["event"]["report_period"] == "Q3"
    assert spec["event"]["day_offsets"] == [0, 1]
    assert spec["event"]["day_after_time"] == "09:50"
    assert "symbols" not in spec["event"]
    assert "notify_at" not in spec["event"]
    assert [item["kind"] for item in spec["actions"]] == ["brief", "daily"]
    assert "立即" in result["message"] and "09:50" in result["message"]
    assert "含周末" in result["message"]


def test_recursive_report_predicate_uses_catalog_and_safe_structured_tool(tools):
    spec = price_spec()
    spec["condition"] = {
        "all": [
            {"field": "stocks_data.601088.ma60", "op": "gte", "value": 45},
            {
                "any": [
                    {"field": "stocks_data.601088.pe_ratio", "op": "lt", "value": 12},
                    {
                        "field": "quotes.601088.last",
                        "op": "lt",
                        "value": 50,
                        "max_age_seconds": 120,
                    },
                ]
            },
        ]
    }
    assert (
        tools.execute("trigger_manage", {"action": "create", "spec": spec})["status"]
        == "created"
    )
    assert tools.store.get(tools.owner, 1)["spec"]["condition"]["all"][1]["any"][0][
        "field"
    ].endswith("pe_ratio")


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "stocks_data.601088.made_up", "op": "lt", "value": 1},
        {"path": "stocks_data.601088.made_up", "op": "lt", "value": 1},
        {"not": {"field": "config.api_key", "op": "exists"}},
        {"all": [{"field": "quotes.601088.last", "op": "lt"}]},
        {"field": "quotes.601088.last", "op": "eval", "value": "run()"},
    ],
)
def test_invalid_or_unknown_condition_does_not_pretend_success(tools, condition):
    spec = price_spec()
    spec["condition"] = condition
    assert (
        tools.execute({"action": "create", "spec": spec})["status"]
        == "needs_clarification"
    )
    assert tools.store.list(tools.owner) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"chat_id": "another-chat"},
        {"route": {"chat": "another"}},
        {"job": "arbitrary"},
        {"stocks": ["601088"]},
        {"symbols": ["601088"]},
        {"tool": "send"},
        {"highlight_symbols": "601088"},
        {"trigger_reason": 1},
    ],
)
def test_action_payload_is_bounded_and_never_accepts_routing_or_job_scope(
    tools, payload
):
    spec = price_spec()
    spec["actions"][0]["payload"] = payload
    assert (
        tools.execute({"action": "create", "spec": spec})["status"]
        == "needs_clarification"
    )
    assert tools.store.list(tools.owner) == []


@pytest.mark.parametrize(
    "actions",
    [
        None,
        [],
        ["brief"],
        [{"kind": "shell"}],
        [{"kind": "reminder"}],
        [{"kind": "brief", "unknown": 1}],
    ],
)
def test_invalid_actions_return_clarification_instead_of_crashing(tools, actions):
    spec = price_spec()
    spec["actions"] = actions
    assert (
        tools.execute({"action": "create", "spec": spec})["status"]
        == "needs_clarification"
    )


def test_untrusted_owner_args_rejected_and_other_owner_is_not_disclosed(tools):
    other = TriggerTools(
        tools.store, "other-owner", "private-chat", message_id="other-message", now=NOW
    )
    other.handle_text("11月2号逆回购到期")
    for action in ["list", "get", "remove", "create"]:
        result = tools.execute({"action": action, "owner": "other-owner", "number": 1})
        assert result["status"] == "needs_clarification"
        assert "private-chat" not in json.dumps(result)
    assert tools.execute({"action": "get", "number": 1})["status"] == "not_found"
    result = tools.handle_text("remove trigger 1")
    assert result["data"]["not_found"] == [1]
    assert other.store.get("other-owner", 1)["state"] == "active"


@pytest.mark.parametrize(
    "command",
    [
        "remove trigger 1,2,3",
        "删除触发器 1，2、3",
        "移除触发器 1 2 3",
        "DELETE TRIGGERS １，２，３",
    ],
)
def test_batch_remove_accepts_chinese_aliases_fullwidth_and_spaces(tools, command):
    for day in [2, 3, 4]:
        tools.handle_text(f"11月{day}号逆回购到期")
    result = tools.handle_text(command)
    assert result["data"]["removed"] == [1, 2, 3]
    assert "已删除触发器 1、2、3" in result["message"]
    replay = tools.handle_text(command)
    assert replay["data"]["already_deleted"] == [1, 2, 3]
    assert "已不存在" in replay["message"]


@pytest.mark.parametrize(
    "command",
    [
        "remove trigger",
        "删除触发器 1，",
        "remove trigger 1; DROP TABLE",
        "remove trigger 0",
        "remove trigger -1",
        "remove trigger 1,wrong",
    ],
)
def test_bad_delete_never_partially_executes(tools, command):
    tools.handle_text("11月2号逆回购到期")
    assert tools.handle_text(command)["status"] == "needs_clarification"
    assert tools.store.get(tools.owner, 1)["state"] == "active"


def test_list_stable_numbers_survive_removal_and_store_reopen(tools):
    tools.handle_text("11月2号逆回购到期")
    tools.handle_text("11月20号逆回购到期")
    tools.handle_text("remove trigger 1")
    tools.store = TriggerStore(tools.store.path)
    result = tools.handle_text("拉取已经激活的触发器")
    assert [row["number"] for row in result["data"]["triggers"]] == [2]
    assert "2." in result["message"]
    assert tools.handle_text("11月21号逆回购到期")["data"]["trigger"]["number"] == 3


def test_pause_resume_and_inactive_listing_need_no_confirmation(tools):
    tools.handle_text("11月2号逆回购到期")
    assert tools.handle_text("暂停触发器 #1")["status"] == "updated"
    assert tools.handle_text("拉取已经激活的触发器")["data"]["triggers"] == []
    assert tools.handle_text("查看已暂停的触发器")["data"]["triggers"][0]["number"] == 1
    assert tools.handle_text("resume trigger 1")["status"] == "updated"
    assert tools.store.get(tools.owner, 1)["state"] == "active"


def test_unqualified_list_defaults_active_and_short_number_commands_work(tools):
    tools.handle_text("11月2号逆回购到期")
    assert tools.handle_text("暂停#1")["status"] == "updated"
    assert tools.handle_text("list triggers")["data"]["triggers"] == []
    assert len(tools.handle_text("查看所有触发器")["data"]["triggers"]) == 1
    assert tools.handle_text("恢复#1")["status"] == "updated"
    assert len(tools.handle_text("查看触发器")["data"]["triggers"]) == 1


def test_deletion_discloses_submitted_and_uncertain_actions_cannot_be_retracted(tools):
    tools.store.remove = Mock(
        return_value={
            "removed": [1],
            "already_deleted": [2],
            "not_found": [3],
            "cancelled_actions": 2,
            "submitted_actions": 1,
            "uncertain_actions": 1,
        }
    )
    result = tools.handle_text("remove trigger 1,2,3")
    assert "无法撤回" in result["message"]
    assert "不确定" in result["message"]
    assert result["data"]["cancelled_actions"] == 2
    tools.store.remove.assert_called_once_with(tools.owner, [1, 2, 3])


def test_catalog_prefix_filters_real_fields(tools):
    result = tools.execute({"action": "catalog", "prefix": "quotes."})
    assert result["data"]["catalog"]
    assert all(item["path"].startswith("quotes.") for item in result["data"]["catalog"])
    assert "quotes.*.last" in {item["path"] for item in result["data"]["catalog"]}


def test_list_shows_chinese_schedule_actions_next_date_and_recent_result(tools):
    tools.handle_text("11月2号逆回购到期")
    result = tools.handle_text("拉取已经激活的触发器")
    message = result["message"]
    assert "1. 逆回购到期【生效中】" in message
    assert "2026年11月02日 09:50" in message
    assert "Asia/Shanghai" in message and "提醒：逆回购到期" in message
    assert "最近结果：尚未触发" in message
    assert "[active]" not in message


def test_get_unknown_future_data_stays_waiting_and_diagnostics_hide_internal_ids(tools):
    tools.handle_text("601088跌到50以下了发简报")
    with tools.store.transaction() as db:
        tools.store.record_state(
            tools.owner,
            1,
            {
                "diagnostic": json.dumps(
                    {
                        "truth": "unknown",
                        "reasons": ["quotes.601088.last:future_observation"],
                    }
                ),
                "last_truth": "false",
            },
            db=db,
        )
    result = tools.handle_text("get trigger 1")
    assert "601088现价小于50" in result["message"]
    assert "等待数据" in result["message"] and "源数据时点在未来" in result["message"]
    assert "最近条件核验未满足" not in result["message"]
    assert "owner-A" not in json.dumps(result, ensure_ascii=False)
    assert "chat-A" not in json.dumps(result, ensure_ascii=False)
    assert "request_key" not in result["data"]["trigger"]


@pytest.mark.parametrize(
    "status,delivery,message",
    [
        ("unknown", "unknown", "结果不明"),
        ("succeeded", "sent", "已发送"),
        ("failed", "failed", "失败"),
    ],
)
def test_done_list_get_retain_real_delivery_state_without_run_identifiers(
    tools, status, delivery, message
):
    from src.triggers.evaluator import TriggerEvaluator

    tools.handle_text("11月2号逆回购到期")
    due = datetime(2026, 11, 2, 1, 50, tzinfo=timezone.utc)
    TriggerEvaluator(tools.store).evaluate({}, now=due)
    action = tools.store.claim_pending(now=due)[0]
    tools.store.mark_submitted(action["id"], "sensitive-execution-run", now=due)
    tools.store.mark_completed(
        action["id"],
        status=status,
        deliveries={"feishu": {"status": delivery}},
        now=due,
    )
    result = tools.handle_text("get trigger 1")
    assert "已结束" in result["message"]
    assert f"最近结果：{message}" in result["message"]
    assert "sensitive-execution-run" not in json.dumps(result, ensure_ascii=False)
    assert "run_id" not in result["data"]["trigger"]["last_result"]
    assert tools.handle_text("查看已完成的触发器")["data"]["triggers"][0]["number"] == 1
    changed = tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": tools.store.get(tools.owner, 1)["revision"],
            "changes": {"label": "已结束事项的新标签"},
        }
    )
    assert changed["status"] == "updated"
    assert tools.store.get(tools.owner, 1)["state"] == "done"


def test_label_edit_from_another_chat_does_not_move_existing_delivery_route(tools):
    tools.handle_text("11月2号逆回购到期")
    elsewhere = TriggerTools(
        tools.store, tools.owner, "another-chat", message_id="other-message", now=NOW
    )
    result = elsewhere.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": 1,
            "changes": {"label": "新标签"},
        }
    )
    assert result["status"] == "updated"
    assert (
        tools.store.get(tools.owner, 1)["spec"]["actions"][0]["payload"]["chat_id"]
        == tools.chat
    )


def test_missing_authenticated_message_or_naive_clock_does_not_create(tools):
    assert (
        tools.handle_text("11月2号逆回购到期", message_id="")["status"]
        == "needs_clarification"
    )
    assert (
        tools.handle_text("11月2号逆回购到期", now=NOW.replace(tzinfo=None))["status"]
        == "needs_clarification"
    )
    assert tools.store.list(tools.owner) == []


def test_unknown_prose_does_not_guess_a_rule_or_run_llm(tools):
    for text in [
        "帮我选一只股票",
        "601088跌到便宜的时候发简报",
        "下个月某一天逆回购到期",
        "",
    ]:
        assert tools.handle_text(text) is None
    assert tools.store.list(tools.owner) == []


def test_update_price_uses_current_revision_keeps_stable_number(tools):
    tools.handle_text("601088跌到50以下了发简报")
    result = tools.handle_text("修改#1价格阈值为49.5")
    assert result["status"] == "updated"
    record = tools.store.get(tools.owner, 1)
    assert record["revision"] == 2 and record["number"] == 1
    assert record["spec"]["condition"]["value"] == 49.5
    assert "49.5" in record["label"]
    assert record["spec"]["actions"][0]["payload"]["chat_id"] == tools.chat


def test_update_time_keeps_date_and_echoes_it(tools):
    tools.handle_text("11月2号逆回购到期")
    result = tools.handle_text("把触发器1的时间改为10:15")
    assert result["status"] == "updated"
    assert "2026年11月02日 10:15" in result["message"]
    assert tools.store.get(tools.owner, 1)["revision"] == 2


def test_stale_structured_update_does_not_overwrite_newer_rule(tools):
    tools.handle_text("601088跌到50以下了发简报")
    old = tools.store.get(tools.owner, 1)
    tools.handle_text("修改#1阈值为49")
    condition = deepcopy(old["spec"]["condition"])
    condition["value"] = 45
    reply = tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": old["revision"],
            "changes": {"condition": condition},
        }
    )
    assert reply["status"] == "needs_clarification"
    assert tools.store.get(tools.owner, 1)["spec"]["condition"]["value"] == 49


def test_update_can_explicitly_clear_optional_condition(tools):
    spec = price_spec()
    spec["schedule"] = {"at": "2026-11-02T09:50:00+08:00"}
    tools.execute({"action": "create", "spec": spec})
    result = tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": 1,
            "changes": {"condition": None},
        }
    )
    assert result["status"] == "updated"
    assert "condition" not in tools.store.get(tools.owner, 1)["spec"]


def test_unrelated_update_preserves_literal_business_id_in_predicate(tools):
    spec = price_spec()
    spec["condition"] = {
        "field": "ref_portfolio_status",
        "op": "eq",
        "value": {"id": "business-record", "position": 1},
    }
    created = tools.execute({"action": "create", "spec": spec})
    assert created["status"] == "created"
    changed = tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": 1,
            "changes": {"label": "新名称"},
        }
    )
    assert changed["status"] == "updated"
    assert tools.store.get(tools.owner, 1)["spec"]["condition"]["value"] == {
        "id": "business-record",
        "position": 1,
    }


def test_event_receipt_uses_actual_year_offsets_and_override_times(tools):
    spec = price_spec()
    del spec["condition"]
    spec["event"] = {
        "kind": "announcement.q3_report",
        "report_period": "2026Q3",
        "day_offsets": [1],
        "offset_times": {"1": "10:20"},
    }
    result = tools.execute({"action": "create", "spec": spec})
    assert result["status"] == "created"
    assert "2026年三季报" in result["message"] and "次日10:20" in result["message"]
    assert "当天立即" not in result["message"] and "每年" not in result["message"]


@pytest.mark.parametrize(
    "command,kind,days",
    [
        ("每天9:50发简报", "daily", list(range(7))),
        ("每周一10:00提醒我检查逆回购", "weekly", [0]),
    ],
)
def test_explicit_daily_weekly_schedule(tools, command, kind, days):
    result = tools.handle_text(command)
    assert result["status"] == "created"
    schedule = tools.store.get(tools.owner, 1)["spec"]["schedule"]
    assert schedule["kind"] == kind and schedule["weekdays"] == days
    assert "Asia/Shanghai" in result["message"]


def test_schema_and_prompt_do_not_allow_authentication_or_arbitrary_jobs():
    schema = trigger_tool_schema()[0]["function"]
    assert schema["name"] == "trigger_manage"
    props = schema["parameters"]["properties"]
    assert not {"owner", "chat", "chat_id", "sender"} & set(props)
    assert set(
        props["spec"]["properties"]["actions"]["items"]["properties"]["kind"]["enum"]
    ) == {"brief", "daily", "reminder"}
    assert "完整监控池" in TRIGGER_PROMPT
    assert "action_results逐动作" in TRIGGER_PROMPT


@pytest.fixture
def shared_execution(tools):
    """Real shared run storage, never start a worker or run a report."""
    from src.core.execution import ExecutionStore, JobSpec, ModuleSpec, RunResult

    store = ExecutionStore(tools.store.path)
    store.apply(
        ModuleSpec(
            "reports",
            tuple(
                JobSpec("reports." + kind, lambda payload, context: RunResult())
                for kind in ("brief", "daily")
            ),
        )
    )
    yield store
    store.close_idle_connections()


def multi_actions(tools, kinds=("brief", "daily"), *, occurred=True):
    value = price_spec()
    value["actions"] = [{"kind": kind, "payload": {}} for kind in kinds]
    assert tools.execute({"action": "create", "spec": value})["status"] == "created"
    record = tools.store.get(tools.owner, 1)
    if occurred:
        with tools.store.transaction() as db:
            tools.store.record_occurrence(record, "fixture-first", {}, NOW, db=db)
    return record


def submit_first(tools, execution):
    action = tools.store.claim_pending(limit=1, now=NOW)[0]
    run = execution.submit(
        "reports." + action["kind"], {}, source="synthetic-ui-receipt"
    )
    tools.store.mark_submitted(action["id"], run, now=NOW)
    return action, run


def test_each_action_without_an_occurrence_is_explicitly_not_triggered(tools):
    multi_actions(tools, occurred=False)
    reply = tools.handle_text("get trigger 1")
    assert "简报：尚未触发" in reply["message"]
    assert "日报：尚未触发" in reply["message"]
    assert [row["status"] for row in reply["data"]["trigger"]["action_results"]] == [
        "not_triggered",
        "not_triggered",
    ]


@pytest.mark.parametrize(
    "stage,status,label",
    [
        (None, "queued", "排队中"),
        ("brief_stock_data", "generating", "正在生成"),
        ("brief_notifications", "delivering", "正在投递"),
    ],
)
def test_submitted_action_reads_queued_running_and_delivery_phase_from_shared_sqlite(
    tools, shared_execution, stage, status, label
):
    multi_actions(tools)
    action, run = submit_first(tools, shared_execution)
    if stage is not None:
        shared_execution.owner_started("fixture-owner")
        claimed = shared_execution.claim("fixture-owner", "default", ["reports.brief"])
        assert claimed["id"] == run
        assert shared_execution.progress(run, claimed["claim_token"], stage)
    before = tools.store.actions(tools.owner, 1)
    reply = tools.handle_text("get trigger 1")
    results = reply["data"]["trigger"]["action_results"]
    assert results[0]["status"] == status
    assert results[1]["status"] == "pending"
    assert "简报：" + label in reply["message"] and "日报：待发送" in reply["message"]
    assert tools.store.actions(tools.owner, 1) == before
    assert run not in json.dumps(reply, ensure_ascii=False)
    assert action["id"] not in json.dumps(reply, ensure_ascii=False)


def test_http400_brief_failure_never_hides_daily_pending_and_public_codes_are_safe(
    tools, shared_execution
):
    multi_actions(tools)
    action, run = submit_first(tools, shared_execution)
    tools.store.mark_completed(
        action["id"],
        status="failed",
        reason="private-token-must-not-leak",
        deliveries={
            "feishu": {
                "status": "failed",
                "reason": "delivery_failed",
                "pieces": [
                    {
                        "status": "failed",
                        "reason": "http_rejected",
                        "http_status": 400,
                        "code": 230099,
                        "detail": "private-token-must-not-leak",
                        "owner": "other-private-owner",
                    }
                ],
            }
        },
        now=NOW,
    )
    # The execution row is still queued, while the action has a terminal durable
    # delivery result. Its terminal receipt wins over stale queue metadata.
    reply = tools.handle_text("拉取已经激活的触发器")
    assert "简报：失败（HTTP 400，平台code=230099）" in reply["message"]
    assert "日报：待发送" in reply["message"]
    public = json.dumps(reply, ensure_ascii=False)
    assert all(
        value not in public
        for value in (
            run,
            "private-token-must-not-leak",
            "other-private-owner",
            tools.owner,
            tools.chat,
        )
    )
    results = reply["data"]["triggers"][0]["action_results"]
    assert results[0]["status"] == "failed" and results[1]["status"] == "pending"
    assert "http_rejected" in results[0]["diagnostics"]


def test_same_kind_actions_keep_distinct_indices_and_outcomes(tools, shared_execution):
    multi_actions(tools, ("brief", "brief"))
    action, _ = submit_first(tools, shared_execution)
    tools.store.mark_completed(
        action["id"],
        status="unknown",
        deliveries={
            "feishu": {"status": "unknown", "reason": "http_submission_unknown"}
        },
        now=NOW,
    )
    reply = tools.handle_text("get trigger 1")
    assert "简报（动作1）：结果不明" in reply["message"]
    assert "简报（动作2）：待发送" in reply["message"]
    assert [
        item["action_index"] for item in reply["data"]["trigger"]["action_results"]
    ] == [0, 1]


def test_report_data_not_ready_is_distinct_from_acknowledged_delivery(
    tools, shared_execution
):
    multi_actions(tools)
    action, _ = submit_first(tools, shared_execution)
    tools.store.mark_completed(
        action["id"],
        status="failed",
        reason="data_not_ready:all_enabled_channels_sent",
        deliveries={
            "feishu": {"status": "sent", "pieces": [{"status": "sent"}]},
            "email": {"status": "skipped"},
        },
        now=NOW,
    )
    reply = tools.handle_text("get trigger 1")
    assert "简报：已送达；报告数据未就绪" in reply["message"]
    results = reply["data"]["trigger"]["action_results"]
    assert (
        results[0]["status"] == "sent" and "data_not_ready" in results[0]["diagnostics"]
    )
    assert results[1]["status"] == "pending"


def test_newer_occurrence_pending_is_not_hidden_by_late_old_receipt(
    tools, shared_execution
):
    record = multi_actions(tools, ("brief",))
    action, _ = submit_first(tools, shared_execution)
    with tools.store.transaction() as db:
        tools.store.record_occurrence(
            record, "fixture-newer", {}, NOW + timedelta(minutes=1), db=db
        )
    tools.store.mark_completed(
        action["id"],
        status="succeeded",
        deliveries={"feishu": {"status": "sent"}},
        now=NOW + timedelta(minutes=2),
    )
    reply = tools.handle_text("get trigger 1")
    assert reply["data"]["trigger"]["action_results"][0]["status"] == "pending"
    assert "最近结果：待发送" in reply["message"]


def test_unreconciled_terminal_execution_receipt_is_visible_without_writing_state(
    tools, shared_execution
):
    from src.core.execution import RunResult

    multi_actions(tools)
    action, run = submit_first(tools, shared_execution)
    shared_execution.owner_started("fixture-owner")
    claimed = shared_execution.claim("fixture-owner", "default", ["reports.brief"])
    assert shared_execution.finish(
        run,
        claimed["claim_token"],
        RunResult(
            "failed",
            "delivery_failed",
            deliveries={
                "feishu": {
                    "status": "failed",
                    "http_status": 400,
                    "reason": "http_rejected",
                }
            },
        ),
    )
    assert tools.store.get_action(action["id"])["state"] == "submitted"
    reply = tools.handle_text("get trigger 1")
    assert "简报：失败（HTTP 400）" in reply["message"]
    assert "日报：待发送" in reply["message"]
    assert tools.store.get_action(action["id"])["state"] == "submitted"


def test_confirmed_partial_failure_does_not_become_an_unknown_delivery(
    tools, shared_execution
):
    multi_actions(tools)
    action, _ = submit_first(tools, shared_execution)
    tools.store.mark_completed(
        action["id"],
        status="partial",
        deliveries={
            "feishu": {
                "status": "partial",
                "pieces": [
                    {"status": "sent"},
                    {"status": "failed", "reason": "http_rejected", "http_status": 400},
                    {"status": "skipped", "reason": "batch_stopped"},
                ],
            }
        },
        now=NOW,
    )
    reply = tools.handle_text("get trigger 1")
    first = reply["data"]["trigger"]["action_results"][0]
    assert first["status"] == "failed"
    assert "部分送达，其他发送失败（HTTP 400）" in reply["message"]
