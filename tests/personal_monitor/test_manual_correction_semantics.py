"""SYNTHETIC fixtures: corrections preserve retraction/holding semantics."""

from copy import deepcopy

import pytest

from src.interactive.assistant.manual_positions import (
    extract_manual_rows,
    validate_manual_messages,
    validate_manual_rows,
)

SYNTHETIC_TEXT = (
    "账户合成AH，H股按 2026-01-15 更正为：01816 中广核 3400股、00728 中国电信 5600股、"
    "09999 网易 120股、01339 中国人民保险集团 27800股，之前那组数量作废。"
)
CURRENT = {"01816": 3400, "00728": 5600, "09999": 120, "01339": 27800}

SYNTHETIC_LIST_CORRECTIONS = (
    (
        "仅对验收账户合成验收应用本轮测试更正；原事实时间2026-01-15T11:45:00.123+08:00，"
        "H股数量为：01816 中广核 3400股、00728 中国电信 5600股、09999 网易 120股、"
        "01339 中国人民保险集团 27800股。之前验收基线的这四笔数量作废，"
        "请按本条明确事实直接更正，其他8条保持。"
    ),
    (
        "仅对隔离测试账户合成验收，按本条完整更正四行，包含数量保持不变的行。"
        "数据时间2026-01-15T11:45:00.123+08:00，四行代码及数量为："
        "01816 中广核 3400股、00728 中国电信 5600股、09999 网易 120股、"
        "01339 中国人民保险集团 27800股。请同时更新这四行的事实来源和数据时间，"
        "其余8条保持。确认执行。"
    ),
)


@pytest.mark.parametrize("text", SYNTHETIC_LIST_CORRECTIONS)
def test_synthetic_security_list_quantity_header_preserves_all_four_facts(text):
    extracted = extract_manual_rows(text, [])
    assert {row["instrument"]: row["quantity"] for row in extracted} == CURRENT
    grounded, untouched = validate_manual_messages(
        rows(), [message("actual-list-correction", text)], []
    )
    assert untouched == []
    assert {row["instrument"]: row["quantity"] for row in grounded} == CURRENT
    assert all(
        row["manual_evidence"]["message_ids"] == ["actual-list-correction"]
        for row in grounded
    )


@pytest.mark.parametrize(
    "text,not_a_code",
    [
        ("600036价格为601088，持仓200股", "601088"),
        ("600036市值为601088，持仓200股", "601088"),
        ("600036价格为601088 持仓200股", "601088"),
        ("600036市值为601088 持仓200股", "601088"),
        ("600036数量为01816，可用2000股", "01816"),
        ("600036数量为01816 可用2000股", "01816"),
        ("股票600036数量为01816 中广核2000股", "01816"),
        ("股票价格为601088，持仓200股", "601088"),
        ("股票市值为601088，持仓200股", "601088"),
        ("代码及数量为：01816，可用2000股", "01816"),
        ("代码及数量为：01816 可用2000股", "01816"),
        ("H股数量为：01816、00728 中国电信4000股", "01816"),
        ("H股数量为：01816，00728 中国电信4000股", "01816"),
        ("H股数量为：01816 00728 中国电信4000股", "01816"),
        ("数据时间2026-01-15T11:45:00.123+08:00，数量为01816", "01816"),
        ("数量为01816，持仓2000股", "01816"),
    ],
)
def test_quantity_list_header_cannot_turn_scalar_or_later_quantity_into_code(
    text, not_a_code
):
    try:
        extracted = extract_manual_rows(text, [])
    except ValueError:
        return  # An ambiguous scalar stays fail closed.
    assert not_a_code not in {row["instrument"] for row in extracted}


