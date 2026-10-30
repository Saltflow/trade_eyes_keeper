"""SYNTHETIC fixtures: authenticated sources and screenshot time/revision replay.

All observations are fixed fixtures with the real local exchange calendar. No
model, broker, messaging platform or production state is contacted.
"""

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

from src.interactive.assistant.personal_tools import PersonalTools
from src.personal_monitor import MonitorStore
from src.personal_monitor_runtime import exchange_state

TZ = ZoneInfo("Asia/Shanghai")
OWNER, CHAT, ACCOUNT = "owner", "chat", "合成AH"
QUANTITY_TIME = datetime(2026, 1, 18, 8, 54, 18, 932000, tzinfo=TZ)
CODE_TIME = datetime(2026, 1, 18, 8, 54, 43, 621000, tzinfo=TZ)
QUANTITY_TEXT = (
    "确认将 A 股那 10 只按 2026-01-18 写入合成AH， 然后H股分别是中广核3400股，"
    "电信3400股， 网易120股， PICC 27800股"
)
CODE_TEXT = "代码分别是01816，00728， 09999， 01339"
EXPECTED = {"01816": 3400, "00728": 3400, "09999": 120, "01339": 27800}


def calendar_quote(code, as_of):
    moment = datetime.fromisoformat(as_of)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    market = exchange_state("XSHG", moment)
    return {
        "price": 40,
        "currency": "CNY",
        "provider_time": (
            moment if market["market_open"] else market["session_close"]
        ).isoformat(),
        "source": "controlled_calendar_quote",
        "market_timezone": "Asia/Shanghai",
        "market_closed": not market["market_open"],
        "latest_trading_date": market["latest_completed_session"],
    }


@pytest.fixture
def personal(tmp_path):
    store = MonitorStore(tmp_path / "messages.sqlite")
    store.ensure_account(OWNER, ACCOUNT)
    return PersonalTools(store, quote_loader=calendar_quote)


def remember_pair(
    personal,
    *,
    owner=OWNER,
    chat=CHAT,
    quantity_text=QUANTITY_TEXT,
    code_text=CODE_TEXT,
    quantity_time=QUANTITY_TIME,
    code_time=CODE_TIME,
):
    personal.remember_user_message(
        owner, chat, "quantity", quantity_text, quantity_time
    )
    personal.remember_user_message(owner, chat, "codes", code_text, code_time)


def candidates():
    # Deliberately reversed model order and fabricated fields have no authority.
    return [
        {
            "instrument": code,
            "quantity": quantity,
            "name": "模型补出的名字",
            "currency": "USD",
            "price": 999,
            "market_value": 1,
        }
        for code, quantity in reversed(list(EXPECTED.items()))
    ]


def manual_import(
    personal,
    *,
    owner=OWNER,
    chat=CHAT,
    account=ACCOUNT,
    revision=0,
    source_ids=None,
    now=CODE_TIME,
):
    return personal.invoke(
        owner,
        chat,
        "import_manual",
        {
            "account": account,
            "as_of": now.isoformat(),
            "expected_revision": revision,
            "rows": candidates(),
            "source_message_ids": source_ids or ["quantity", "codes"],
        },
        {
            "id": "import-turn",
            "message_id": "import-message",
            "original_text": CODE_TEXT,
            "effective_time": now,
        },
    )


@pytest.mark.parametrize("foreign", ["owner", "chat"])
def test_source_ids_cannot_cross_authenticated_owner_or_chat(personal, foreign):
    remember_pair(
        personal,
        owner="someone-else" if foreign == "owner" else OWNER,
        chat="other-chat" if foreign == "chat" else CHAT,
    )
    with pytest.raises(ValueError, match="当前用户本会话最近的认证消息"):
        manual_import(personal)
    assert personal.store.status(OWNER)["accounts"][0]["positions"] == []


def test_source_messages_expire_after_fifteen_minutes(personal):
    remember_pair(personal, quantity_time=CODE_TIME - timedelta(seconds=901))
    with pytest.raises(ValueError, match="15分钟窗口"):
        manual_import(personal)


