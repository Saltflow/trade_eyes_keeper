"""SYNTHETIC fixtures: complete facts retain authenticated provenance."""

from datetime import datetime, timedelta
from itertools import permutations

import pytest
from zoneinfo import ZoneInfo

from src.interactive.assistant.personal_tools import PersonalTools
from src.personal_monitor import MonitorStore, RevisionConflict

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime.fromisoformat("2026-01-15T13:00:00+08:00")
FACT_TIME = datetime.fromisoformat("2026-01-15T11:45:00.123+08:00")
FACT_ID = "om_SYNTHETIC_FACT_001"
EXECUTE_ID = "om_SYNTHETIC_EXECUTE_001"
SYNTHETIC_TEXT = (
    "账户合成AH，H股按 2026-01-15 更正为：01816 中广核 3400股、00728 中国电信 5600股、"
    "09999 网易 120股、01339 中国人民保险集团 27800股，之前那组数量作废。"
)
NEW = {"01816": 3400, "00728": 5600, "09999": 120, "01339": 27800}
OLD = dict(NEW, **{"00728": 2300, "01339": 26900})


def context(identity=EXECUTE_ID, text="按刚才更正执行", when=NOW):
    return {
        "id": identity,
        "message_id": identity,
        "original_text": text,
        "effective_time": when,
    }


def arguments(refs, *, revision=1):
    return {
        "account": "合成AH",
        "expected_revision": revision,
        "source_message_ids": refs,
        "rows": [
            {"instrument": code, "quantity": quantity} for code, quantity in NEW.items()
        ],
    }


def account(tools):
    return tools.store.status("owner", "合成AH")["accounts"][0]


def remember(tools, identity, text, when=FACT_TIME, *, owner="owner", chat="chat"):
    tools.remember_user_message(owner, chat, identity, text, when)


@pytest.fixture
def personal(tmp_path, monkeypatch):
    from src.interactive.assistant import personal_tools

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(personal_tools, "datetime", FrozenDatetime)
    store = MonitorStore(tmp_path / "confirmation_sources.sqlite3")
    store.ensure_account("owner", "合成AH")
    store.import_snapshot(
        "owner",
        "合成AH",
        [{"instrument": code, "quantity": quantity} for code, quantity in OLD.items()]
        + [{"instrument": "601088", "quantity": 200}],
        (FACT_TIME - timedelta(minutes=5)).isoformat(),
        0,
        "manual",
    )
    return PersonalTools(store)


def apply(tools, refs, *, ctx=None, revision=1):
    return tools.invoke(
        "owner",
        "chat",
        "import_manual",
        arguments(refs, revision=revision),
        ctx or context(),
    )["result"]


def assert_current_fact(tools):
    state = account(tools)
    assert {
        row["instrument"]: row["quantity"]
        for row in state["positions"]
        if len(row["instrument"]) == 5
    } == NEW
    for row in state["positions"]:
        if row["instrument"] not in NEW:
            continue
        assert datetime.fromisoformat(row["as_of"]) == FACT_TIME
        evidence = row["manual_evidence"]
        assert evidence["message_ids"] == [FACT_ID]
        assert evidence["quantity_message_id"] == evidence["code_message_id"] == FACT_ID
        assert datetime.fromisoformat(evidence["quantity_effective_time"]) == FACT_TIME
        assert datetime.fromisoformat(evidence["code_effective_time"]) == FACT_TIME


def test_complete_old_fact_plus_current_execute_confirmation(personal):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    result = apply(personal, [FACT_ID, EXECUTE_ID])
    assert result["revision"] == 2
    assert set(result["applied"]) == set(NEW)
    assert_current_fact(personal)


