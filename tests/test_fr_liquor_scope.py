"""Real financial-scope regressions for the frozen disclosure parser."""

import json
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import extract_field
from src.fundamental_data.disclosure.absence import apply_explicit_absences
from src.fundamental_data.disclosure.contract import FIELDS

ROOT = Path(__file__).parent / "fixtures/fr_liquor_scope"


def fixture(name):
    return json.loads((ROOT / f"{name}.json").read_text(encoding="utf-8"))


def fields(meta):
    return {
        k: {
            "metric": k,
            "label": v[0],
            "status": "not_located",
            "period_end": meta["period_end"],
            "scope": "consolidated",
            "candidates": [],
        }
        for k, v in FIELDS.items()
    }


def test_real_moutai_operating_interest_is_separate_from_finance_cost():
    f = fixture("moutai_interest")
    field = extract_field(
        "interest_income", FIELDS["interest_income"], f["sections"], f["metadata"]
    )
    assert field["status"] == "obtained"
    assert field["amount"] == "842988240.60"
    assert field["comparative_amount"] == "1476991223.18"
    assert field["component_scope"]["name"] == "finance_cost"
    assert any("3,216,069,376.12" in x["quote"] for x in field["scope_exclusions"])
    assert not any(c.get("amount") == "3216069376.12" for c in field["candidates"])


def test_missing_finance_cost_parent_does_not_promote_operating_interest():
    f = fixture("moutai_interest")
    f["sections"][0]["lines"] = [
        r for r in f["sections"][0]["lines"] if "财务费用" not in r["text"]
    ]
    result = extract_field(
        "interest_income", FIELDS["interest_income"], f["sections"], f["metadata"]
    )
    assert result["status"] != "obtained"
    assert "amount" not in result


def test_real_luzhou_none_is_an_explicit_optional_amount_note_exclusion():
    f = fixture("luzhou_absence")
    financial = fields(f["metadata"])
    reviewed = apply_explicit_absences(financial, f["sections"], f["metadata"])
    assert reviewed == ["contract_assets"]
    field = financial["contract_assets"]
    assert field["status"] == "not_applicable"
    assert field["quote"] == "5、合同资产\n无。"
    assert field["source_ref"]["pages"] == [78]
    assert "amount" not in field and "value" not in field


@pytest.mark.parametrize("kind", ["policy", "other_notes", "notes_block"])
def test_policy_parent_or_generic_notes_do_not_exclude(kind):
    f = fixture("luzhou_absence")
    f["sections"][0]["kind"] = kind
    assert not apply_explicit_absences(
        fields(f["metadata"]), f["sections"], f["metadata"]
    )


@pytest.mark.parametrize(
    "body", ["", "尚未披露", "本期无坏账准备转回。", "√适用 □不适用", "无重大变动。"]
)
def test_blank_unknown_or_subtopic_none_is_not_excluded(body):
    f = fixture("luzhou_absence")
    f["sections"][0]["lines"][1]["text"] = body
    assert not apply_explicit_absences(
        fields(f["metadata"]), f["sections"], f["metadata"]
    )


def test_numeric_candidate_or_confirmed_conflict_vetoes_exclusion():
    f = fixture("luzhou_absence")
    for status in ("parse_failed", "conflict", "obtained"):
        financial = fields(f["metadata"])
        financial["contract_assets"].update(
            status=status, candidates=[{"numbers": ["1,000.00"]}]
        )
        assert not apply_explicit_absences(financial, f["sections"], f["metadata"])




def test_explicit_none_cross_page_or_extra_text_not_auto_excluded():
    f = fixture("luzhou_absence")
    f["sections"][0]["lines"][1]["page"] += 1
    assert not apply_explicit_absences(
        fields(f["metadata"]), f["sections"], f["metadata"]
    )


def test_real_pdfium_cross_page_parent_equity_ignores_spaced_page_number():
    from src.fundamental_data.disclosure import locate_sections

    source = fixture("moutai_cross_page")
    f = extract_field(
        "parent_equity",
        FIELDS["parent_equity"],
        locate_sections(source["pages"]),
        source["metadata"],
    )
    assert f["status"] == "obtained"
    assert f["amount"] == "244637811032.18"
    assert f["comparative_amount"] == "233105984399.47"
    assert f["source_ref"]["pages"] == [58, 59]


def test_real_pdfium_inline_note_heading_recovers_complete_amount_note():
    from src.fundamental_data.disclosure import locate_sections

    source = fixture("luzhou_inline_note")
    sections = locate_sections(source["pages"])
    reviewed = apply_explicit_absences(
        fields(source["metadata"]), sections, source["metadata"]
    )
    assert reviewed == ["contract_assets"]
    note = next(
        s for s in sections if s["kind"] == "notes" and s["title"] == "5、合同资产"
    )
    assert note["text"] == "5、合同资产\n无。"


def test_references_in_prose_never_create_major_note_headings():
    from src.fundamental_data.disclosure import locate_sections

    source = {"1": "详情见七、合并财务报表项目注释\n5、合同资产\n无。"}
    assert not [s for s in locate_sections(source) if s["kind"] == "notes"]


@pytest.mark.parametrize(
    "metric,value", [("long_debt", "3179600000.00"), ("treasury_stock", "639021998.78")]
)
def test_real_same_year_balance_dates_keep_current_and_beginning_separate(
    metric, value
):
    source = fixture("luzhou_same_year_dates")
    result = extract_field(
        metric, FIELDS[metric], source["sections"], source["metadata"], source["layout"]
    )
    assert result["status"] == "obtained"
    assert result["amount"] == value
    assert result["comparative_amount"] is None
    assert result["comparative_period_end"] == "2022-01-01"
    assert result["coordinate_evidence"]["column"] == "current"


@pytest.mark.parametrize(
    "metric,value",
    [
        ("investment_cash_in", "16841494897"),
        ("investment_income", "-894540912"),
        ("fair_value_change", "-1048183094"),
        ("interest_expense", "21808761138"),
        ("interest_income", "23896197081"),
        ("treasury_stock", "522073500"),
    ],
)
def test_real_cccc_2023_aliases_and_negative_finance_parent(metric, value):
    source = fixture("cccc_2023_statements")
    result = extract_field(
        metric, FIELDS[metric], source["sections"], source["metadata"], source["layout"]
    )
    assert result["status"] == "obtained"
    assert result["amount"] == value
    if metric == "investment_income":
        assert any("1,002,107,838" in x["quote"] for x in result["scope_exclusions"])
    if metric == "treasury_stock":
        assert result["comparative_amount"] is None
        assert result["cell_status"]["comparative"] == "blank_in_statement"


def test_real_cccc_dash_current_cashflow_is_not_fabricated_zero():
    source = fixture("cccc_2023_statements")
    result = extract_field(
        "acquisition_cash",
        FIELDS["acquisition_cash"],
        source["sections"],
        source["metadata"],
        source["layout"],
    )
    assert result["status"] != "obtained"
    assert "amount" not in result
    assert result["cell_status"]["current"] == "blank_in_statement"
    candidate = next(c for c in result["candidates"] if c.get("comparative_amount"))
    assert candidate["comparative_amount"] == "2721257293"
    assert candidate["comparative_reported_amount"] == "-2721257293"
