"""Authenticated trigger UX using temporary SQLite and data-only fixtures."""

import json
from concurrent.futures import ThreadPoolExecutor
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
Q3 = {
    "label": "监控标的三季报",
    "event": {
        "kind": "announcement.q3_report",
        "report_period": "Q3",
        "day_offsets": [0, 1],
    },
    "match_policy": "level",
    "actions": [{"kind": "brief"}, {"kind": "daily"}],
}


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
        "condition": {
            "field": "quotes.601088.last",
            "op": "lt",
            "value": 50,
            "max_age_seconds": 120,
        },
        "actions": [{"kind": "brief", "payload": {"highlight_symbols": ["601088"]}}],
    }


def maturity_spec(day=2, kind="daily"):
    return {
        "label": "逆回购到期",
        "schedule": {"at": f"2026-11-{day:02d}T09:50:00+08:00"},
        "match_policy": "once",
        "actions": [{"kind": kind, "payload": {"trigger_reason": "逆回购到期"}}],
    }


@pytest.mark.parametrize("day", [2, 20])
def test_structured_maturity_uses_complete_daily_report(tools, day):
    reply = tools.execute({"action": "create", "spec": maturity_spec(day)})
    assert reply["status"] == "created"
    assert f"2026年11月{day:02d}日 09:50" in reply["message"]
    assert "Asia/Shanghai" in reply["message"]
    item = tools.store.get(tools.owner, reply["data"]["trigger"]["number"])
    assert item["spec"]["schedule"]["times"] == [f"2026-11-{day:02d}T01:50:00+00:00"]
    assert item["spec"]["actions"] == [
        {
            "kind": "daily",
            "payload": {"trigger_reason": "逆回购到期", "chat_id": "chat-A"},
        }
    ]
    assert "chat-A" not in json.dumps(reply, ensure_ascii=False)
    assert "feishu:owner-A" not in json.dumps(reply, ensure_ascii=False)


def test_explicit_brief_for_maturity_uses_existing_brief_action(tools):
    result = tools.execute({"action": "create", "spec": maturity_spec(kind="brief")})
    assert result["status"] == "created"
    assert [
        item["kind"] for item in tools.store.get(tools.owner, 1)["spec"]["actions"]
    ] == ["brief"]


def test_creation_replay_is_idempotent_but_distinct_dates_are_independent(tools):
    first = tools.execute({"action": "create", "spec": maturity_spec()})
    replay = tools.execute({"action": "create", "spec": maturity_spec()})
    second = tools.execute({"action": "create", "spec": maturity_spec(20)})
    assert (
        first["data"]["trigger"]["number"] == replay["data"]["trigger"]["number"] == 1
    )
    assert second["data"]["trigger"]["number"] == 2


def test_price_rule_uses_fresh_last_not_day_low_and_preserves_full_pool(tools):
    result = tools.execute({"action": "create", "spec": price_spec()})
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


def test_price_reuse_preserves_original_and_request_replay_after_edit(tools):
    first = tools.execute({"action": "create", "spec": price_spec()})
    original = tools.store.get(tools.owner, 1)
    value = price_spec()
    value["label"] = "换个说法"
    value["condition"]["value"] = 50.0
    value["actions"][0]["payload"]["trigger_reason"] = "另一份展示说明"
    replay = tools.execute({"action": "create", "spec": value}, message_id="message-2")
    assert (
        first["data"]["trigger"]["number"] == replay["data"]["trigger"]["number"] == 1
    )
    assert tools.store.get(tools.owner, 1) == original
    prepared = tools._prepare(value)
    assert tools.store.create(tools.owner, prepared, "explicit-reuse")["number"] == 1
    tools.store = TriggerStore(tools.store.path)
    tools.store.update(tools.owner, 1, {"label": "后续修改"}, expected_revision=1)
    assert (
        tools.store.create(tools.owner, prepared, "explicit-reuse")["label"]
        == "后续修改"
    )
    with pytest.raises(ValueError, match="different"):
        tools.store.create(
            tools.owner, {**prepared, "label": "不同请求"}, "explicit-reuse"
        )