@pytest.mark.parametrize(
    "refs",
    list(permutations([FACT_ID, "confirm-a"]))
    + list(permutations([FACT_ID, "confirm-a", "confirm-b"])),
)
@pytest.mark.parametrize("confirmation_before_fact", [False, True])
def test_fact_free_confirmation_count_order_and_age_do_not_change_evidence(
    personal, refs, confirmation_before_fact
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    when = (
        FACT_TIME - timedelta(minutes=10)
        if confirmation_before_fact
        else NOW - timedelta(minutes=1)
    )
    remember(personal, "confirm-a", "按刚才更正执行", when)
    remember(personal, "confirm-b", "确认执行", when + timedelta(seconds=1))
    apply(personal, list(refs))
    assert_current_fact(personal)


def test_complete_fact_outside_top_ten_and_old_confirmation_remain_retrievable(
    personal,
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "old-confirm", "确认执行", FACT_TIME + timedelta(seconds=1))
    for index in range(12):
        remember(
            personal,
            f"chatter-{index}",
            "随便看看",
            FACT_TIME + timedelta(minutes=index + 1),
        )
    apply(personal, ["old-confirm", FACT_ID, EXECUTE_ID])
    assert_current_fact(personal)


def test_confirmation_only_reference_cannot_silently_substitute_another_source(
    personal,
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [EXECUTE_ID])
    assert account(personal) == before


def test_status_exposes_explicit_complete_source_tool_arguments_for_execution(personal):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    before = account(personal)
    result = personal.invoke(
        "owner", "chat", "status", {"account": "合成AH"}, context("status", "查看持仓")
    )
    assert account(personal) == before
    candidate = next(
        row
        for row in result["validated_manual_candidates"]
        if row["source_message_id"] == FACT_ID
    )
    assert candidate["applicable"]
    assert candidate["tool_arguments"]["source_message_ids"] == [FACT_ID]
    personal.invoke(
        "owner", "chat", "import_manual", candidate["tool_arguments"], context()
    )
    assert_current_fact(personal)


def test_confirmation_without_any_fact_cannot_create_positions(personal):
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [EXECUTE_ID])
    assert account(personal) == before


@pytest.mark.parametrize("source_kind", ["fact", "confirmation"])
@pytest.mark.parametrize("scope", ["owner", "chat"])
def test_foreign_owner_or_chat_reference_is_never_accepted(
    personal, source_kind, scope
):
    kwargs = {scope: "foreign"}
    if source_kind == "fact":
        remember(personal, FACT_ID, SYNTHETIC_TEXT, **kwargs)
        refs = [FACT_ID, EXECUTE_ID]
    else:
        remember(personal, FACT_ID, SYNTHETIC_TEXT)
        remember(
            personal,
            "foreign-confirm",
            "确认执行",
            NOW - timedelta(seconds=1),
            **kwargs,
        )
        refs = [FACT_ID, "foreign-confirm"]
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, refs)
    assert account(personal) == before


def test_confirmation_explicitly_naming_another_account_is_not_fact_free(personal):
    personal.store.ensure_account("owner", "其他账户")
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "other-account", "确认执行其他账户", NOW - timedelta(seconds=1))
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, "other-account", EXECUTE_ID])
    assert account(personal) == before


def test_complete_fact_explicitly_for_another_account_cannot_be_borrowed(personal):
    personal.store.ensure_account("owner", "其他账户")
    remember(personal, FACT_ID, SYNTHETIC_TEXT.replace("合成AH", "其他账户"))
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, EXECUTE_ID])
    assert account(personal) == before


def test_newer_incompatible_complete_reference_is_not_a_fact_free_confirmation(
    personal,
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(
        personal,
        "new-fact",
        SYNTHETIC_TEXT.replace("5600股", "8900股"),
        NOW - timedelta(seconds=1),
    )
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, "new-fact", EXECUTE_ID])
    assert account(personal) == before


@pytest.mark.parametrize(
    "later",
    [
        "账户合成AH，00728当前6000股",
        "之前那组数量作废",
        "00728不是4000股",
        "不要执行这次更正",
        "00728已清仓",
        "00728已经全部卖出",
        "00728当前0股",
    ],
)
def test_later_conflicting_or_withdrawing_message_blocks_old_complete_fact(
    personal, later
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "objection", later, NOW - timedelta(minutes=1))
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, EXECUTE_ID])
    assert account(personal) == before


@pytest.mark.parametrize(
    "later", ["00728价格4港元", "00728币种USD", "00728市值9000港元"]
)
def test_later_metadata_fact_blocks_stale_candidate_and_manual_write(personal, later):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "newer-metadata", later, NOW - timedelta(minutes=1))
    before = account(personal)
    status = personal.invoke(
        "owner",
        "chat",
        "status",
        {"account": "合成AH"},
        context("metadata-review", "查看持仓"),
    )
    assert not any(
        item["source_message_id"] == FACT_ID and item["applicable"]
        for item in status["validated_manual_candidates"]
    )
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, EXECUTE_ID])
    assert account(personal) == before


