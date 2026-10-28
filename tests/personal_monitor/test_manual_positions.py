"""Typed facts, entity scope and metadata preservation for all manual imports."""

from copy import deepcopy

import pytest

from src.interactive.assistant.manual_positions import validate_manual_rows


def row(code="601088", quantity=700, **metadata):
    return {"instrument": code, "quantity": quantity, **metadata}


@pytest.mark.parametrize(
    "text,quantity",
    [
        ("601088现在持有700股", 700),
        ("601088：700股", 700),
        ("601088，数量700", 700),
        ("601088 持仓=700", 700),
        ("601088数量1,000股", 1000),
        ("601088持有1.5万股", 15000),
        ("601088 2千股", 2000),
        ("601088 数量0", 0),
        ("601088持有0股", 0),
        ("601088已经清仓", 0),
        ("601088清仓了", 0),
        ("601088持仓已清零", 0),
        ("601088已全部卖出", 0),
        ("VOO持有1.25份", 1.25),
        ("QQQ 2 shares", 2),
        ("00700.HK数量500股", 500),
    ],
)
def test_explicit_quantities_and_completed_zero_facts(text, quantity):
    code = (
        "VOO"
        if "VOO" in text
        else "QQQ"
        if "QQQ" in text
        else "00700"
        if "00700" in text
        else "601088"
    )
    grounded, untouched = validate_manual_rows([row(code, quantity)], text, [])
    assert grounded == [row(code, quantity)]
    assert untouched == []


@pytest.mark.parametrize(
    "text,quantity",
    [
        ("帮我看看601088", 0),
        ("帮我看看601088", 1),
        ("帮我看看601088", 601088),
        ("601088", 0),
        ("601088涨了700元", 700),
        ("601088价格700元", 700),
        ("601088清仓", 0),
        ("601088不要清仓", 0),
        ("601088还没清仓", 0),
        ("601088并未清仓", 0),
        ("601088不应该清仓", 0),
        ("如果601088已清仓", 0),
        ("601088如果已清仓就告诉我", 0),
        ("601088计划清仓", 0),
        ("601088已清仓吗？", 0),
        ("601088已清仓的话就提醒", 0),
        ("601088清仓了就告诉我", 0),
        ("601088已清仓是假设", 0),
        ("601088已清仓，但没确认", 0),
        ("601088持有0股的话", 0),
        ("601088数量0吗？", 0),
        ("如果601088数量700股就提醒", 700),
        ("601088可能持有700股", 700),
        ("601088预计持有700股", 700),
        ("601088卖出700股", 700),
        ("601088持有-1股", 1),
        ("601088持有1e3股", 3),
        ("601088数量1,00股", 1),
    ],
)
def test_identifiers_questions_negation_and_conditions_are_never_facts(text, quantity):
    with pytest.raises(ValueError):
        validate_manual_rows([row(quantity=quantity)], text, [])


def test_only_selected_security_is_modified_even_if_model_repeats_entire_snapshot():
    existing = [
        row("601088", 1000, name="中国神华", currency="CNY"),
        row("01088", 400, currency="HKD"),
    ]
    original = deepcopy(existing)
    grounded, untouched = validate_manual_rows(
        [
            row("601088", 9999, currency="USD"),
            row("01088", 500, currency="USD", price=0),
        ],
        "01088现在500股",
        existing,
    )
    assert grounded == [row("01088", 500, currency="HKD")]
    assert untouched == ["601088"]
    assert existing == original


def test_unknown_second_code_cannot_lend_its_quantity_to_first_security():
    with pytest.raises(ValueError):
        validate_manual_rows(
            [row("601088", 500)], "601088继续持有，600036现在500股", []
        )


def test_multiple_selected_securities_keep_their_own_quantities():
    grounded, untouched = validate_manual_rows(
        [row("601088", 700), row("00700", 300)],
        "601088现在持有700股，00700数量300股",
        [],
    )
    assert grounded == [row("601088", 700), row("00700", 300)]
    assert untouched == []