@pytest.mark.parametrize(
    "text",
    [
        "01816可用2000股",
        "01816可用数量为2000股",
        "01816可用股数：2000份",
        "H股数量为：01816中广核，可用2000股",
        "中广核可用数量2000份",
        "01816可用2000股、00728中国电信4000股",
        "01816可买2000股",
        "01816可卖2000股",
        "01816可买入数量为2000股",
        "01816可卖出股数2000份",
        "01816可交易额度2000份",
        "H股数量为：01816中广核可卖2000股",
        "H股数量为：01816中广核可买2000股",
        "H股数量为：01816中广核买入2000股",
        "H股数量为：01816中广核卖出2000股",
        "H股数量为：01816中广核成交数量2000股",
        "01816可用持仓数量2000",
        "01816可用的持仓数量2000",
        "卖出H股数量为：01816中广核2000股",
        "买入H股数量为：01816中广核2000股",
        "成交H股数量为：01816中广核2000股",
    ],
)
def test_available_only_quantity_is_not_a_holding_even_with_existing_name(text):
    existing = [{"instrument": "01816", "name": "中广核", "quantity": 1000}]
    original = deepcopy(existing)
    with pytest.raises(ValueError):
        validate_manual_rows(rows({"01816": 2000}), text, existing)
    assert existing == original


@pytest.mark.parametrize(
    "text",
    [
        "01816持仓2000股，可用1000股",
        "01816可用1000股，持仓2000股",
        "中广核持仓2000份，可用数量1000份",
        "中广核可用股数为1000股，持仓2000股",
        "H股数量为：01816 中广核2000股，可用1000股",
        "01816买入后当前持有2000股",
        "01816持仓2000股，可卖出1000股",
        "01816可交易额度1000份，当前持有2000份",
        "H股数量为：01816中广核持仓2000股，可买入1000股",
        "01816成交1000股，当前持有2000股",
        "01816可用1000股，当前数量2000股",
    ],
)
def test_holding_quantity_is_preserved_independently_of_available_quantity(text):
    existing = [{"instrument": "01816", "name": "中广核", "quantity": 1000}]
    grounded, untouched = validate_manual_rows(rows({"01816": 2000}), text, existing)
    assert not untouched
    assert grounded[0]["quantity"] == 2000


@pytest.mark.parametrize(
    "text",
    [
        "账户验收，01816可用2000股",
        "账户验收，H股数量为：01816中广核，可用2000股",
        "账户验收，01816可卖出数量2000股",
    ],
)
def test_available_only_manual_tool_cannot_write_positions(tmp_path, text):
    from datetime import datetime

    from src.interactive.assistant.personal_tools import PersonalTools
    from src.personal_monitor import MonitorStore

    tools = PersonalTools(MonitorStore(tmp_path / "available-only.sqlite3"))
    tools.store.ensure_account("owner", "验收")
    tools.store.import_snapshot(
        "owner",
        "验收",
        [{"instrument": "01816", "name": "中广核", "quantity": 1000}],
        "2026-01-15T00:13:00+08:00",
        0,
        source="manual",
    )
    before = tools.store.status("owner", "验收")
    with pytest.raises(ValueError):
        tools.invoke(
            "owner",
            "chat",
            "import_manual",
            {
                "account": "验收",
                "expected_revision": 1,
                "rows": rows({"01816": 2000}),
            },
            {
                "id": "available-only",
                "message_id": "available-only",
                "original_text": text,
                "effective_time": datetime.fromisoformat(
                    "2026-01-15T11:45:00.123+08:00"
                ),
            },
        )
    assert tools.store.status("owner", "验收") == before


def test_list_holding_amount_cannot_become_another_security_code():
    extracted = extract_manual_rows(
        "股票持仓为：601088持仓100000股、00728中国电信300股", []
    )
    assert extracted == [
        {"instrument": "601088", "quantity": 100000},
        {"instrument": "00728", "quantity": 300},
    ]


@pytest.mark.parametrize(
    "modifier",
    [
        "可用持仓数量",
        "可用的持仓数量",
        "可卖出的持仓份额",
        "可买入的持仓股数",
        "可用的股票持仓数量",
        "可卖持有份额",
    ],
)
@pytest.mark.parametrize("named", [False, True])
def test_compound_capacity_field_cannot_reclassify_itself_as_holdings(modifier, named):
    existing = (
        [{"instrument": "01816", "name": "中广核", "quantity": 1000}] if named else []
    )
    text = f"H股数量为：01816中广核{modifier}2000股"
    with pytest.raises(ValueError):
        extract_manual_rows(text, existing)