@pytest.mark.parametrize("later", ["00728没有清仓", "如果00728已清仓就提醒我"])
def test_negative_or_conditional_clearing_is_not_an_asserted_zero_position(
    personal, later
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "not-current-clearing", later, NOW - timedelta(minutes=1))
    apply(personal, [FACT_ID, EXECUTE_ID])
    assert_current_fact(personal)


@pytest.mark.parametrize(
    "future_kind", ["fact_time", "confirmation_time", "declared_date"]
)
def test_future_fact_or_confirmation_is_rejected(personal, future_kind):
    text = (
        SYNTHETIC_TEXT.replace("2026-01-15", "2099-01-01")
        if future_kind == "declared_date"
        else SYNTHETIC_TEXT
    )
    when = NOW + timedelta(minutes=1) if future_kind == "fact_time" else FACT_TIME
    remember(personal, FACT_ID, text, when)
    confirm_time = (
        NOW + timedelta(minutes=1)
        if future_kind == "confirmation_time"
        else NOW - timedelta(seconds=1)
    )
    remember(personal, "confirmation", "确认执行", confirm_time)
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, "confirmation"])
    assert account(personal) == before


def test_naive_confirmation_mixed_with_aware_fact_fails_as_validation_error(personal):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "naive-confirm", "确认执行", NOW.replace(tzinfo=None))
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, [FACT_ID, "naive-confirm"])
    assert account(personal) == before


@pytest.mark.parametrize(
    "field,typed_fact,expected",
    [
        ("price", "价格4港元", 4.0),
        ("currency", "币种USD", "USD"),
        ("market_value", "市值9000港元", 9000.0),
    ],
)
def test_metadata_only_manual_candidate_preview_agrees_with_actual_write(
    personal, field, typed_fact, expected
):
    personal.store.import_snapshot(
        "owner",
        "合成AH",
        [
            {
                "instrument": "00728",
                "quantity": 2000,
                "price": 3.0,
                "currency": "HKD",
                "market_value": 6000.0,
            }
        ],
        (FACT_TIME - timedelta(minutes=1)).isoformat(),
        1,
        "manual",
    )
    remember(personal, "metadata-fact", "账户合成AH，00728当前2000股，" + typed_fact)
    before = account(personal)
    status = personal.invoke(
        "owner",
        "chat",
        "status",
        {"account": "合成AH"},
        context("metadata-status", "查看持仓"),
    )
    assert account(personal) == before
    candidate = next(
        item
        for item in status["validated_manual_candidates"]
        if item["source_message_id"] == "metadata-fact"
    )
    assert candidate["applicable"] and not candidate["already_current"]
    change = next(row for row in candidate["changes"] if row["instrument"] == "00728")
    assert change["field_changes"][field]["after"] == expected
    args = candidate["tool_arguments"]
    assert args["rows"] == [{"instrument": "00728", "quantity": 2000}]
    assert args["source_message_ids"] == ["metadata-fact"]
    personal.invoke("owner", "chat", "import_manual", args, context("metadata-execute"))
    after = account(personal)
    changed = next(row for row in after["positions"] if row["instrument"] == "00728")
    assert changed["quantity"] == 2000 and changed[field] == expected
    assert changed["manual_evidence"]["message_ids"] == ["metadata-fact"]
    assert [row for row in after["positions"] if row["instrument"] != "00728"] == [
        row for row in before["positions"] if row["instrument"] != "00728"
    ]


def test_complete_old_fact_does_not_bypass_watermark(personal):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    personal.store.import_snapshot(
        "owner",
        "合成AH",
        [{"instrument": "00728", "quantity": 6000}],
        (NOW - timedelta(minutes=1)).isoformat(),
        1,
        "manual",
    )
    before = account(personal)
    result = apply(personal, [FACT_ID, EXECUTE_ID], revision=2)
    assert result["applied"] == []
    assert {row["reason"] for row in result["rejected"]} == {"older_account_snapshot"}
    assert account(personal) == before


def test_complete_fact_does_not_bypass_compare_and_swap(personal):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    before = account(personal)
    with pytest.raises(RevisionConflict):
        apply(personal, [FACT_ID, EXECUTE_ID], revision=0)
    assert account(personal) == before