def test_same_name_across_a_h_markets_needs_code():
    existing = [row("601088", 100, name="中国神华"), row("01088", 200, name="中国神华")]
    with pytest.raises(ValueError, match="多个市场"):
        validate_manual_rows([row("601088", 700)], "中国神华现在700股", existing)
    grounded, _ = validate_manual_rows(
        [row("601088", 700)], "中国神华601088现在700股", existing
    )
    assert grounded[0]["instrument"] == "601088"
    assert grounded[0]["quantity"] == 700


def test_unique_existing_name_is_a_grounded_entity():
    grounded, _ = validate_manual_rows(
        [row("601088", 700)], "中国神华现在700股", [row(name="中国神华")]
    )
    assert grounded[0]["quantity"] == 700


def test_preserve_existing_metadata_and_never_import_model_inventions():
    existing = [
        row(
            quantity=1000,
            currency="CNY",
            price=40,
            market_value=40000,
            name="中国神华",
            custom={"keep": True},
            quote_evidence={"source": "previous_quote"},
        )
    ]
    original = deepcopy(existing)
    grounded, _ = validate_manual_rows(
        [
            row(
                quantity=700,
                currency="USD",
                price=999,
                market_value=999999,
                name="伪造名称",
                custom={"keep": False},
            )
        ],
        "601088现在700股",
        existing,
    )
    assert grounded[0] == dict(existing[0], quantity=700)
    assert existing == original


def test_new_hk_manual_facts_come_from_typed_labels_not_model_metadata():
    text = "证券01088，数量500股，价格41.60港元，市值20800港元，币种HKD"
    grounded, _ = validate_manual_rows(
        [row("01088", 500, currency="USD", price=9, market_value=999, name="模型名字")],
        text,
        [],
    )
    assert grounded == [
        row("01088", 500, price=41.6, market_value=20800.0, currency="HKD")
    ]


def test_explicit_metadata_change_removes_obsolete_quote_and_unit_evidence():
    existing = [
        row(
            "01088",
            400,
            currency="HKD",
            price=40,
            market_value=16000,
            quote_evidence={"price": 40},
            raw_values={
                "quantity": {"unit": "万股"},
                "price": {"unit": "分"},
                "other": "keep",
            },
        )
    ]
    original = deepcopy(existing)
    grounded, _ = validate_manual_rows(
        [row("01088", 500)],
        "01088数量500股，价格41.60港元，市值2.08万港元，币种HKD",
        existing,
    )
    assert grounded[0]["price"] == 41.6
    assert grounded[0]["market_value"] == 20800
    assert "quote_evidence" not in grounded[0]
    assert grounded[0]["raw_values"] == {"other": "keep"}
    assert existing == original


def test_uncertain_metadata_does_not_overwrite_existing_fact():
    grounded, _ = validate_manual_rows(
        [row("01088", 500, currency="USD", price=999)],
        "01088数量500股，币种USD吗，价格999美元吗？",
        [row("01088", 400, currency="HKD", price=40)],
    )
    assert grounded[0]["currency"] == "HKD"
    assert grounded[0]["price"] == 40


@pytest.mark.parametrize(
    "rows,text",
    [
        ([row("601088", 100), row("601088.SS", 100)], "601088数量100股"),
        ([row(quantity=700)], "601088数量800股"),
        ([row(quantity=700)], "601088数量700股，现在800股"),
        ([row(quantity=True)], "601088数量1股"),
        ([row(quantity=float("nan"))], "601088数量700股"),
        ([row("01088", 500)], "01088数量500股，价格41港元，币种USD"),
    ],
)
def test_conflicting_or_duplicate_model_rows_fail_closed(rows, text):
    with pytest.raises(ValueError):
        validate_manual_rows(rows, text, [])


