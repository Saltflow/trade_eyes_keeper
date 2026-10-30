"""SYNTHETIC fixtures: A model-selected screenshot label cannot authorize a new account."""

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from src.interactive.assistant.personal_tools import (
    PersonalTools,
    account_creation_requested,
)
from src.personal_monitor import MonitorStore

SYNTHETIC_DATE_CORRECTION = (
    "现在是 2026-01-18 11:47，“10月26日”是明天，截图不可能来自明天。"
    "请确认你指的是哪一天：2025-10-26，还是 2026-01-17（或其他）？ "
    "确认了是1月18， 这批是重复账户， 之前的是错误的 这批是正确的"
)

SYNTHETIC_NAMED_CREATE = (
    "请创建独立验收账户「合成验收」，本轮后续持仓与截图都使用这个验收账户。"
)


def context(text, identity="typed-declaration"):
    return {
        "id": identity,
        "message_id": identity,
        "original_text": text,
        "effective_time": datetime.now(ZoneInfo("Asia/Shanghai")),
    }


@pytest.mark.parametrize(
    "text",
    [
        "新建账户测试账户",
        "请创建一个账户“测试账户”",
        "新增账户：测试账户",
        "账户名是测试账户",
        "账户名测试账户",
        "账户名称为「测试账户」",
        "这两张账户名都是测试账户",
        "新建一个名为“测试账户”的账户",
        "创建账户测试账户，不要导入任何持仓",
    ],
)
def test_named_current_declaration_is_accepted(text):
    assert account_creation_requested(text, "测试账户")


@pytest.mark.parametrize(
    "text,account",
    [
        (SYNTHETIC_NAMED_CREATE, "合成验收"),
        ("请创建一个独立验收账户「试用账户」", "试用账户"),
        ("新建测试持仓账户“若水”", "若水"),
        ("请开设独立测试验收持仓账号：沙盒", "沙盒"),
        ("新增验收测试账户，账户名是账户甲", "账户甲"),
        ("建立测试独立账户以后再说", "以后再说"),
    ],
)
def test_bounded_account_modifiers_are_explicit_creation(text, account):
    # A postponed declaration is rejected even when it resembles a literal name.
    assert account_creation_requested(text, account) == (account != "以后再说")


@pytest.mark.parametrize(
    "text,account",
    [
        ("不要创建独立验收账户「沙盒」", "沙盒"),
        ("禁止新建测试持仓账户沙盒", "沙盒"),
        ("如果需要，创建独立验收账户沙盒", "沙盒"),
        ("假设创建独立验收账户沙盒", "沙盒"),
        ("机器人说创建独立验收账户沙盒", "沙盒"),
        ("创建独立验收账户沙盒 是助手的原话", "沙盒"),
        ("创建独立验收账户沙盒，这句话作废", "沙盒"),
        ("创建独立验收账户沙盒吗", "沙盒"),
        ("创建独立验收账户沙盒 以后再执行", "沙盒"),
        ("创建独立验收账户沙盒，或者创建测试账户其他", "沙盒"),
        ("创建独立验收账户沙盒二号", "沙盒"),
        ("创建独立验收账户「沙盒」", "独立验收账户"),
        ("创建独立验收账户", "验收账户"),
        ("创建不要验收账户沙盒", "沙盒"),
        ("创建超高风险账户沙盒", "沙盒"),
        ("截图中的独立验收账户是沙盒", "沙盒"),
    ],
)
def test_account_modifiers_do_not_bypass_creation_authority(text, account):
    assert not account_creation_requested(text, account)


def test_actual_named_create_writes_only_requested_account(personal):
    result = personal.invoke(
        "owner",
        "chat",
        "account",
        {"account": "合成验收"},
        context(SYNTHETIC_NAMED_CREATE),
    )
    assert result["result"] == {"account": "合成验收", "revision": 0}
    account = personal.store.status("owner", "合成验收")["accounts"][0]
    assert account["positions"] == []
    assert personal.store.status("foreign-owner")["accounts"][0]["account"] == (
        "人民币账户 港股"
    )


@pytest.mark.parametrize("name", ["若水", "未完成", "暂时不", "以后再说"])
def test_literal_account_name_is_not_an_instruction_qualifier(name):
    assert account_creation_requested(f"新建账户“{name}”", name)