def split_sources(personal, *, old=False, evict=False):
    when = FACT_TIME if old else NOW - timedelta(minutes=3)
    quantity_text = (
        "账户合成AH，H股分别是中广核3400股，电信5600股，网易120股，PICC 27800股"
    )
    remember(personal, "quantity", quantity_text, when)
    remember(
        personal,
        "codes",
        "代码分别是01816，00728，09999，01339",
        when + timedelta(seconds=20),
    )
    if evict:
        for index in range(12):
            remember(
                personal,
                f"later-{index}",
                "随便看看",
                when + timedelta(seconds=30 + index),
            )


def test_genuine_split_facts_within_window_keep_original_quantity_time(personal):
    split_sources(personal)
    apply(personal, ["quantity", "codes", EXECUTE_ID])
    for row in account(personal)["positions"]:
        if row["instrument"] not in NEW:
            continue
        evidence = row["manual_evidence"]
        assert evidence["message_ids"] == ["quantity", "codes"]
        assert evidence["quantity_message_id"] == "quantity"
        assert evidence["code_message_id"] == "codes"
        assert datetime.fromisoformat(row["as_of"]) == NOW - timedelta(minutes=3)


@pytest.mark.parametrize("expired", [True, False])
def test_genuine_split_facts_keep_window_and_top_ten_constraints(personal, expired):
    split_sources(personal, old=expired, evict=not expired)
    before = account(personal)
    with pytest.raises(ValueError):
        apply(personal, ["quantity", "codes", EXECUTE_ID])
    assert account(personal) == before


def database_rows(personal, table):
    with personal.store.connection() as db:
        return [
            dict(row)
            for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")
        ]


def seed_delivered_notification(personal):
    personal.store.register_route("owner", "chat")
    personal.store.create_reminder(
        "owner",
        "matured",
        (FACT_TIME - timedelta(days=1)).isoformat(),
        "已提醒过",
        "chat",
        now=NOW,
    )
    with personal.store.connection(write=True) as db:
        db.execute(
            "INSERT INTO outbox(id,business_key,owner,account,kind,route,text,payload,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "sent-threshold",
                "sent-threshold",
                "owner",
                "合成AH",
                "threshold",
                "chat",
                "已触发",
                '{"instrument":"00728","rule_id":"rule"}',
                "sent",
                FACT_TIME.isoformat(),
            ),
        )
        db.execute(
            "INSERT INTO tiers VALUES(?,?,?,?,?,?,?)",
            (
                "owner",
                "合成AH",
                "00728",
                "rule",
                "configured-tier",
                "sent",
                "sent-threshold",
            ),
        )


def test_equivalent_reference_sets_do_not_repeat_journal_or_notifications_and_history_does_not_revive_them(
    personal,
):
    remember(personal, FACT_ID, SYNTHETIC_TEXT)
    remember(personal, "extra-confirm", "确认执行", NOW - timedelta(seconds=1))
    seed_delivered_notification(personal)
    tables = ["reminders", "outbox", "tiers"]
    notifications = {name: database_rows(personal, name) for name in tables}
    apply(personal, [FACT_ID, EXECUTE_ID])
    entries = database_rows(personal, "position_history_entries")
    requests = database_rows(personal, "position_history_requests")
    for refs in ([EXECUTE_ID, FACT_ID], ["extra-confirm", FACT_ID, EXECUTE_ID]):
        result = apply(personal, refs, ctx=context(), revision=2)
        assert result["duplicate"] and result["applied"] == []
        assert account(personal)["revision"] == 2
        assert database_rows(personal, "position_history_entries") == entries
        assert database_rows(personal, "position_history_requests") == requests
        assert {name: database_rows(personal, name) for name in tables} == notifications
    for action, text, revision in [
        ("undo_positions", "撤销上次持仓修改", 2),
        ("redo_positions", "重做", 3),
    ]:
        personal.invoke(
            "owner",
            "chat",
            action,
            {"account": "合成AH", "expected_revision": revision},
            context(action, text),
        )
        assert account(personal)["revision"] == revision + 1
        assert {name: database_rows(personal, name) for name in tables} == notifications
    assert len(database_rows(personal, "position_history_entries")) == 1
    assert len(database_rows(personal, "position_history_requests")) == 3
    assert_current_fact(personal)