def test_only_recent_ten_auth_messages_are_exposed_and_can_be_referenced(personal):
    remember_pair(personal)
    for index in range(10):
        personal.remember_user_message(
            OWNER,
            CHAT,
            f"filler-{index}",
            "无持仓事实",
            CODE_TIME + timedelta(seconds=index + 1),
        )
    status = personal.invoke(OWNER, CHAT, "status", {}, {"id": "status"})
    identities = [item["id"] for item in status["authenticated_messages"]]
    assert len(identities) == 10
    assert "quantity" not in identities and "codes" not in identities
    with pytest.raises(ValueError, match="当前用户本会话最近的认证消息"):
        manual_import(personal, now=CODE_TIME + timedelta(seconds=15))


@pytest.mark.parametrize("mode", ["wrong_target", "two_accounts", "missing_account"])
def test_referenced_facts_need_one_explicit_target_account(personal, mode):
    personal.store.ensure_account(OWNER, "其他账户")
    quantity_text = (
        QUANTITY_TEXT.replace(ACCOUNT, "账户未指定")
        if mode == "missing_account"
        else QUANTITY_TEXT
    )
    code_text = CODE_TEXT + "；其他账户" if mode == "two_accounts" else CODE_TEXT
    remember_pair(personal, quantity_text=quantity_text, code_text=code_text)
    with pytest.raises(ValueError, match="唯一目标账户"):
        manual_import(
            personal, account="其他账户" if mode == "wrong_target" else ACCOUNT
        )


@pytest.mark.parametrize(
    "source_ids", [["quantity", "quantity"], ["quantity", "codes", "x", "y"]]
)
def test_duplicate_or_unbounded_source_ids_are_rejected(personal, source_ids):
    remember_pair(personal)
    with pytest.raises(ValueError, match="1至3条"):
        manual_import(personal, source_ids=source_ids)


def test_real_two_messages_ground_all_four_hk_positions_without_model_metadata(
    personal,
):
    remember_pair(personal)
    result = manual_import(personal)
    assert set(result["result"]["applied"]) == set(EXPECTED)
    positions = personal.store.status(OWNER)["accounts"][0]["positions"]
    assert {row["instrument"]: row["quantity"] for row in positions} == EXPECTED
    for row in positions:
        assert not {"name", "currency", "price", "market_value"} & row.keys()
        evidence = row["manual_evidence"]
        assert evidence["message_ids"] == ["quantity", "codes"]
        assert evidence["quantity_effective_time"] == QUANTITY_TIME.isoformat()
        assert datetime.fromisoformat(row["as_of"]) == QUANTITY_TIME


def test_real_ordered_map_keeps_each_existing_securitys_metadata_and_does_not_stop_at_netease(
    personal,
):
    prior = [
        {
            "instrument": "01816",
            "quantity": 1,
            "name": "中广核电力",
            "currency": "HKD",
            "price": 3.275,
            "market_value": 5572.74,
        },
        {
            "instrument": "00728",
            "quantity": 1,
            "name": "中国电信",
            "currency": "HKD",
            "price": 4.315,
            "market_value": 14684.81,
        },
        {
            "instrument": "09999",
            "quantity": 1,
            "name": "网易",
            "currency": "HKD",
            "price": 183,
            "market_value": 15569.64,
        },
        {
            "instrument": "01339",
            "quantity": 1,
            "name": "中国人民保险集团",
            "currency": "HKD",
            "price": 4.915,
            "market_value": 83633.64,
        },
    ]
    personal.store.import_snapshot(
        OWNER,
        ACCOUNT,
        prior,
        (QUANTITY_TIME - timedelta(minutes=1)).isoformat(),
        0,
        "manual",
    )
    remember_pair(personal)
    result = manual_import(personal, revision=1)
    assert set(result["result"]["applied"]) == set(EXPECTED)
    actual = {
        row["instrument"]: row
        for row in personal.store.status(OWNER)["accounts"][0]["positions"]
    }
    for source in prior:
        row = actual[source["instrument"]]
        assert row["quantity"] == EXPECTED[source["instrument"]]
        assert {
            key: row[key] for key in ("name", "currency", "price", "market_value")
        } == {key: source[key] for key in ("name", "currency", "price", "market_value")}