@pytest.mark.parametrize(
    "change", ["freshness", "cooldown", "policy", "highlight", "schedule"]
)
def test_same_price_signature_with_changed_parameters_requires_update(tools, change):
    tools.execute({"action": "create", "spec": price_spec()})
    spec = price_spec()
    if change == "freshness":
        spec["condition"]["max_age_seconds"] = 60
    elif change == "cooldown":
        spec["cooldown_seconds"] = 60
    elif change == "policy":
        spec["match_policy"] = "once"
    elif change == "highlight":
        spec["actions"][0]["payload"]["highlight_symbols"] = []
    else:
        spec["schedule"] = {"at": "2026-11-02T09:50:00+08:00"}
    reply = tools.execute({"action": "create", "spec": spec}, message_id="changed")
    assert reply["status"] == "needs_clarification" and "update" in reply["message"]
    assert len(tools.store.list(tools.owner, include_inactive=True)) == 1
    with tools.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM trigger_requests").fetchone()[0] == 1


def test_price_reuse_is_atomic_across_different_messages(tools):
    def create(index):
        spec = price_spec()
        spec["label"] = f"同一价格条件说法{index}"
        return tools.execute(
            {"action": "create", "spec": spec}, message_id=f"parallel-{index}"
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(create, range(12)))
    assert {reply["data"]["trigger"]["number"] for reply in replies} == {1}
    with tools.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM trigger_definitions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trigger_requests").fetchone()[0] == 12
        assert db.execute("SELECT next_number FROM trigger_owners").fetchone()[0] == 2


@pytest.mark.parametrize("state", ["paused", "done", "deleted"])
def test_inactive_price_rule_is_not_reused_as_active(tools, state):
    tools.execute({"action": "create", "spec": price_spec()})
    with tools.store.transaction() as db:
        db.execute("UPDATE trigger_definitions SET state=? WHERE number=1", (state,))
    reply = tools.execute(
        {"action": "create", "spec": price_spec()}, message_id="new-active"
    )
    assert reply["data"]["trigger"]["number"] == 2
    assert tools.store.get(tools.owner, 1)["state"] == state


@pytest.mark.parametrize(
    "owner,chat,number",
    [("another-owner", "chat-A", 1), ("feishu:owner-A", "another-chat", 2)],
)
def test_price_reuse_does_not_cross_owner_or_chat(tools, owner, chat, number):
    tools.execute({"action": "create", "spec": price_spec()})
    other = TriggerTools(tools.store, owner, chat, message_id="other", now=NOW)
    reply = other.execute({"action": "create", "spec": price_spec()})
    assert reply["data"]["trigger"]["number"] == number
    assert (
        other.store.get(owner, number)["spec"]["actions"][0]["payload"]["chat_id"]
        == chat
    )


@pytest.mark.parametrize("change", ["symbol", "threshold", "operator", "kind"])
def test_different_price_signature_remains_an_independent_rule(tools, change):
    tools.execute({"action": "create", "spec": price_spec()})
    spec = price_spec()
    if change == "symbol":
        spec["condition"]["field"] = "quotes.000001.last"
    elif change == "threshold":
        spec["condition"]["value"] = 49
    elif change == "operator":
        spec["condition"]["op"] = "lte"
    else:
        spec["actions"][0]["kind"] = "daily"
    reply = tools.execute({"action": "create", "spec": spec}, message_id="independent")
    assert reply["data"]["trigger"]["number"] == 2


def test_q3_event_matches_every_year_all_monitored_and_both_report_actions(tools):
    result = tools.execute({"action": "create", "spec": Q3})
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
    other.execute({"action": "create", "spec": maturity_spec()})
    for action in ["list", "get", "remove", "create"]:
        result = tools.execute({"action": action, "owner": "other-owner", "number": 1})
        assert result["status"] == "needs_clarification"
        assert "private-chat" not in json.dumps(result)
    assert tools.execute({"action": "get", "number": 1})["status"] == "not_found"
    result = tools.execute({"action": "remove", "number": 1})
    assert result["data"]["not_found"] == [1]
    assert other.store.get("other-owner", 1)["state"] == "active"