@pytest.mark.parametrize(
    "text,account",
    [
        (SYNTHETIC_DATE_CORRECTION, "人民币账户 港股"),
        (SYNTHETIC_DATE_CORRECTION, "人民币账户 港股 全部"),
        ("截图日期是1月18日", "默认账户"),
        ("这是当前持仓", "截图测试"),
        ("新建账户", "账户"),
        ("新建账户测试账户", "新建账户测试账户"),
        ("账户名以后再说", "以后再说"),
        ("新建账户以后再说", "以后再说"),
        ("账户名我再想想", "我再想想"),
        ("账户名称稍后再定", "稍后再定"),
        ("不要创建账户测试账户", "测试账户"),
        ("别新建账户测试账户", "测试账户"),
        ("禁止创建账户测试账户", "测试账户"),
        ("如果需要，新建账户测试账户", "测试账户"),
        ("假设创建账户测试账户", "测试账户"),
        ("考虑新建账户测试账户", "测试账户"),
        ("是否创建账户测试账户", "测试账户"),
        ("如何创建账户测试账户", "测试账户"),
        ("新建账户测试账户吗", "测试账户"),
        ("新建账户测试账户是什么意思", "测试账户"),
        ("例如新建账户测试账户", "测试账户"),
        ("我没有说新建账户测试账户", "测试账户"),
        ("机器人说新建账户测试账户", "测试账户"),
        ("“新建账户测试账户”是助手的回复", "测试账户"),
        ("创建账户测试账户，这句话作废", "测试账户"),
        ("新建账户测试账户二号", "测试账户"),
        ("账户名是测试账户二号", "测试账户"),
        ("账户名是其他账户", "测试账户"),
        ("账户名是测试账户，或者账户名是其他账户", "测试账户"),
        ("账户名是测试账户，或者账户名是其他账户", "其他账户"),
        ("新建账户测试账户", ""),
        ("新建账户测试账户", " 测试账户"),
    ],
)
def test_no_default_tail_conditional_denial_or_ambiguous_name(text, account):
    assert not account_creation_requested(text, account)


@pytest.fixture
def personal(tmp_path):
    store = MonitorStore(tmp_path / "isolated.sqlite3")
    store.ensure_account("owner", "合成AH")
    store.ensure_account("foreign-owner", "人民币账户 港股")
    return PersonalTools(store)


@pytest.mark.parametrize("account", ["人民币账户 港股", "人民币账户 港股 全部"])
def test_real_model_wrong_account_tool_does_not_create_or_bind(personal, account):
    before = personal.store.status("owner")
    ctx = context(SYNTHETIC_DATE_CORRECTION)
    ctx["image_batch"] = {
        "batch_id": "archived-image",
        "document": {"account_hint": account},
    }
    with pytest.raises(ValueError, match="新账户需要明确"):
        personal.invoke("owner", "chat", "account", {"account": account}, ctx)
    assert personal.store.status("owner") == before
    assert personal._image_account("owner", "chat", "archived-image") is None
    assert not ctx.get("personal_receipts")


@pytest.mark.parametrize("action", ["import_manual", "import_image"])
def test_import_cannot_bypass_named_account_creation(personal, action):
    before = personal.store.status("owner")
    args = {"account": "未创建"}
    if action == "import_manual":
        args.update(
            expected_revision=0, rows=[{"instrument": "00728", "quantity": 4000}]
        )
    else:
        args["batch_id"] = "archived-image"
    with pytest.raises(ValueError, match="导入不会自动建账户"):
        personal.invoke(
            "owner", "chat", action, args, context("账户未创建，00728当前4000股")
        )
    assert personal.store.status("owner") == before


def test_explicit_create_then_manual_import_still_works(personal):
    ctx = context("创建账户测试账户，00728当前4000股")
    created = personal.invoke("owner", "chat", "account", {"account": "测试账户"}, ctx)
    assert created["result"]["revision"] == 0
    result = personal.invoke(
        "owner",
        "chat",
        "import_manual",
        {
            "account": "测试账户",
            "expected_revision": 0,
            "rows": [{"instrument": "00728", "quantity": 4000}],
        },
        ctx,
    )
    assert result["result"]["applied"] == ["00728"]
    account = personal.store.status("owner", "测试账户")["accounts"][0]
    assert account["positions"][0]["quantity"] == 4000


def test_existing_account_date_correction_does_not_need_new_creation_request(personal):
    result = personal.invoke(
        "owner",
        "chat",
        "account",
        {"account": "合成AH"},
        context(SYNTHETIC_DATE_CORRECTION),
    )
    assert result["result"] == {"account": "合成AH", "revision": 0}
    assert [r["account"] for r in personal.store.status("owner")["accounts"]] == [
        "合成AH"
    ]


def test_named_reply_binds_only_authenticated_current_chat(personal):
    batch = {"batch_id": "original-image", "document": {"account_hint": "broker-label"}}
    ctx = context("账户名是测试账户")
    ctx["image_batch"] = batch
    personal.invoke("owner", "chat", "account", {"account": "测试账户"}, ctx)
    assert personal._image_account("owner", "chat", "original-image") == "测试账户"
    assert personal._image_account("owner", "foreign-chat", "original-image") is None
    assert personal._image_account("foreign-owner", "chat", "original-image") is None