@pytest.mark.parametrize(
    "code_text",
    [
        "01816，00728，09999，01339",
        "代码分别是01816，00728，09999",
        "代码分别是01816，01816，09999，01339",
        "代码可能分别是01816，00728，09999，01339",
    ],
)
def test_cross_message_mapping_needs_explicit_unique_equal_length_order(
    personal, code_text
):
    remember_pair(personal, code_text=code_text)
    with pytest.raises(ValueError, match="分别映射|不能回退旧数量"):
        manual_import(personal)
    assert personal.store.status(OWNER)["accounts"][0]["positions"] == []


def test_ordered_message_pair_cannot_cross_beijing_calendar_days(personal):
    quantity_time = datetime(2026, 1, 17, 23, 59, tzinfo=TZ)
    code_time = datetime(2026, 1, 18, 0, 1, tzinfo=TZ)
    remember_pair(personal, quantity_time=quantity_time, code_time=code_time)
    with pytest.raises(ValueError, match="分别映射"):
        manual_import(personal, now=code_time)


def test_code_clarification_cannot_refresh_old_quantity_over_newer_manual_update(
    personal,
):
    remember_pair(personal)
    revised_at = QUANTITY_TIME + timedelta(seconds=10)
    personal.store.import_snapshot(
        OWNER,
        ACCOUNT,
        [{"instrument": "09999", "quantity": 777}],
        revised_at.isoformat(),
        0,
        "manual",
    )
    result = manual_import(personal, revision=1)
    assert result["result"]["applied"] == []
    assert {row["reason"] for row in result["result"]["rejected"]} == {
        "older_account_snapshot"
    }
    account = personal.store.status(OWNER)["accounts"][0]
    assert account["revision"] == 1
    assert len(account["positions"]) == 1
    assert account["positions"][0]["quantity"] == 777
    assert datetime.fromisoformat(account["positions"][0]["as_of"]) == revised_at


def save_unit_batch(personal):
    stamp = datetime(2026, 1, 18, 0, 14, 38, 754000, tzinfo=TZ)
    identity = hashlib.sha256(b"controlled-unit-screenshot").hexdigest()
    batch = {
        "batch_id": identity,
        "content_sha256": identity,
        "revisions": {ACCOUNT: 0},
        "document": {
            "account_hint": ACCOUNT,
            "as_of": stamp.isoformat(),
            "positions": [
                {
                    "code": "601088",
                    "name": "中国神华",
                    "asset_type": "equity",
                    "quantity": 200,
                    "quantity_unit": None,
                    "price": 40,
                    "market_value": 8000,
                    "currency": "CNY",
                    "uncertain_fields": ["quantity_unit"],
                    "ambiguities": ["quantity_unit:未标明单位"],
                }
            ],
        },
    }
    with personal.store.connection(write=True) as db:
        db.execute(
            "INSERT INTO personal_image_batches VALUES(?,?,?,?,?,?)",
            (OWNER, CHAT, identity, json.dumps(batch), "pending", stamp.isoformat()),
        )
    first = personal.invoke(
        OWNER,
        CHAT,
        "import_image",
        {"account": ACCOUNT, "batch_id": identity},
        {
            "id": "first",
            "original_text": "这些是合成AH当前持仓",
            "effective_time": stamp,
        },
    )
    assert first["result"]["revision"] == 1
    assert first["result"]["pending_issues"] == ["601088"]
    return identity, stamp


def confirm_units(personal, identity, stamp, *, as_of=None):
    return personal.invoke(
        OWNER,
        CHAT,
        "import_image",
        {
            "account": ACCOUNT,
            "batch_id": identity,
            "as_of": as_of or stamp.date().isoformat(),
            "confirm_unscaled_quantity_units": True,
        },
        {
            "id": "confirm",
            "message_id": "unit-message",
            "original_text": "确认合成AH数量都是股/份，没有万倍",
            "effective_time": CODE_TIME,
        },
    )