def test_batch_remove_is_idempotent(tools):
    for day in [2, 3, 4]:
        tools.execute({"action": "create", "spec": maturity_spec(day)})
    result = tools.execute({"action": "remove", "numbers": [1, 2, 3]})
    assert result["data"]["removed"] == [1, 2, 3]
    assert "已删除触发器 1、2、3" in result["message"]
    replay = tools.execute({"action": "remove", "numbers": [1, 2, 3]})
    assert replay["data"]["already_deleted"] == [1, 2, 3]
    assert "已不存在" in replay["message"]


@pytest.mark.parametrize("numbers", [[], [1, 0], [1, -1], [1, "wrong"], [True]])
def test_bad_delete_never_partially_executes(tools, numbers):
    tools.execute({"action": "create", "spec": maturity_spec()})
    assert (
        tools.execute({"action": "remove", "numbers": numbers})["status"]
        == "needs_clarification"
    )
    assert tools.store.get(tools.owner, 1)["state"] == "active"


def test_list_stable_numbers_survive_removal_and_store_reopen(tools):
    tools.execute({"action": "create", "spec": maturity_spec()})
    tools.execute({"action": "create", "spec": maturity_spec(20)})
    tools.execute({"action": "remove", "number": 1})
    tools.store = TriggerStore(tools.store.path)
    result = tools.execute({"action": "list"})
    assert [row["number"] for row in result["data"]["triggers"]] == [2]
    assert "2." in result["message"]
    assert (
        tools.execute({"action": "create", "spec": maturity_spec(21)})["data"][
            "trigger"
        ]["number"]
        == 3
    )


def test_pause_resume_and_inactive_listing_need_no_confirmation(tools):
    tools.execute({"action": "create", "spec": maturity_spec()})
    assert tools.execute({"action": "pause", "number": 1})["status"] == "updated"
    assert tools.execute({"action": "list"})["data"]["triggers"] == []
    assert (
        tools.execute({"action": "list", "state": "paused"})["data"]["triggers"][0][
            "number"
        ]
        == 1
    )
    assert tools.execute({"action": "resume", "number": 1})["status"] == "updated"
    assert tools.store.get(tools.owner, 1)["state"] == "active"


def test_list_defaults_active_and_explicit_all_includes_paused(tools):
    tools.execute({"action": "create", "spec": maturity_spec()})
    assert tools.execute({"action": "pause", "number": 1})["status"] == "updated"
    assert tools.execute({"action": "list"})["data"]["triggers"] == []
    assert (
        len(tools.execute({"action": "list", "state": "all"})["data"]["triggers"]) == 1
    )
    assert tools.execute({"action": "resume", "number": 1})["status"] == "updated"
    assert len(tools.execute({"action": "list"})["data"]["triggers"]) == 1


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
    result = tools.execute({"action": "remove", "numbers": [1, 2, 3]})
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
    tools.execute({"action": "create", "spec": maturity_spec()})
    result = tools.execute({"action": "list"})
    message = result["message"]
    assert "1. 逆回购到期【生效中】" in message
    assert "2026年11月02日 09:50" in message
    assert "Asia/Shanghai" in message and "日报（完整监控池）" in message
    assert "最近结果：尚未触发" in message
    assert "[active]" not in message


def test_get_unknown_future_data_stays_waiting_and_diagnostics_hide_internal_ids(tools):
    tools.execute({"action": "create", "spec": price_spec()})
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
    result = tools.execute({"action": "get", "number": 1})
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

    tools.execute({"action": "create", "spec": maturity_spec()})
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
    result = tools.execute({"action": "get", "number": 1})
    assert "已结束" in result["message"]
    assert f"最近结果：{message}" in result["message"]
    assert "sensitive-execution-run" not in json.dumps(result, ensure_ascii=False)
    assert "run_id" not in result["data"]["trigger"]["last_result"]
    assert (
        tools.execute({"action": "list", "state": "done"})["data"]["triggers"][0][
            "number"
        ]
        == 1
    )
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
    tools.execute({"action": "create", "spec": maturity_spec()})
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
        tools.execute({"action": "create", "spec": maturity_spec()}, message_id="")[
            "status"
        ]
        == "needs_clarification"
    )
    assert (
        tools.execute(
            {"action": "create", "spec": maturity_spec()}, now=NOW.replace(tzinfo=None)
        )["status"]
        == "needs_clarification"
    )
    assert tools.store.list(tools.owner) == []


