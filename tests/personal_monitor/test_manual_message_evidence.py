"""Ground cross-message holdings only in explicit authenticated ordered lists."""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from src.interactive.assistant.manual_positions import validate_manual_messages

NOW = datetime.fromisoformat("2026-10-25T08:54:18.932+08:00")
QUANTITIES = "确认将A股按2026-10-25写入账户，H股分别是中广核2000股，电信2000股，网易100股，PICC 19000股"
CODES = "代码分别是01816，00728，09999，01339"
EXPECTED = {"01816": 2000, "00728": 2000, "09999": 100, "01339": 19000}


def message(identity, text, seconds=0, **extra):
    return {
        "id": identity,
        "text": text,
        "effective_time": (NOW + timedelta(seconds=seconds)).isoformat(),
        **extra,
    }


def rows(**metadata):
    return [
        dict(instrument=code, quantity=quantity, **metadata)
        for code, quantity in EXPECTED.items()
    ]


def test_real_named_quantities_then_explicit_codes_keep_original_quantity_time():
    quantities = message("om_quantity", QUANTITIES)
    codes = message("om_codes", CODES, 30)
    original = deepcopy([quantities, codes])
    grounded, untouched = validate_manual_messages(rows(), [quantities, codes], [])
    assert {row["instrument"]: row["quantity"] for row in grounded} == EXPECTED
    assert untouched == []
    for row in grounded:
        assert row["manual_evidence"] == {
            "message_ids": ["om_quantity", "om_codes"],
            "quantity_message_id": "om_quantity",
            "quantity_effective_time": quantities["effective_time"],
            "code_message_id": "om_codes",
            "code_effective_time": codes["effective_time"],
        }
    assert original == [quantities, codes]


def test_tool_row_order_does_not_change_explicit_source_order_mapping():
    reordered = list(reversed(rows(name="forged", price=0, currency="USD")))
    original = deepcopy(reordered)
    grounded, untouched = validate_manual_messages(
        reordered,
        [message("codes", CODES, 1), message("quantities", QUANTITIES)],
        [],
    )
    assert {row["instrument"]: row["quantity"] for row in grounded} == EXPECTED
    assert [row["instrument"] for row in grounded] == list(reversed(EXPECTED))
    assert all(set(row) == {"instrument", "quantity", "manual_evidence"} for row in grounded)
    assert untouched == []
    assert reordered == original


@pytest.mark.parametrize(
    "quantity_text,code_text",
    [
        (QUANTITIES, "代码是01816，00728，09999，01339"),
        (QUANTITIES.replace("分别是", "是"), CODES),
        (QUANTITIES, "代码分别是01816，00728，09999"),
        (QUANTITIES, "代码分别是01816，00728，09999，01339，00700"),
        (QUANTITIES, "代码分别是01816，00728，09999，09999"),
        (QUANTITIES, "代码分别是01816，00728，09999，1339"),
        (QUANTITIES, "代码分别是01816，00728，09999，01339，"),
        (QUANTITIES.replace("中广核2000股", "2000股"), CODES),
        (QUANTITIES.replace("电信2000股", "中广核2000股"), CODES),
        (QUANTITIES.replace("中广核2000股", "中广核-2000股"), CODES),
        (QUANTITIES.replace("中广核2000股", "中广核+2000股"), CODES),
        (QUANTITIES.replace("中广核2000股", "中广核2e3股"), CODES),
        (QUANTITIES.replace("中广核2000股", "中广核2000手"), CODES),
        (QUANTITIES + "，还有其他持仓", CODES),
    ],
)
def test_missing_order_or_ambiguous_items_fail_closed(quantity_text, code_text):
    with pytest.raises(ValueError):
        validate_manual_messages(
            rows(),
            [message("quantity", quantity_text), message("codes", code_text, 1)],
            [],
        )


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("如果", ""),
        ("假设", ""),
        ("计划", ""),
        ("不要", ""),
        ("", "，这些是错的"),
        ("撤销：", ""),
        ("", "，取消上述持仓"),
        ("", "吗？"),
    ],
)
def test_conditional_negative_revoked_and_questioned_lists_are_not_facts(prefix, suffix):
    with pytest.raises(ValueError):
        validate_manual_messages(
            rows(),
            [
                message("quantity", prefix + QUANTITIES + suffix),
                message("codes", CODES, 1),
            ],
            [],
        )


