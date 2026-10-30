"""SYNTHETIC fixtures: Pure holdings queries render authenticated stored facts without model tails."""

import json
import time
from datetime import datetime
from unittest.mock import Mock

import pytest
from zoneinfo import ZoneInfo

from src.interactive.assistant.personal_tools import PersonalTools
from src.interactive.assistant.service import FeishuAssistant
from src.personal_monitor import MonitorStore


@pytest.fixture
def service(tmp_path):
    client = Mock()
    client.complete.return_value = {"role": "assistant", "content": "模型路径"}
    assistant = FeishuAssistant(
        {"interactive": {"feishu": {"assistant": {"enabled": True}}}},
        Mock(return_value=(True, "local capture")),
        project_root=tmp_path,
        client=client,
        configuration=Mock(),
        research=Mock(),
        capabilities={"personal"},
        personal_route_allowed=lambda route: route == "chat",
        start_workers=False,
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    for owner, name, code in [
        ("owner", "合成AH", "601088"),
        ("foreign", "秘密账户", "00700"),
    ]:
        assistant.personal_store.ensure_account(owner, name)
        assistant.personal_store.import_snapshot(
            owner,
            name,
            [
                {
                    "instrument": code,
                    "name": "中国神华" if owner == "owner" else "秘密证券",
                    "quantity": 200,
                    "quantity_unit": "股",
                    "price": 49.22,
                    "market_value": 9844,
                    "currency": "CNY",
                    "quote_evidence": {
                        "price": 50.2,
                        "currency": "CNY",
                        "source": "controlled_fixture",
                        "provider_time": now,
                    },
                }
            ],
            now,
            0,
            "screenshot",
        )
    yield assistant
    assistant.stop()


@pytest.mark.parametrize(
    "text",
    [
        "查看我现在的持仓",
        "我看一眼现在的持仓",
        "查询持仓",
        "我的持仓？",
        "查看合成AH当前持仓",
        "查询账户合成AH持仓",
    ],
)
def test_pure_query_renders_canonical_code_and_separate_fact_quote_times(service, text):
    service._history[("chat", "owner")] = (
        time.time(),
        [{"role": "assistant", "content": "旧错误：031 中国神华"}],
    )
    service.handle("chat", "owner", text, "query")
    answer = service.send_message.call_args.args[1]
    assert "601088 中国神华" in answer and "031 中国神华" not in answer
    assert "事实时间" in answer and "行情时间" in answer
    assert "49.22" in answer and "50.2" in answer and "controlled_fixture" in answer
    assert "秘密账户" not in answer and "00700" not in answer
    service.client.complete.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "查看持仓并把00728改为4000股",
        "查看持仓，明天提醒我",
        "我的持仓需要如何更正？",
        "如果查看持仓就提醒",
        "查询持仓后撤销上次修改",
        "查看秘密账户持仓",
    ],
)
def test_write_conditional_explanatory_or_foreign_account_requests_keep_model_path(
    service, text
):
    assert service.personal.holdings_query_scope("owner", text) is None


def test_query_uses_existing_route_permission_and_never_leaks_on_denial(service):
    service.handle("denied-chat", "owner", "查看我现在的持仓", "denied-query")
    answer = service.send_message.call_args.args[1]
    assert "601088" not in answer and "9844" not in answer
    service.client.complete.assert_not_called()


def test_query_does_not_create_or_change_any_positions_rules_or_notifications(service):
    before = service.personal_store.status("owner")
    service.handle("chat", "owner", "查看我现在的持仓", "readonly-query")
    assert service.personal_store.status("owner") == before


def test_query_displays_empty_accounts_and_pending_issues_without_invented_units(
    tmp_path,
):
    store = MonitorStore(tmp_path / "empty.sqlite3")
    store.ensure_account("owner", "empty")
    tools = PersonalTools(store)
    status = store.status("owner")
    status["accounts"][0]["pending_issues"] = [
        {"instrument": "888880", "reason": "non_equity_asset_pending,ambiguous_fields"}
    ]
    answer = tools.render_holdings(status)
    assert "没有已记录持仓" in answer
    assert "888880" in answer and "期限待确认" in answer
    assert "6.7手" not in answer and "错误代码" not in answer