def test_later_explicit_holding_field_after_compound_capacity_is_independent():
    grounded, untouched = validate_manual_rows(
        rows({"01816": 2000}),
        "01816可用持仓数量1000股，当前持有2000股",
        [],
    )
    assert not untouched and grounded[0]["quantity"] == 2000


@pytest.mark.parametrize(
    "text",
    [
        "00728 中国电信 买入后当前持有2000",
        "00728 中国电信 买入后总持仓2000",
        "00728 中国电信 成交后当前持有2000",
        "00728 可用1000股当前持有2000",
    ],
)
def test_final_holding_label_is_visible_when_quantity_has_no_unit(text):
    grounded, untouched = validate_manual_rows(rows({"00728": 2000}), text, [])
    assert not untouched and grounded[0]["quantity"] == 2000


def message(identity, text, second=0):
    return {
        "id": identity,
        "text": text,
        "effective_time": f"2026-01-15T12:00:{second:02d}+08:00",
    }


def rows(values=CURRENT):
    return [
        {"instrument": code, "quantity": quantity} for code, quantity in values.items()
    ]


def test_complete_correction_sentence_is_accepted_with_only_current_message_provenance():
    source = message("om_human_correction", SYNTHETIC_TEXT)
    existing = [dict(row, currency="HKD", name="保留原名称") for row in rows()]
    original = deepcopy(existing)
    result, untouched = validate_manual_messages(rows(), [source], existing)
    assert {row["instrument"]: row["quantity"] for row in result} == CURRENT
    assert untouched == [] and existing == original
    assert all(
        row["manual_evidence"]["message_ids"] == ["om_human_correction"]
        for row in result
    )
    assert all(
        row["currency"] == "HKD" and row["name"] == "保留原名称" for row in result
    )


@pytest.mark.parametrize(
    "text",
    [
        "之前那组数量作废，01816当前2000股",
        "01816当前2000股，之前那组数量作废",
        "01816当前2000股，之前说错了",
        "之前数量错误；01816更正为2000股",
        "之前01816持仓1000股是错误的，现在01816持仓2000股",
        "01816之前1000股，现在2000股",
        "01816不是1000股而是2000股",
        "01816从1000股改为2000股",
        "01816从1000股更正为2000股，旧数量取消",
        "之前数量错了现在更正为01816当前2000股",
        "01816之前1000股作废现在2000股",
    ],
)
def test_explicit_current_fact_is_not_tainted_by_retracted_old_fact(text):
    result, untouched = validate_manual_rows(rows({"01816": 2000}), text, [])
    assert result == rows({"01816": 2000}) and untouched == []
    result, untouched = validate_manual_messages(
        rows({"01816": 2000}), [message("new", text)], []
    )
    assert result[0]["quantity"] == 2000 and untouched == []


@pytest.mark.parametrize(
    "text",
    [
        "01816当前2000股作废",
        "01816当前2000股，取消这个数",
        "01816当前2000股，以上是错误的",
        "撤销01816当前2000股",
        "01816不是2000股",
        "01816从1000股改为2000股是假设",
        "如果01816不是1000股而是2000股就提醒",
        "不要把01816从1000股改为2000股",
        "01816不是1000股而是2000股吗？",
        "01816之前2000股",
        "如果之前数量错了，01816当前2000股",
        "如果01816以前1000股不对，现在01816当前2000股",
        "01816当前2000股，不过2000股不是实际数量",
        "01816当前2000股，之前那组作废，但2000股不是当前持仓",
        "01816当前2000股是否正确",
        "01816当前2000股，是不是这样",
        "01816当前2000股对吗",
    ],
)
def test_withdrawn_conditional_or_old_only_numbers_are_not_current_facts(text):
    with pytest.raises(ValueError):
        validate_manual_rows(rows({"01816": 2000}), text, [])