@pytest.mark.parametrize("seconds", [-1, 901, 86400])
def test_codes_cannot_rejuvenate_old_or_future_quantity_sources(seconds):
    with pytest.raises(ValueError):
        validate_manual_messages(
            rows(),
            [message("quantity", QUANTITIES), message("codes", CODES, seconds)],
            [],
        )


def test_cross_beijing_day_fails_even_within_15_minutes():
    before_midnight = "2026-10-25T23:59:30+08:00"
    after_midnight = "2026-10-26T00:00:01+08:00"
    with pytest.raises(ValueError):
        validate_manual_messages(
            rows(),
            [
                message("quantity", QUANTITIES, effective_time=before_midnight),
                message("codes", CODES, effective_time=after_midnight),
            ],
            [],
        )


@pytest.mark.parametrize("count", [1, 20])
def test_ordered_lists_are_generic_and_bounded(count):
    names = ["甲" * index + "公司" for index in range(count)]
    quantity_text = "港股分别是" + "，".join(
        f"{name}{index + 1}股" for index, name in enumerate(names)
    )
    codes = [str(index + 1).zfill(5) for index in range(count)]
    code_text = "代码分别是" + "，".join(codes)
    candidates = [{"code": code, "quantity": index + 1} for index, code in enumerate(codes)]
    grounded, untouched = validate_manual_messages(
        candidates,
        [message("quantity", quantity_text), message("codes", code_text, 900)],
        [],
    )
    assert len(grounded) == count
    assert untouched == []


def test_more_than_20_ordered_items_are_not_accepted():
    codes = [str(index + 1).zfill(5) for index in range(21)]
    candidates = [{"code": code, "quantity": 1} for code in codes]
    with pytest.raises(ValueError):
        validate_manual_messages(
            candidates,
            [
                message("quantity", "港股分别是" + "，".join(f"公司{'甲' * i}1股" for i in range(21))),
                message("codes", "代码分别是" + "，".join(codes), 1),
            ],
            [],
        )


def test_direct_assertion_uses_only_its_own_source_and_drops_model_metadata():
    current = message("direct", "01816当前2000股", 20)
    grounded, untouched = validate_manual_messages(
        [{"code": "01816", "quantity": 2000, "name": "forged", "price": 999, "currency": "USD"}],
        [message("older", "无关问题"), current],
        [],
    )
    assert set(grounded[0]) == {"instrument", "quantity", "manual_evidence"}
    assert grounded[0]["manual_evidence"]["message_ids"] == ["direct"]
    assert grounded[0]["manual_evidence"]["quantity_effective_time"] == current["effective_time"]
    assert untouched == []


def test_newer_direct_quantity_conflict_cannot_fall_back_to_matching_older_fact():
    with pytest.raises(ValueError, match="不一致"):
        validate_manual_messages(
            [{"code": "01816", "quantity": 2000}],
            [
                message("older", "01816当前2000股"),
                message("newer", "01816当前3000股", 1),
            ],
            [],
        )


def test_existing_metadata_survives_but_model_fields_never_replace_it():
    existing = [{"instrument": "01816", "quantity": 1, "currency": "HKD", "name": "stored"}]
    original = deepcopy(existing)
    grounded, _ = validate_manual_messages(
        [{"code": "01816", "quantity": 2000, "name": "forged", "currency": "USD", "market_value": 1}],
        [message("quantity", QUANTITIES), message("codes", CODES, 1)],
        existing,
    )
    assert grounded[0]["name"] == "stored"
    assert grounded[0]["currency"] == "HKD"
    assert "market_value" not in grounded[0]
    assert existing == original


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [message("codes", CODES)],
        [message("same", QUANTITIES), message("same", CODES, 1)],
        [message("quantity", QUANTITIES, role="assistant"), message("codes", CODES, 1)],
        [message("quantity", QUANTITIES, effective_time="2026-10-25T08:00:00"), message("codes", CODES, 1)],
        [message("quantity", QUANTITIES), message("codes", CODES, effective_time=None)],
    ],
)
def test_missing_unusable_duplicate_or_assistant_sources_fail_closed(messages):
    with pytest.raises(ValueError):
        validate_manual_messages(rows(), messages, [])


