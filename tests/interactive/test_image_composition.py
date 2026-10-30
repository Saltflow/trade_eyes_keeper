"""Complementary screenshots join by evidence, never by position or division."""

from copy import deepcopy

import pytest

from src.interactive.assistant.image_composition import compose_position_document

CURRENT = "a" * 64
PREVIOUS = "b" * 64
THIRD = "c" * 64


def row(**overrides):
    result = {
        "code": None,
        "name": "示例股份",
        "quantity": None,
        "price": "12.3400",
        "price_decimals": 4,
        "market_value": "1234.00",
        "currency": "CNY",
        "market_value_unit": "元",
        "quantity_unit": None,
        "price_unit": "元",
        "uncertain_fields": ["code", "quantity"],
    }
    result.update(overrides)
    return result


def document(*rows, **overrides):
    result = {
        "account_hint": "示例 账户",
        "as_of": None,
        "positions": list(rows),
        "reminders": [],
        "ambiguities": [],
    }
    result.update(overrides)
    return result


def previous(document_value=None, **overrides):
    result = {
        "batch_id": "previous-batch",
        "source_image_hashes": [PREVIOUS],
        "message_time": {"create_time": "1792850000000"},
        "document": document_value
        or document(
            row(
                code="600001",
                quantity="100",
                price=None,
                price_decimals=0,
                uncertain_fields=["price"],
            )
        ),
    }
    result.update(overrides)
    return result


def test_complementary_columns_match_decimal_value_and_exact_account_without_mutation():
    current = document(row())
    batch = previous()
    batch["document"]["account_hint"] = "示例账户"
    originals = deepcopy((current, batch))
    result = compose_position_document(current, CURRENT, [batch])
    combined = result["positions"][0]
    assert combined["code"] == "600001" and combined["quantity"] == "100"
    assert combined["price"] == "12.3400"
    assert combined["uncertain_fields"] == []
    assert combined["field_sources"]["code"] == [PREVIOUS]
    assert combined["field_sources"]["quantity"] == [PREVIOUS]
    assert combined["field_sources"]["price"] == [CURRENT]
    assert combined["source_image_hashes"] == [CURRENT, PREVIOUS]
    assert result["source_image_hashes"] == [CURRENT, PREVIOUS]
    assert result["composition"]["source_batch_ids"] == ["previous-batch"]
    assert (current, batch) == originals


def test_missing_price_currency_joins_on_explicit_market_value_currency():
    current = document(
        row(
            code="512810",
            quantity=100,
            price=None,
            currency=None,
            price_unit=None,
            market_value_currency="CNY",
            price_decimals=0,
            asset_type="etf",
            uncertain_fields=["price", "price_unit", "currency", "price_decimals"],
            ambiguities=["price:未显示", "currency:价格币种未显示"],
        )
    )
    donor = document(
        row(
            asset_type="other",
            market_value_currency="CNY",
            uncertain_fields=["code", "quantity", "asset_type"],
            ambiguities=["asset_type:名称未显示类型"],
        )
    )
    result = compose_position_document(current, CURRENT, [previous(donor)])
    combined = result["positions"][0]
    assert combined["price"] == "12.3400" and combined["currency"] == "CNY"
    assert combined["asset_type"] == "etf" and combined["price_decimals"] == 4
    assert combined["uncertain_fields"] == [] and combined["ambiguities"] == []
    assert combined["field_sources"]["price"] == [PREVIOUS]
    assert combined["field_sources"]["currency"] == [PREVIOUS]


@pytest.mark.parametrize(
    "changes",
    [
        {"market_value_currency": "USD"},
        {"market_value_currency": "CNY", "currency": "HKD"},
        {
            "market_value_currency": "CNY",
            "uncertain_fields": ["price", "market_value_currency"],
        },
    ],
)
def test_market_value_currency_matching_does_not_override_conflict_or_uncertainty(
    changes,
):
    current_row = row(
        code="600001",
        quantity=100,
        price=None,
        currency=None,
        market_value_currency="CNY",
        uncertain_fields=["price", "currency"],
    )
    current_row.update(changes)
    donor = document(
        row(market_value_currency="CNY", uncertain_fields=["code", "quantity"])
    )
    result = compose_position_document(
        document(current_row), CURRENT, [previous(donor)]
    )
    assert result["positions"][0]["price"] is None