@pytest.mark.parametrize(
    "text",
    [
        "查看合成AH账户现在的持仓，并列出数量、各来源的数据时间及尚未确认的信息。",
        "查看账户合成AH现在的持仓，并显示来源及数据时间及待确认信息",
        "查询合成AH持仓并列出数量、来源、事实时间、未确认信息",
    ],
)
def test_named_readonly_projection_tail_has_no_model_or_write(service, text):
    before = service.personal_store.status("owner")
    service.handle("chat", "owner", text, "projected-query")
    answer = service.send_message.call_args.args[1]
    assert "601088 中国神华" in answer and "事实时间" in answer
    assert "来源 截图" in answer and "行情时间" in answer
    assert service.personal_store.status("owner") == before
    service.client.complete.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "不要查看合成AH账户持仓",
        "如果查看合成AH账户持仓就提醒",
        "查看合成AH账户持仓并更正数量",
        "查看合成AH账户持仓并列出数量然后撤销",
        "查看合成AH账户持仓并分析估值",
        "查看合成AH账户持仓并列出数量及为什么需要更正",
        "查看秘密账户现在的持仓，并列出数量及数据时间",
    ],
)
def test_projection_tail_does_not_intercept_non_read_intents(service, text):
    assert service.personal.holdings_query_scope("owner", text) is None


def seed_pending_repos(
    service,
    *,
    linked_account="合成AH",
    batch_id="repo-pending",
    source_owner="owner",
    source_chat="chat",
):
    import json

    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    rows = [
        {
            "instrument": "131990",
            "code": "131990",
            "quantity": 6.7,
            "quantity_unit": None,
            "price": 100,
            "market_value": 67000.0,
            "currency": "CNY",
            "market_value_currency": "CNY",
            "importable": False,
            "ready": False,
            "reason": "non_equity_asset_pending,ambiguous_fields",
        },
        {
            "instrument": "888880",
            "code": "888880",
            "quantity": 8.9,
            "quantity_unit": None,
            "price": 100,
            "market_value": 89000.0,
            "currency": "CNY",
            "market_value_currency": "CNY",
            "importable": False,
            "ready": False,
            "reason": "non_equity_asset_pending,ambiguous_fields",
        },
    ]
    service.personal_store.import_snapshot(
        "owner", "合成AH", rows, stamp, 1, source="screenshot"
    )
    batch = {
        "batch_id": batch_id,
        "document": {"positions": rows, "as_of": stamp},
        "import_result": {"pending": True},
        "import_account": linked_account,
    }
    with service.personal_store.connection(write=True) as database:
        database.execute(
            "INSERT INTO personal_image_batches VALUES(?,?,?,?,?,?)",
            (
                source_owner,
                source_chat,
                batch_id,
                json.dumps(batch),
                "partial_pending",
                stamp,
            ),
        )
        database.execute(
            "INSERT INTO personal_image_account_links VALUES(?,?,?,?)",
            (source_owner, source_chat, batch_id, linked_account),
        )


def test_pending_repo_amounts_stay_with_same_code_and_unconfirmed_unit(service):
    seed_pending_repos(service)
    before = service.personal_store.status("owner")
    service.handle(
        "chat",
        "owner",
        "查看合成AH账户现在的持仓，并列出数量、各来源的数据时间及尚未确认的信息。",
        "pending-projection",
    )
    answer = service.send_message.call_args.args[1]
    lines = answer.splitlines()
    row131 = next(
        lines[i + 1]
        for i, line in enumerate(lines)
        if line.startswith("- 待确认 131990")
    )
    row888 = next(
        lines[i + 1]
        for i, line in enumerate(lines)
        if line.startswith("- 待确认 888880")
    )
    assert "截图数量 6.7" in row131 and "截图市值 67000.0" in row131
    assert "截图数量 8.9" in row888 and "截图市值 89000.0" in row888
    assert "单位待确认" in row131 and "单位待确认" in row888
    assert "到期日期" not in answer and "8.9股" not in row888
    assert service.personal_store.status("owner") == before
    service.client.complete.assert_not_called()