def test_live_rule_request_holding_noun_introduces_security_not_quantity():
    text = (
        "请直接在个人隔离测试账户qa-case记录当前持仓601088中国神华1000股，币种CNY，"
        "创建预期年化回报小于6%和小于4%各提醒一次的规则，不提卖出比例。"
        "这些是假设验收输入，不是真实持仓。研究使用我明确选定的测试版本qa-v1、场景base，"
        "模型equity_cashflow_dcf_v1，来源是本条用户提供的隔离测试假设："
        "cashflow_per_share=3，growth_first=0.02，growth_second=0.02，terminal_growth=0.02；"
        "发送回当前会话。直接执行这些个人监控操作并返回结果。"
    )
    grounded, untouched = validate_manual_rows([row(quantity=1000)], text, [])
    assert grounded == [row(quantity=1000, currency="CNY")]
    assert untouched == []


@pytest.mark.parametrize("prefix", ["持仓", "持有", "当前持仓", "现在持有"])
def test_holding_prefix_disambiguated_by_following_own_quantity(prefix):
    grounded, _ = validate_manual_rows(
        [row(quantity=1000)], prefix + "601088中国神华1000股", []
    )
    assert grounded[0]["quantity"] == 1000


@pytest.mark.parametrize("quantity", [100000, 10000])
def test_holding_quantity_without_share_unit_is_not_a_new_security(quantity):
    grounded, _ = validate_manual_rows(
        [row(quantity=quantity)], f"601088持仓{quantity}", []
    )
    assert grounded == [row(quantity=quantity)]


def test_later_security_quantity_cannot_reclassify_previous_holding_amount():
    grounded, untouched = validate_manual_rows(
        [row(quantity=100000), row("100000", 300), row("00700", 300)],
        "601088持仓100000，00700持有300股",
        [],
    )
    assert grounded == [row(quantity=100000), row("00700", 300)]
    assert untouched == ["100000"]


def test_live_screenshot_correction_explicit_h_code_wins_over_existing_a_name():
    text = (
        "手工更正刚才截图的问题行：账户acceptance-isolated，证券01088中国神华，数量500股，"
        "币种HKD，价格41.60港元，市值20800港元。数据时间明确为2026-10-20T00:00:00+08:00，"
        "请直接更新这一行，其他行保留。"
    )
    existing = [
        row(
            "601088",
            1000,
            name="中国神华",
            currency="CNY",
            price=40,
            market_value=40000,
        )
    ]
    original = deepcopy(existing)
    grounded, untouched = validate_manual_rows(
        [row("601088", 1000), row("01088", 500, currency="USD", price=999)],
        text,
        existing,
    )
    assert grounded == [
        row("01088", 500, currency="HKD", price=41.6, market_value=20800.0)
    ]
    assert untouched == ["601088"]
    assert existing == original


@pytest.mark.parametrize("separator", ["", " ", "：", "（"])
def test_name_adjacent_to_explicit_code_is_not_another_existing_security(separator):
    grounded, untouched = validate_manual_rows(
        [row("01088", 500), row("601088", 100)],
        f"01088{separator}中国神华，数量500股",
        [row("601088", 100, name="中国神华")],
    )
    assert grounded == [row("01088", 500)]
    assert untouched == ["601088"]


@pytest.mark.parametrize(
    "stamp",
    ["2026-10-20T00:00:00+08:00", "2026-10-20 T00:00:00Z", "2026-10-20 00:00:00"],
)
def test_iso_time_tokens_do_not_split_security_from_its_quantity(stamp):
    grounded, _ = validate_manual_rows(
        [row("01088", 500)], f"01088数据时间{stamp}，数量500股", []
    )
    assert grounded == [row("01088", 500)]


def test_time_exclusion_does_not_ignore_an_actual_second_security():
    grounded, untouched = validate_manual_rows(
        [row("601088", 500), row("01088", 500)],
        "601088数据时间2026-10-20T00:00:00+08:00，01088数量500股",
        [],
    )
    assert grounded == [row("01088", 500)]
    assert untouched == ["601088"]