def test_only_explicitly_uncertain_other_asset_type_can_be_replaced():
    current = document(
        row(
            code="512810",
            quantity=100,
            price=None,
            asset_type="other",
            uncertain_fields=["price", "asset_type"],
            ambiguities=["asset_type:看不清类型"],
        )
    )
    donor = document(row(asset_type="etf"))
    combined = compose_position_document(current, CURRENT, [previous(donor)])[
        "positions"
    ][0]
    assert combined["asset_type"] == "etf" and combined["price"] == "12.3400"
    assert combined["uncertain_fields"] == [] and combined["ambiguities"] == []
    current["positions"][0]["uncertain_fields"] = ["price"]
    assert (
        compose_position_document(current, CURRENT, [previous(donor)])["positions"][0][
            "price"
        ]
        is None
    )


def test_reordered_rows_match_names_values_and_not_row_numbers():
    current = document(
        row(name="甲", market_value=100), row(name="乙", market_value=200)
    )
    donor = document(
        row(
            name="乙",
            market_value="200.00",
            code="600002",
            quantity=20,
            price=None,
            uncertain_fields=[],
        ),
        row(
            name="甲",
            market_value="100.0",
            code="600001",
            quantity=10,
            price=None,
            uncertain_fields=[],
        ),
    )
    result = compose_position_document(current, CURRENT, [previous(donor)])
    assert [item["code"] for item in result["positions"]] == ["600001", "600002"]
    assert [item["quantity"] for item in result["positions"]] == [10, 20]


def test_different_names_require_explicit_visible_alias_or_same_code():
    current = document(row(name="示例简称"))
    donor = document(
        row(
            code="600001",
            name="示例股份有限公司",
            quantity=100,
            price=None,
            uncertain_fields=[],
        )
    )
    assert (
        compose_position_document(current, CURRENT, [previous(donor)])["positions"][0][
            "quantity"
        ]
        is None
    )
    donor["positions"][0]["aliases"] = ["示例简称"]
    assert (
        compose_position_document(current, CURRENT, [previous(donor)])["positions"][0][
            "quantity"
        ]
        == 100
    )
    current["positions"][0].update(code="600001", uncertain_fields=["quantity"])
    donor["positions"][0].pop("aliases")
    assert (
        compose_position_document(current, CURRENT, [previous(donor)])["positions"][0][
            "quantity"
        ]
        == 100
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "600002"),
        ("quantity", 101),
        ("price", "12.35"),
        ("quantity_unit", "万股"),
        ("price_unit", "分"),
        ("market_value_unit", "万元"),
        ("asset_type", "reverse_repo"),
        ("market", "HK"),
        ("exchange", "SZ"),
    ],
)
def test_nonempty_conflicts_never_merge(field, value):
    current_row = row(
        code="600001",
        quantity=100,
        quantity_unit="股",
        asset_type="stock",
        market="A",
        exchange="SH",
        uncertain_fields=[],
    )
    donor_row = deepcopy(current_row)
    donor_row[field] = value
    donor_row["name"] = None
    # A shared visible alias still cannot override conflicting facts.
    donor_row["aliases"] = [current_row["name"]]
    current_row["price_decimals"] = None
    result = compose_position_document(
        document(current_row), CURRENT, [previous(document(donor_row))]
    )
    assert result["positions"][0]["price_decimals"] is None
    assert result["composition"]["matched_rows"] == []
    assert result["composition"]["blocked"][0]["reason"] == "conflicting_facts"