def test_pending_image_details_need_same_confirmed_account(service):
    seed_pending_repos(service, linked_account="另一账户")
    service.handle("chat", "owner", "查看合成AH账户现在的持仓", "other-pending")
    answer = service.send_message.call_args.args[1]
    assert "截图市值 89000.0" not in answer and "截图市值 67000.0" not in answer
    assert "另一账户" not in answer
    service.client.complete.assert_not_called()


@pytest.mark.parametrize(
    "source_owner,source_chat", [("foreign", "chat"), ("owner", "foreign-chat")]
)
def test_pending_image_details_stay_with_authenticated_owner_and_chat(
    service, source_owner, source_chat
):
    seed_pending_repos(service, source_owner=source_owner, source_chat=source_chat)
    service.handle("chat", "owner", "查看合成AH账户现在的持仓", "isolated-pending")
    answer = service.send_message.call_args.args[1]
    assert "截图市值 89000.0" not in answer and "截图市值 67000.0" not in answer
    service.client.complete.assert_not_called()


def test_projection_does_not_choose_between_two_own_named_accounts(service):
    service.personal_store.ensure_account("owner", "第二账户")
    assert (
        service.personal.holdings_query_scope(
            "owner", "查看合成AH和第二账户的持仓并列出数量"
        )
        is None
    )


def status_call(arguments=None):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "status-read",
                "type": "function",
                "function": {
                    "name": "personal_monitor_status",
                    "arguments": json.dumps(arguments or {}),
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "question",
    [
        "查看我现在的持仓（附带说明）",
        "能把账户的资产和未确认信息整理一下吗",
        "查一下个人监控的持仓以及提醒状态",
    ],
)
def test_model_selected_status_cannot_invent_repo_units_or_quote_freshness(
    service, question
):
    seed_pending_repos(service)
    before = service.personal_store.status("owner")
    service.client.complete.side_effect = [
        status_call(),
        {
            "role": "assistant",
            "content": "实时状态：最新价49.22；6.7张、8.9张，按收盘价计算。",
        },
    ]
    service.handle("chat", "owner", question, "model-read")
    answer = service.send_message.call_args.args[1]
    assert "6.7（单位待确认）" in answer and "8.9（单位待确认）" in answer
    assert "记录价格 49.22" in answer and "行情核对：50.2" in answer
    assert (
        "实时状态" not in answer
        and "最新价" not in answer
        and "收盘价计算" not in answer
    )
    assert "6.7张" not in answer and "8.9张" not in answer and "秘密账户" not in answer
    assert service.personal_store.status("owner") == before
    assert service.client.complete.call_count == 2


def test_model_status_read_keeps_reminders_rules_exceptions_and_delivery_states(
    service,
):
    result = service.personal_store.status("owner")
    account = result["accounts"][0]
    account.update(
        rules=[
            {
                "rule_id": "configured",
                "definition": {
                    "metric": "expected_return",
                    "tiers": [{"id": "custom", "value": 0.0437}],
                },
            }
        ],
        exceptions=["601088"],
        tiers=[{"tier_id": "custom", "state": "sent"}],
        evaluations=[{"reason": "data_not_ready"}],
    )
    result["reminders"] = [
        {
            "reminder_id": "roll",
            "text": "滚存",
            "due_at": "2026-11-02T01:50:00+00:00",
            "route": "chat",
            "revision": 3,
            "state": "pending",
        }
    ]
    result["outbox"] = [
        {
            "id": "unconfirmed",
            "text": "旧提醒",
            "state": "unknown",
            "attempts": 1,
            "token": "must-not-display",
        }
    ]
    answer = PersonalTools.render_status(result)
    for fact in (
        "0.0437",
        "custom",
        "暂停例外",
        "data_not_ready",
        "滚存",
        "2026-11-02 09:50:00",
        "待发送",
        "版本 3",
        "投递待核查",
    ):
        assert fact in answer
    assert "must-not-display" not in answer