def test_multiple_plausible_quantity_lists_need_explicit_disambiguation():
    with pytest.raises(ValueError, match="唯一"):
        validate_manual_messages(
            rows(),
            [
                message("quantity1", QUANTITIES),
                message("quantity2", QUANTITIES, 1),
                message("codes", CODES, 2),
            ],
            [],
        )


def test_tool_quantity_cannot_override_explicit_order_mapping():
    candidates = rows()
    candidates[0]["quantity"] = 19000
    with pytest.raises(ValueError, match="不一致"):
        validate_manual_messages(
            candidates,
            [message("quantity", QUANTITIES), message("codes", CODES, 1)],
            [],
        )


@pytest.mark.parametrize("typed_quantity", ["2,000", "2千", "0.2万"])
def test_explicit_quantity_format_is_scaled_only_from_authenticated_text(typed_quantity):
    grounded, untouched = validate_manual_messages(
        [{"code": "01816", "quantity": 2000}],
        [
            message("quantity", "港股分别是中广核" + typed_quantity + "股"),
            message("codes", "代码分别是01816", 1),
        ],
        [],
    )
    assert grounded[0]["quantity"] == 2000
    assert untouched == []


def test_unselected_tool_rows_remain_untouched():
    grounded, untouched = validate_manual_messages(
        rows() + [{"code": "00700", "quantity": 999, "price": 1}],
        [message("quantity", QUANTITIES), message("codes", CODES, 1)],
        [],
    )
    assert {row["instrument"] for row in grounded} == set(EXPECTED)
    assert untouched == ["00700"]


def test_source_times_are_compared_in_beijing_even_when_offsets_differ():
    grounded, _ = validate_manual_messages(
        rows(),
        [
            message("quantity", QUANTITIES, effective_time="2026-10-25T00:54:18Z"),
            message("codes", CODES, effective_time="2026-10-25T08:54:19+08:00"),
        ],
        [],
    )
    assert grounded[0]["manual_evidence"]["quantity_effective_time"] == "2026-10-25T08:54:18+08:00"


@pytest.mark.parametrize("extra_tool_row", [False, True])
def test_existing_one_named_security_does_not_short_circuit_ordered_mapping(extra_tool_row):
    candidates = rows()
    if extra_tool_row:
        candidates.append({"code": "00700", "quantity": 999})
    existing = [{"instrument": "09999", "quantity": 1, "name": "网易"}]
    grounded, untouched = validate_manual_messages(
        candidates,
        [message("quantity", QUANTITIES), message("codes", CODES, 1)],
        existing,
    )
    assert {row["instrument"]: row["quantity"] for row in grounded} == EXPECTED
    assert untouched == (["00700"] if extra_tool_row else [])
    assert all(row["manual_evidence"]["message_ids"] == ["quantity", "codes"] for row in grounded)


def test_partial_direct_assertions_without_ordered_mapping_do_not_silently_drop_rows():
    with pytest.raises(ValueError, match="分别映射"):
        validate_manual_messages(
            [{"code": "01816", "quantity": 2000}, {"code": "00728", "quantity": 2000}],
            [message("direct", "01816当前2000股")],
            [],
        )


def test_newer_partial_quantity_correction_conflicts_with_older_ordered_evidence():
    candidates = rows()
    candidates[2]["quantity"] = 200
    with pytest.raises(ValueError, match="不一致"):
        validate_manual_messages(
            candidates,
            [
                message("quantity", QUANTITIES),
                message("codes", CODES, 1),
                message("correction", "09999当前200股", 2),
            ],
            [],
        )


def test_newer_partial_fact_cannot_hide_model_using_old_quantity():
    with pytest.raises(ValueError, match="不一致"):
        validate_manual_messages(
            rows(),
            [
                message("quantity", QUANTITIES),
                message("codes", CODES, 1),
                message("correction", "09999当前200股", 2),
            ],
            [],
        )