@pytest.mark.parametrize(
    "currency,value",
    [
        ("HKD", "1234.00"),
        ("CNY", "1234.01"),
        (None, "1234.00"),
        ("CNY", "NaN"),
        ("CNY", True),
    ],
)
def test_currency_and_decimal_market_value_are_required_anchors(currency, value):
    donor = previous()
    donor["document"]["positions"][0].update(currency=currency, market_value=value)
    result = compose_position_document(document(row()), CURRENT, [donor])
    assert result["positions"][0]["code"] is None


@pytest.mark.parametrize("hint", [None, "", "另一个账户"])
def test_account_is_required_and_must_match(hint):
    result = compose_position_document(
        document(row(), account_hint=hint), CURRENT, [previous()]
    )
    assert result["positions"][0]["quantity"] is None


def test_duplicate_name_amount_candidates_and_duplicate_current_rows_are_ambiguous():
    donor = previous(
        document(
            row(code="600001", quantity=100, price=None, uncertain_fields=[]),
            row(code="600002", quantity=100, price=None, uncertain_fields=[]),
        )
    )
    result = compose_position_document(document(row()), CURRENT, [donor])
    assert result["positions"][0]["code"] is None
    assert result["composition"]["blocked"] == [
        {"row_index": 0, "reason": "ambiguous_candidates"}
    ]
    result = compose_position_document(document(row(), row()), CURRENT, [previous()])
    assert all(item["quantity"] is None for item in result["positions"])
    assert len(result["composition"]["blocked"]) == 2


def test_duplicates_across_different_images_remain_ambiguous_not_first_wins():
    result = compose_position_document(
        document(row()),
        CURRENT,
        [previous(), previous(source_image_hashes=[THIRD], batch_id="third")],
    )
    assert result["positions"][0]["code"] is None
    assert result["composition"]["blocked"][0]["reason"] == "ambiguous_candidates"


@pytest.mark.parametrize(
    "current_time,previous_time",
    [
        ("2026-10-25", "2026-10-24"),
        (None, "2026-10-25"),
        ("2026-10-25", None),
        ("2026-10-25T14:00:00+08:00", "2026-10-25T14:01:00+08:00"),
        ("invalid", "invalid"),
    ],
)
def test_old_or_unprovable_snapshot_times_do_not_merge(current_time, previous_time):
    result = compose_position_document(
        document(row(), as_of=current_time),
        CURRENT,
        [
            previous(
                document(
                    row(code="600001", quantity=100, price=None, uncertain_fields=[]),
                    as_of=previous_time,
                )
            )
        ],
    )
    assert result["positions"][0]["code"] is None


def test_equal_offsets_merge_but_conflicting_user_declaration_does_not():
    current = document(row(), as_of="2026-10-25T14:00:00+08:00")
    donor = previous(
        document(
            row(code="600001", quantity=100, price=None, uncertain_fields=[]),
            as_of="2026-10-25T06:00:00Z",
        )
    )
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        == "600001"
    )
    current["declared_as_of"] = "2026-10-24T14:00:00+08:00"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        is None
    )


def test_platform_message_time_is_not_a_snapshot_timestamp_or_fact():
    donor = previous(message_time={"create_time": "100", "update_time": "999"})
    result = compose_position_document(document(row()), CURRENT, [donor])
    assert result["positions"][0]["code"] == "600001"
    assert result["as_of"] is None
    assert "message_time" not in result["positions"][0]


def test_free_text_ambiguities_and_flags_remain_field_uncertainty_only_partly_resolves():
    current = document(
        row(
            ambiguities=["数量或价格列含义不明确"],
            issues=["ambiguous_fields"],
            uncertain_fields=["code", "quantity", "price_unit"],
        )
    )
    donor = previous()
    donor["document"]["positions"][0]["ambiguities"] = ["来源备注仍需核查"]
    donor["document"]["positions"][0]["uncertain_fields"] = ["price", "quantity"]
    result = compose_position_document(current, CURRENT, [donor])
    item = result["positions"][0]
    assert item["code"] == "600001" and item["quantity"] is None
    assert item["uncertain_fields"] == ["quantity", "price_unit"]
    assert item["ambiguities"] == ["数量或价格列含义不明确", "来源备注仍需核查"]
    assert item["issues"] == ["ambiguous_fields"]