def test_failed_later_status_read_never_reuses_earlier_success(service, monkeypatch):
    original = service.call_tool
    calls = []

    def invoke(chat, owner, name, args):
        calls.append(name)
        if len(calls) == 2:
            raise ValueError("controlled read failure")
        return original(chat, owner, name, args)

    monkeypatch.setattr(service, "call_tool", invoke)
    service.client.complete.side_effect = [
        status_call(),
        status_call(),
        {"role": "assistant", "content": "旧结果49.22，已核对。"},
    ]
    service.handle("chat", "owner", "重新读两次我的持仓信息", "read-failed")
    answer = service.send_message.call_args.args[1]
    assert "查询未完成" in answer and "49.22" not in answer and "601088" not in answer


def test_model_status_observation_does_not_leak_into_next_no_tool_turn(service):
    service.client.complete.side_effect = [
        status_call({"account": "合成AH"}),
        {"role": "assistant", "content": "不可靠摘要"},
        {"role": "assistant", "content": "收到。"},
    ]
    service.handle("chat", "owner", "把我的资产记录详细列一下", "first-read")
    assert "601088" in service.send_message.call_args.args[1]
    service.handle("chat", "owner", "收到谢谢", "next-chat")
    assert service.send_message.call_args.args[1] == "收到。"


def test_two_explicit_account_reads_do_not_hide_the_first_account(service):
    service.personal_store.ensure_account("owner", "第二账户")
    service.client.complete.side_effect = [
        status_call({"account": "合成AH"}),
        status_call({"account": "第二账户"}),
        {"role": "assistant", "content": "只显示第二账户。"},
    ]
    service.handle("chat", "owner", "分别查询合成AH和第二账户的持仓", "two-reads")
    answer = service.send_message.call_args.args[1]
    assert "合成AH（版本" in answer and "601088" in answer
    assert "第二账户（版本" in answer and "没有已记录持仓" in answer
    assert "秘密账户" not in answer


def test_actual_named_account_query_is_a_complete_read_request(service):
    service.personal_store.ensure_account("owner", "合成验收")
    assert service.personal.holdings_query_scope(
        "owner",
        "查看合成验收账户现在的持仓，并列出数量、各来源的数据时间及尚未确认的信息。",
    ) == {"account": "合成验收"}


@pytest.mark.parametrize("operation", ["import_image", "import_manual"])
def test_duplicate_import_receipt_reports_no_new_position_change(operation):
    answer = PersonalTools.render_receipts(
        {
            "personal_receipts": [
                {
                    "operation": operation,
                    "result": {
                        "account": "合成AH",
                        "applied": ["601088", "600036"],
                        "duplicate": True,
                        "changes": [],
                        "revision": 1,
                        "history_id": None,
                        "rejected": [
                            {"instrument": "888880", "reason": "ambiguous_fields"}
                        ],
                    },
                }
            ]
        }
    )
    assert "重复" in answer and "本次持仓未更改" in answer
    assert "601088" in answer and "600036" in answer
    assert "888880" in answer and "仍有不明确的字段" in answer
    assert "已更新" not in answer and "版本 1" not in answer


@pytest.mark.parametrize("operation", ["import_image", "import_manual"])
def test_actual_new_import_receipt_keeps_committed_change(operation):
    answer = PersonalTools.render_receipts(
        {
            "personal_receipts": [
                {
                    "operation": operation,
                    "result": {
                        "account": "合成AH",
                        "applied": ["601088"],
                        "duplicate": False,
                        "changes": [
                            {
                                "instrument": "601088",
                                "before_quantity": 100,
                                "after_quantity": 200,
                            }
                        ],
                        "history_id": "isolated-history",
                    },
                }
            ]
        }
    )
    assert "已更新" in answer and "601088：100 → 200" in answer
    assert "撤销上次持仓修改" in answer and "重复" not in answer