@pytest.mark.parametrize(
    "newer",
    [
        "01816不是2000股",
        "撤销01816当前2000股",
        "01816当前2000股作废",
        "之前那组数量作废",
        "不是这组数量",
        "01816可能2000股",
        "如果01816当前2000股",
        "01816当前2000股吗？",
    ],
)
def test_new_rejection_or_uncertainty_cannot_fall_back_to_old_matching_fact(newer):
    with pytest.raises(ValueError):
        validate_manual_messages(
            rows({"01816": 2000}),
            [message("old", "01816当前2000股"), message("new", newer, 1)],
            [],
        )


def test_model_old_quantity_cannot_override_explicit_current_replacement():
    with pytest.raises(ValueError, match="不一致"):
        validate_manual_messages(
            rows({"01816": 1000}),
            [
                message("old", "01816当前1000股"),
                message("new", "01816不是1000股而是2000股", 1),
            ],
            [],
        )


def test_retraction_of_last_item_does_not_borrow_other_new_quantity():
    with pytest.raises(ValueError):
        validate_manual_messages(
            rows({"01816": 2000, "00728": 4000}),
            [message("new", "01816当前2000股，00728当前4000股作废")],
            [],
        )


def test_new_complete_correction_wins_over_selected_old_matching_groups():
    older = "01816当前3400股，00728当前2300股，09999当前120股，01339当前26900股"
    result, untouched = validate_manual_messages(
        rows(), [message("old", older), message("new", SYNTHETIC_TEXT, 1)], []
    )
    assert {
        row["instrument"]: row["quantity"] for row in result
    } == CURRENT and untouched == []
    assert all(row["manual_evidence"]["message_ids"] == ["new"] for row in result)


def test_extract_complete_correction_is_read_only_and_contains_only_typed_rows():
    existing = [dict(row, name="旧名称", currency="HKD", price=123) for row in rows()]
    original = deepcopy(existing)
    assert extract_manual_rows(SYNTHETIC_TEXT, existing) == rows()
    assert existing == original


@pytest.mark.parametrize(
    "text",
    [
        "01816当前2000股，00728代码先给你",
        "01816当前2000股，00728不是4000股",
        "01816当前2000股，00728当前4000股作废",
        "01816当前2000股，00728可能4000股",
        "01816当前2000股，00728当前4000股吗？",
        "01816当前2000股，01816当前3000股",
        "如果01816当前2000股，就记录",
        "01816当前2000股作废",
        "01816之前2000股",
        "2026-01-15 12:16:34，市值200000元，价格5.5元",
        "2000股",
        "之前那组数量作废",
        "",
    ],
)
def test_extract_refuses_partial_negative_conditional_ambiguous_or_no_facts(text):
    with pytest.raises(ValueError):
        extract_manual_rows(text, [])


def test_extract_refuses_ambiguous_existing_display_name():
    with pytest.raises(ValueError, match="证券身份"):
        extract_manual_rows(
            "同名当前2000股",
            [
                {"instrument": "01816", "name": "同名", "quantity": 10},
                {"instrument": "00728", "name": "同名", "quantity": 20},
            ],
        )


def test_extract_unique_existing_name_and_replaced_number_preserve_numeric_scale():
    assert extract_manual_rows(
        "中广核不是1000股而是2千股",
        [{"instrument": "01816", "name": "中广核", "quantity": 1}],
    ) == rows({"01816": 2000})


def test_extract_dates_and_quantity_digits_are_not_securities_or_metadata():
    assert extract_manual_rows(
        "账户合成AH，按2099-01-01更正为01816持仓200,000股，价格3.5港元",
        [],
    ) == rows({"01816": 200000})


def test_extract_is_bounded_to_two_hundred_securities():
    text = "；".join(f"{600000 + index}当前1股" for index in range(201))
    with pytest.raises(ValueError, match="1至200"):
        extract_manual_rows(text, [])