def test_does_not_divide_market_value_to_reconstruct_quantity_or_price():
    donor = previous()
    donor["document"]["positions"][0].update(
        quantity=None, price=None, uncertain_fields=["quantity", "price"]
    )
    result = compose_position_document(document(row()), CURRENT, [donor])
    assert result["positions"][0]["quantity"] is None
    current = document(
        row(
            price=None,
            price_decimals=None,
            quantity=100,
            uncertain_fields=["code", "price"],
        )
    )
    result = compose_position_document(current, CURRENT, [donor])
    assert result["positions"][0]["price"] is None


def test_price_precision_comes_with_its_directly_observed_price():
    current = document(
        row(
            code="600001",
            quantity=100,
            price=None,
            price_decimals=0,
            uncertain_fields=["price"],
        )
    )
    donor = previous(
        document(
            row(
                price="12.3400", price_decimals=4, uncertain_fields=["code", "quantity"]
            )
        )
    )
    result = compose_position_document(current, CURRENT, [donor])
    item = result["positions"][0]
    assert item["price"] == "12.3400" and item["price_decimals"] == 4
    assert item["field_sources"]["price"] == [PREVIOUS]
    assert item["field_sources"]["price_decimals"] == [PREVIOUS]


def test_reverse_repo_keeps_observed_nominal_price_and_does_not_generate_reminder():
    current = document(
        row(
            name="示例逆回购",
            price="100.0000",
            market_value=9000,
            asset_type="reverse_repo",
        )
    )
    donor = previous(
        document(
            row(
                code="131999",
                name="示例逆回购",
                quantity=90,
                price=None,
                market_value="9000.00",
                asset_type="reverse_repo",
                uncertain_fields=["price"],
            )
        )
    )
    result = compose_position_document(current, CURRENT, [donor])
    item = result["positions"][0]
    assert (
        item["asset_type"] == "reverse_repo"
        and item["price"] == "100.0000"
        and item["quantity"] == 90
    )
    assert result["reminders"] == []


def test_a_and_h_securities_and_explicit_exchange_conflicts_never_merge():
    current = document(
        row(code="600001", currency="CNY", uncertain_fields=["quantity"])
    )
    donor = previous(
        document(
            row(
                code="00001.HK",
                quantity=100,
                currency="CNY",
                price=None,
                uncertain_fields=[],
            )
        )
    )
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["quantity"]
        is None
    )
    current = document(row(code="600001.SH", uncertain_fields=["quantity"]))
    donor["document"]["positions"][0]["code"] = "600001.SZ"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["quantity"]
        is None
    )
    current = document(row(), market="A")
    donor["document"].update(market="HK")
    donor["document"]["positions"][0]["code"] = "00001.HK"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["quantity"]
        is None
    )


def test_same_image_is_not_an_independent_source_and_hash_collision_fails_closed():
    assert (
        compose_position_document(
            document(row()), CURRENT, [previous(source_image_hashes=[CURRENT])]
        )["positions"][0]["code"]
        is None
    )
    first, conflicting = previous(), previous()
    conflicting["document"]["positions"][0]["quantity"] = 999
    assert (
        compose_position_document(document(row()), CURRENT, [first, conflicting])[
            "positions"
        ][0]["code"]
        is None
    )
    assert (
        compose_position_document(
            document(row()), CURRENT, [previous(source_image_hashes=[])]
        )["positions"][0]["code"]
        is None
    )


def test_replay_is_idempotent_and_repeated_batch_is_not_duplicate_candidate():
    current, donor = document(row()), previous()
    first = compose_position_document(current, CURRENT, [donor, deepcopy(donor)])
    assert first["positions"][0]["code"] == "600001"
    assert compose_position_document(current, CURRENT, [donor]) == first
    assert compose_position_document(first, CURRENT, [donor]) == first