def test_same_batch_date_only_unit_confirmation_reuses_accepted_pending_time(personal):
    identity, stamp = save_unit_batch(personal)
    result = confirm_units(personal, identity, stamp)
    assert result["result"]["applied"] == ["601088"]
    account = personal.store.status(OWNER)["accounts"][0]
    assert account["revision"] == 2
    assert account["positions"][0]["quantity"] == 200
    assert datetime.fromisoformat(account["positions"][0]["as_of"]) == stamp
    assert account["pending_issues"] == []
    saved = personal.image_batch(OWNER, CHAT, identity)
    assert datetime.fromisoformat(saved["accepted_as_of"]) == stamp


def test_same_batch_date_only_retry_does_not_override_external_manual_revision(
    personal,
):
    identity, stamp = save_unit_batch(personal)
    personal.store.import_snapshot(
        OWNER,
        ACCOUNT,
        [{"instrument": "601088", "quantity": 333}],
        (stamp + timedelta(seconds=1)).isoformat(),
        1,
        "manual",
    )
    with pytest.raises(ValueError, match="account_revision_changed"):
        confirm_units(personal, identity, stamp)
    assert (
        personal.store.status(OWNER)["accounts"][0]["positions"][0]["quantity"] == 333
    )


def test_explicit_earlier_time_is_not_promoted_to_prior_accepted_snapshot(personal):
    identity, stamp = save_unit_batch(personal)
    result = confirm_units(
        personal, identity, stamp, as_of=(stamp - timedelta(minutes=1)).isoformat()
    )
    assert result["result"]["applied"] == []
    assert {item["reason"] for item in result["result"]["rejected"]} == {
        "older_account_snapshot"
    }
    account = personal.store.status(OWNER)["accounts"][0]
    assert account["revision"] == 1 and account["positions"] == []
    saved = personal.image_batch(OWNER, CHAT, identity)
    assert datetime.fromisoformat(saved["accepted_as_of"]) == stamp


def test_message_identity_cannot_be_rewritten_after_authentication(personal):
    remember_pair(personal)
    personal.remember_user_message(OWNER, CHAT, "quantity", "修改成999999股", CODE_TIME)
    source = next(
        item for item in personal.user_messages(OWNER, CHAT) if item["id"] == "quantity"
    )
    assert source["text"] == QUANTITY_TEXT
    assert source["effective_time"] == QUANTITY_TIME.isoformat()


def test_same_authenticated_sources_are_idempotent_across_tool_turns(personal):
    remember_pair(personal)
    first = manual_import(personal)
    assert first["result"]["revision"] == 1
    second = manual_import(personal, revision=1)
    assert second["result"]["duplicate"] is True
    assert second["result"]["applied"] == []
    assert second["result"]["revision"] == 1
    account = personal.store.status(OWNER)["accounts"][0]
    assert account["revision"] == 1
    assert {
        row["instrument"]: row["quantity"] for row in account["positions"]
    } == EXPECTED


def test_new_direct_manual_fact_removes_only_its_obsolete_message_provenance(personal):
    remember_pair(personal)
    manual_import(personal)
    updated = CODE_TIME + timedelta(seconds=1)
    result = personal.invoke(
        OWNER,
        CHAT,
        "import_manual",
        {
            "account": ACCOUNT,
            "as_of": updated.isoformat(),
            "expected_revision": 1,
            "rows": [{"instrument": "09999", "quantity": 101}],
        },
        {
            "id": "new-direct",
            "message_id": "new-direct-message",
            "original_text": "合成AH的09999当前持仓101股",
            "effective_time": updated,
        },
    )
    assert result["result"]["applied"] == ["09999"]
    positions = {
        row["instrument"]: row
        for row in personal.store.status(OWNER)["accounts"][0]["positions"]
    }
    assert positions["09999"]["quantity"] == 101
    assert "manual_evidence" not in positions["09999"]
    assert datetime.fromisoformat(positions["09999"]["as_of"]) == updated
    for code in EXPECTED.keys() - {"09999"}:
        assert positions[code]["manual_evidence"]["quantity_message_id"] == "quantity"