def test_update_price_uses_current_revision_keeps_stable_number(tools):
    tools.execute({"action": "create", "spec": price_spec()})
    result = tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": 1,
            "changes": {
                "condition": {**price_spec()["condition"], "value": 49.5},
                "label": "价格低于49.5",
            },
        }
    )
    assert result["status"] == "updated"
    record = tools.store.get(tools.owner, 1)
    assert record["revision"] == 2 and record["number"] == 1
    assert record["spec"]["condition"]["value"] == 49.5
    assert "49.5" in record["label"]
    assert record["spec"]["actions"][0]["payload"]["chat_id"] == tools.chat


def test_update_time_keeps_date_and_echoes_it(tools):
    tools.execute({"action": "create", "spec": maturity_spec()})
    result = tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": 1,
            "changes": {"schedule": {"at": "2026-11-02T10:15:00+08:00"}},
        }
    )
    assert result["status"] == "updated"
    assert "2026年11月02日 10:15" in result["message"]
    assert tools.store.get(tools.owner, 1)["revision"] == 2


def test_stale_structured_update_does_not_overwrite_newer_rule(tools):
    tools.execute({"action": "create", "spec": price_spec()})
    old = tools.store.get(tools.owner, 1)
    tools.execute(
        {
            "action": "update",
            "number": 1,
            "expected_revision": 1,
            "changes": {"condition": {**price_spec()["condition"], "value": 49}},
        }
    )
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
    "kind,days,report_kind",
    [("daily", list(range(7)), "brief"), ("weekly", [0], "daily")],
)
def test_explicit_daily_weekly_schedule(tools, kind, days, report_kind):
    spec = {
        "label": "定时报表",
        "schedule": {"kind": kind, "at": "09:50", "weekdays": days},
        "actions": [{"kind": report_kind}],
    }
    result = tools.execute({"action": "create", "spec": spec})
    assert result["status"] == "created"
    saved = tools.store.get(tools.owner, 1)["spec"]
    assert saved["schedule"]["kind"] == kind and saved["schedule"]["weekdays"] == days
    assert "Asia/Shanghai" in result["message"]
    assert [action["kind"] for action in saved["actions"]] == [report_kind]


def test_reserved_optimize_cannot_create_a_live_rule(tools):
    spec = price_spec()
    spec["actions"] = [{"kind": "optimize"}]
    result = tools.execute({"action": "create", "spec": spec})
    assert result["status"] == "needs_clarification"
    assert "预留" in result["message"] and "未启用" in result["message"]
    assert tools.store.list(tools.owner, include_inactive=True) == []


