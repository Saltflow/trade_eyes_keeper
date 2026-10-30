"""Frozen original engineering PDFs: labels, NA comparison and drawing order."""

import json
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import extract_field, locate_sections
from src.fundamental_data.disclosure.contract import FIELDS

ROOT = Path(__file__).parent / "fixtures/fr_construction_repair"


def frozen(name):
    return json.loads((ROOT / (name + ".json")).read_text(encoding="utf8"))


def extract(d, metric):
    return extract_field(
        metric,
        FIELDS[metric],
        locate_sections(d["source"]["pages"]),
        d["source"]["metadata"],
        d["layout"],
    )


@pytest.mark.parametrize(
    "name,metric,amount",
    [
        ("600502_2020-12-31", "contract_assets", "13263826749.50"),
        ("600502_2020-12-31", "contract_liabilities", "11812287157.64"),
        ("600502_2021-12-31", "lease_liabilities", "42339887.71"),
        ("600039_2026-06-30", "borrowing_cash", "4424515622.62"),
        ("601800_2019-12-31", "fair_value_change", "793019450"),
        ("601800_2022-12-31", "fair_value_change", "-135148528"),
    ],
)
def test_original_statement_amounts(name, metric, amount):
    result = extract(frozen(name), metric)
    assert result["status"] == "obtained"
    assert result["amount"] == amount


@pytest.mark.parametrize(
    "name,metric",
    [
        ("600502_2020-12-31", "contract_assets"),
        ("600502_2020-12-31", "contract_liabilities"),
        ("600502_2021-12-31", "lease_liabilities"),
    ],
)
def test_na_comparison_is_not_zero(name, metric):
    field = extract(frozen(name), metric)
    assert field["comparative_amount"] is None
    assert field["cell_status"]["comparative"] == "explicitly_not_applicable"
    assert field["coordinate_evidence"]


def test_joined_pdf_text_needs_real_geometry():
    d = frozen("600039_2026-06-30")
    d["layout"]["pages"] = {}
    assert extract(d, "borrowing_cash")["status"] != "obtained"


def test_actual_note_explicit_zero_is_not_blank_inference():
    from src.fundamental_data.disclosure.note_totals import apply_note_totals

    d = frozen("600502_2021-12-31")
    field = extract(d, "bonds")
    assert "amount" not in field
    apply_note_totals({"bonds": field}, d["note_sections"], d["source"]["metadata"])
    assert field["amount"] == "0"
    assert field["status"] == "obtained"
    assert field["comparative_amount"] == "525000000.00"
    assert field["source_ref"]["pages"] == [195]
    assert field["note_total_evidence"]["statement_comparatives"]


@pytest.mark.parametrize(
    "mutation", ["blank", "wrong_comparison", "parent", "reversed", "duplicate"]
)
def test_note_supplement_fails_closed(mutation):
    from src.fundamental_data.disclosure.note_totals import apply_note_totals

    d = frozen("600502_2021-12-31")
    field = extract(d, "bonds")
    note = d["note_sections"][0]
    total = next(r for r in note["lines"] if r["text"] == "合计 0 525,000,000.00")
    if mutation == "blank":
        total["text"] = "合计   525,000,000.00"
    elif mutation == "wrong_comparison":
        total["text"] = "合计 0 526,000,000.00"
    elif mutation == "parent":
        note["kind"] = "other_notes"
    elif mutation == "reversed":
        next(r for r in note["lines"] if r["text"] == "项目 期末余额 期初余额")[
            "text"
        ] = "项目 期初余额 期末余额"
    else:
        note["lines"].insert(note["lines"].index(total), dict(total))
    apply_note_totals({"bonds": field}, [note], d["source"]["metadata"])
    assert field["status"] != "obtained"
    assert "amount" not in field


def test_joined_text_same_y_is_ambiguous_not_repaired():
    d = frozen("600039_2026-06-30")
    row = next(
        r for r in d["layout"]["pages"]["95"]["lines"] if "取得借款" in r["text"]
    )
    for glyph in row["cell_glyphs"][:14]:
        glyph["bbox"][1] -= 20
        glyph["bbox"][3] -= 20
    assert extract(d, "borrowing_cash")["status"] != "obtained"


def test_na_marker_in_wrong_column_is_not_accepted():
    d = frozen("600502_2020-12-31")
    row = next(
        r for r in d["layout"]["pages"]["70"]["lines"] if "合同资产" in r["text"]
    )
    for glyph in row["cell_glyphs"][-3:]:
        glyph["bbox"][0] -= 130
        glyph["bbox"][2] -= 130
    assert extract(d, "contract_assets")["status"] != "obtained"