def test_preserves_current_row_count_no_historical_rows_or_notes_become_facts():
    donor = previous(
        document(
            row(code="600001", quantity=100, price=None, uncertain_fields=[]),
            row(
                code="600002",
                name="独有历史行",
                market_value=999,
                quantity=4,
                price=249.75,
            ),
        )
    )
    donor["document"]["positions"][0]["evidence"] = "quantity=99999 display note"
    result = compose_position_document(
        document(row(evidence="当前图原文")), CURRENT, [donor]
    )
    assert len(result["positions"]) == 1
    assert result["positions"][0]["quantity"] == 100
    assert result["positions"][0]["evidence"] == "当前图原文"


def test_unverified_field_sources_cannot_introduce_synthetic_hashes():
    donor = previous()
    donor["document"]["positions"][0]["field_sources"] = {"quantity": ["invented"]}
    result = compose_position_document(document(row()), CURRENT, [donor])
    assert result["positions"][0]["quantity"] is None
    assert "invented" not in str(result)


@pytest.mark.parametrize(
    "current_field,previous_field",
    [
        ("declared_account", "declared_account"),
        ("confirmed_account", "account_id"),
        ("account_id", "confirmed_account"),
    ],
)
def test_known_account_identity_conflicts_override_matching_generic_hint(
    current_field, previous_field
):
    current, donor = document(row()), previous()
    current[current_field] = "broker-one"
    donor["document"][previous_field] = "broker-two"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["quantity"]
        is None
    )
    donor["document"][previous_field] = "broker- one"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["quantity"]
        == "100"
    )


def test_conflicting_identity_fields_inside_single_document_or_rows_fail_closed():
    current, donor = (
        document(row(), account_id="one", declared_account="two"),
        previous(),
    )
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        is None
    )
    current = document(row(account_id="one"))
    donor["document"]["positions"][0]["confirmed_account"] = "two"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        is None
    )


def test_only_explicit_field_prefixed_notes_resolve_in_both_directions():
    current = document(
        row(
            uncertain_fields=["code", "quantity", "quantity_unit"],
            ambiguities=[
                "code:未显示",
                "quantity：未显示",
                "quantity_unit:单位未标注",
                "旧版自由文字仍要保留",
            ],
            issues=["ambiguous_fields"],
        )
    )
    donor = previous()
    donor["document"]["positions"][0]["ambiguities"] = [
        "price:未显示",
        "价格列说明仅为显示备注",
    ]
    result = compose_position_document(current, CURRENT, [donor])
    item = result["positions"][0]
    assert item["uncertain_fields"] == ["quantity_unit"]
    assert item["ambiguities"] == [
        "quantity_unit:单位未标注",
        "旧版自由文字仍要保留",
        "价格列说明仅为显示备注",
    ]
    assert item["issues"] == ["ambiguous_fields"]


def test_field_prefix_without_corresponding_declared_uncertainty_is_preserved():
    current = document(
        row(uncertain_fields=[], ambiguities=["quantity:之前的显示提示"])
    )
    result = compose_position_document(current, CURRENT, [previous()])
    assert result["positions"][0]["quantity"] == "100"
    assert result["positions"][0]["ambiguities"] == ["quantity:之前的显示提示"]