def test_schema_and_prompt_do_not_allow_authentication_or_arbitrary_jobs():
    schema = trigger_tool_schema()[0]["function"]
    assert schema["name"] == "trigger_manage"
    props = schema["parameters"]["properties"]
    assert not {"owner", "chat", "chat_id", "sender"} & set(props)
    assert set(
        props["spec"]["properties"]["actions"]["items"]["properties"]["kind"]["enum"]
    ) == {"daily", "brief", "optimize"}
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
    reply = tools.execute({"action": "get", "number": 1})
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
    reply = tools.execute({"action": "get", "number": 1})
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
    reply = tools.execute({"action": "list"})
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
    reply = tools.execute({"action": "get", "number": 1})
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
    reply = tools.execute({"action": "get", "number": 1})
    assert "飞书：已送达；报告数据未就绪" in reply["message"]
    assert "邮件：未发送" in reply["message"]
    results = reply["data"]["trigger"]["action_results"]
    assert (
        results[0]["status"] == "partial"
        and "data_not_ready" in results[0]["diagnostics"]
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
    reply = tools.execute({"action": "get", "number": 1})
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
    reply = tools.execute({"action": "get", "number": 1})
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
    reply = tools.execute({"action": "get", "number": 1})
    first = reply["data"]["trigger"]["action_results"][0]
    assert first["status"] == "failed"
    assert "部分送达，其他发送失败（HTTP 400）" in reply["message"]


@pytest.mark.parametrize(
    "email_status,expected",
    [
        ("failed", "partial"),
        ("unknown", "unknown"),
        ("skipped", "partial"),
        ("sent", "sent"),
    ],
)
def test_email_and_feishu_are_independent_channels_with_no_private_receipt_leak(
    tools, shared_execution, email_status, expected
):
    multi_actions(tools, ("daily",))
    action, run = submit_first(tools, shared_execution)
    email = {
        "status": email_status,
        "reason": {
            "failed": "smtp_rejected",
            "unknown": "smtp_submission_unknown",
            "skipped": "trigger_email_unauthorized",
            "sent": "smtp_accepted",
        }[email_status],
        "archive": "private-archive-path",
        "message_id": "private-email-message-id",
        "error": "raw-secret-smtp-error",
        "pieces": [
            {
                "recipient": "private-recipient@example.test",
                "status": email_status,
                **(
                    {"smtp_code": 550 if email_status == "failed" else 250}
                    if email_status in {"failed", "sent"}
                    else {}
                ),
            }
        ],
    }
    tools.store.mark_completed(
        action["id"],
        status="partial" if email_status != "sent" else "succeeded",
        deliveries={"email": email, "feishu": {"status": "sent"}},
        now=NOW,
    )
    reply = tools.execute({"action": "get", "number": 1})
    result = reply["data"]["trigger"]["action_results"][0]
    assert result["status"] == expected
    assert result["channels"]["feishu"]["status"] == "sent"
    assert result["channels"]["email"]["status"] == email_status
    assert "飞书：已送达" in reply["message"] and "邮件：" in reply["message"]
    if email_status == "failed":
        assert "邮件：失败：邮件服务器拒绝（SMTP 550）" in reply["message"]
    if email_status == "sent":
        assert "邮件：已发送（SMTP已接收）" in reply["message"]
    public = json.dumps(reply, ensure_ascii=False)
    assert all(
        value not in public
        for value in (
            run,
            "private-archive-path",
            "private-email-message-id",
            "raw-secret-smtp-error",
            "private-recipient@example.test",
            tools.owner,
            tools.chat,
        )
    )


def test_partial_smtp_recipient_rejection_is_not_hidden_by_feishu_success(
    tools, shared_execution
):
    multi_actions(tools, ("daily",))
    action, _ = submit_first(tools, shared_execution)
    tools.store.mark_completed(
        action["id"],
        status="partial",
        deliveries={
            "email": {
                "status": "partial",
                "pieces": [
                    {
                        "recipient": "private-sent@example.test",
                        "status": "sent",
                        "reason": "smtp_accepted",
                    },
                    {
                        "recipient": "private-refused@example.test",
                        "status": "failed",
                        "reason": "recipient_rejected",
                        "smtp_code": 553,
                    },
                ],
            },
            "feishu": {"status": "sent"},
        },
        now=NOW,
    )
    reply = tools.execute({"action": "get", "number": 1})
    result = reply["data"]["trigger"]["action_results"][0]
    assert result["status"] == "partial"
    assert result["channels"]["email"]["status"] == "failed"
    assert (
        "部分发送，其他发送失败" in reply["message"] and "SMTP 553" in reply["message"]
    )
    assert "private-" not in json.dumps(reply, ensure_ascii=False)


def test_historical_feishu_only_result_marks_email_not_requested_not_sent(
    tools, shared_execution
):
    multi_actions(tools, ("daily",))
    action, _ = submit_first(tools, shared_execution)
    tools.store.mark_completed(
        action["id"],
        status="succeeded",
        deliveries={"feishu": {"status": "sent"}},
        now=NOW,
    )
    reply = tools.execute({"action": "get", "number": 1})
    channels = reply["data"]["trigger"]["action_results"][0]["channels"]
    assert channels["email"]["status"] == "not_requested"
    assert channels["feishu"]["status"] == "sent"
    assert "邮件未请求" in reply["message"]