def test_multiphysical_image_provenance_preserves_column_sources_not_batch_hash():
    aggregate = "d" * 64
    current = document(
        row(
            code="600001",
            quantity=100,
            uncertain_fields=[],
            field_sources={
                "code": [PREVIOUS],
                "quantity": [PREVIOUS],
                "price": [CURRENT],
                "market_value": [CURRENT, PREVIOUS],
            },
        ),
        source_image_hashes=[CURRENT, PREVIOUS],
    )
    result = compose_position_document(
        current, aggregate, [], current_image_hashes=[CURRENT, PREVIOUS]
    )
    sources = result["positions"][0]["field_sources"]
    assert sources["code"] == [PREVIOUS]
    assert sources["quantity"] == [PREVIOUS]
    assert sources["price"] == [CURRENT]
    assert sources["market_value"] == [CURRENT, PREVIOUS]
    assert result["source_image_hashes"] == [CURRENT, PREVIOUS]
    assert aggregate not in str(result)
    assert (
        compose_position_document(
            result, aggregate, [], current_image_hashes=[CURRENT, PREVIOUS]
        )
        == result
    )


def test_multiphysical_image_sources_ignore_document_claimed_hashes_and_overlap():
    aggregate, fake = "d" * 64, "invented-hash"
    current = document(
        row(field_sources={"price": [CURRENT, fake], "name": [fake]}),
        source_image_hashes=[CURRENT, PREVIOUS, fake],
    )
    result = compose_position_document(
        current, aggregate, [previous()], current_image_hashes=[CURRENT, PREVIOUS]
    )
    item = result["positions"][0]
    assert item["code"] is None and item["quantity"] is None
    assert item["field_sources"]["price"] == [CURRENT]
    assert item["field_sources"]["name"] == []
    assert fake not in str(result)
    assert aggregate not in str(result)


@pytest.mark.parametrize("hashes", [[], None])
def test_physical_hash_list_is_explicitly_validated_without_trusting_document(hashes):
    # None deliberately preserves the original single-image contract.
    if hashes is None:
        result = compose_position_document(
            document(row()), CURRENT, [], current_image_hashes=None
        )
        assert result["source_image_hashes"] == [CURRENT]
    else:
        with pytest.raises(ValueError, match="physical_image_hashes"):
            compose_position_document(
                document(row(), source_image_hashes=[CURRENT]),
                "batch",
                [],
                current_image_hashes=hashes,
            )


@pytest.mark.parametrize("current_known", [False, True])
def test_generic_hint_cannot_bridge_unknown_and_known_account(current_known):
    current, donor = document(row()), previous()
    if current_known:
        current["confirmed_account"] = "broker-B"
    else:
        donor["document"]["confirmed_account"] = "broker-B"
    result = compose_position_document(current, CURRENT, [donor])
    assert result["positions"][0]["code"] is None
    assert result["positions"][0]["quantity"] is None
    assert result["composition"]["source_batch_ids"] == []
    assert result["source_image_hashes"] == [CURRENT]


@pytest.mark.parametrize(
    "current_hint,donor_hint",
    [
        ("账户页面A", "账户页面B"),
        (None, None),
        ("", "不同显示名称"),
    ],
)
def test_same_confirmed_account_overrides_different_or_missing_display_hint(
    current_hint, donor_hint
):
    current, donor = (
        document(row(), account_hint=current_hint, confirmed_account="stable-broker"),
        previous(),
    )
    donor["document"].update(account_hint=donor_hint, account_id="stable-broker")
    result = compose_position_document(current, CURRENT, [donor])
    assert result["positions"][0]["code"] == "600001"
    assert result["positions"][0]["quantity"] == "100"
    assert result["composition"]["source_batch_ids"] == ["previous-batch"]


def test_resolve_account_then_recompose_original_facts_without_cross_account_pollution():
    current, donor = document(row()), previous()
    donor["document"]["confirmed_account"] = "broker-B"
    unknown = compose_position_document(current, CURRENT, [donor])
    assert unknown["positions"][0]["code"] is None
    current["confirmed_account"] = "broker-A"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        is None
    )
    current["confirmed_account"] = "broker-B"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        == "600001"
    )


def test_stable_account_identifiers_are_case_sensitive():
    current, donor = document(row(), account_id="broker-A"), previous()
    donor["document"]["account_id"] = "broker-a"
    assert (
        compose_position_document(current, CURRENT, [donor])["positions"][0]["code"]
        is None
    )
