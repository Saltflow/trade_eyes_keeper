"""Actual 2018 Q3 originals: YTD versus single-quarter financial columns."""

import json
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import extract_field, locate_sections
from src.fundamental_data.disclosure.contract import FIELDS

ROOT = Path(__file__).parent / "fixtures/fr_quarterly_ytd"


def source(code):
    return json.loads((ROOT / (code + "_2018q3.json")).read_text(encoding="utf8"))


@pytest.mark.parametrize(
    "code,metric,amount,comparative",
    [
        ("000568", "net_profit_parent", "2751035356.64", "1997394867.98"),
        ("000568", "cfo", "2262542724.30", "2236659612.59"),
        ("000568", "capex", "506764979.75", "842396163.98"),
        ("600519", "net_profit_parent", "24733552720.33", "19983846984.10"),
        ("600519", "cfo", "28221285915.54", "22786767198.19"),
        ("600519", "capex", "1016477926.63", "756994453.84"),
    ],
)
def test_original_quarterly_ytd_values(code, metric, amount, comparative):
    s = source(code)
    result = extract_field(
        metric, FIELDS[metric], locate_sections(s["pages"]), s["metadata"]
    )
    assert result["status"] == "obtained"
    assert result["amount"] == amount
    assert result["comparative_amount"] == comparative
    assert result["period_end"] == "2018-09-30"
    assert result["comparative_period_end"] == "2017-09-30"
    if code == "600519" and metric == "net_profit_parent":
        assert result["selected_amount_columns"] == [2, 3]


def test_single_quarter_table_never_substitutes_for_missing_ytd():
    s = source("000568")
    sections = [
        section
        for section in locate_sections(s["pages"])
        if section.get("flow_period_basis") != "year_to_date"
    ]
    result = extract_field(
        "net_profit_parent", FIELDS["net_profit_parent"], sections, s["metadata"]
    )
    assert result["status"] == "not_located"
    assert (
        result["scope_exclusions"][0]["reason"]
        == "single_quarter_statement_not_year_to_date"
    )


def test_missing_ytd_header_cannot_select_last_two_columns():
    s = source("600519")
    s["pages"] = {
        p: text.replace("年初至报告期", "无法识别期间")
        for p, text in s["pages"].items()
    }
    result = extract_field(
        "net_profit_parent",
        FIELDS["net_profit_parent"],
        locate_sections(s["pages"]),
        s["metadata"],
    )
    assert result["status"] != "obtained"


def test_sparse_four_column_row_cannot_become_two_column_current_value():
    s = source("600519")
    sections = locate_sections(s["pages"])
    section = next(x for x in sections if x["kind"] == "income")
    for row in section["lines"]:
        if "24,733,552,720.33" in row["text"]:
            row["text"] = (
                row["text"]
                .replace("24,733,552,720.33", "")
                .replace("19,983,846,984.10", "")
            )
    result = extract_field(
        "net_profit_parent", FIELDS["net_profit_parent"], [section], s["metadata"]
    )
    assert result["status"] != "obtained"


@pytest.mark.parametrize(
    "period,metric,amount",
    [
        ("2017-06-30", "net_profit_parent", "1467043514.56"),
        ("2017-06-30", "parent_equity", "11143967255.37"),
        ("2017-06-30", "cfo", "1347676719.19"),
        ("2017-06-30", "capex", "315905067.50"),
        ("2017-12-31", "net_profit_parent", "2557944598.97"),
        ("2017-12-31", "parent_equity", "15171448756.68"),
        ("2017-12-31", "cfo", "3693407461.89"),
        ("2017-12-31", "capex", "1415996563.84"),
    ],
)
def test_original_fy_h1_relative_columns_use_verified_report_period(
    period, metric, amount
):
    s = json.loads((ROOT / (period + "_old_report.json")).read_text(encoding="utf8"))
    result = extract_field(
        metric, FIELDS[metric], locate_sections(s["pages"]), s["metadata"]
    )
    assert result["status"] == "obtained"
    assert result["amount"] == amount
    assert result["column_period_evidence"]


@pytest.mark.parametrize("damage", ["cover_removed", "wrong_period", "reverse_columns"])
def test_relative_year_columns_require_period_and_current_first_proof(damage):
    s = json.loads((ROOT / "2017-12-31_old_report.json").read_text(encoding="utf8"))
    if damage == "cover_removed":
        for page in ("1", "2", "3"):
            s["pages"][page] = ""
    elif damage == "wrong_period":
        s["metadata"]["period_end"] = "2018-12-31"
    else:
        s["pages"] = {
            page: text.replace("本期发生额 上期发生额", "上期发生额 本期发生额")
            for page, text in s["pages"].items()
        }
    result = extract_field(
        "cfo", FIELDS["cfo"], locate_sections(s["pages"]), s["metadata"]
    )
    assert result["status"] != "obtained"


@pytest.mark.parametrize(
    "fixture,metric,amount",
    [
        ("000498_2017-12-31_old_report.json", "parent_equity", "4014415752.95"),
        ("000498_2017-12-31_old_report.json", "cfo", "232437214.69"),
        ("000498_2017-12-31_old_report.json", "capex", "231789505.98"),
        ("600502_2018-06-30_old_report.json", "capex", "210610568.61"),
    ],
)
def test_real_duplicate_headings_and_old_name_page_header(fixture, metric, amount):
    s = json.loads((ROOT / fixture).read_text(encoding="utf8"))
    result = extract_field(
        metric, FIELDS[metric], locate_sections(s["pages"]), s["metadata"]
    )
    assert result["status"] == "obtained" and result["amount"] == amount


@pytest.mark.parametrize("damage", [None, "different_y", "wrong_hash", "no_layout"])
def test_real_split_amounts_require_same_pdf_row_geometry(damage):
    frozen = json.loads(
        (ROOT / "cccc_2017h1_split_pair.json").read_text(encoding="utf8")
    )
    s, layout = frozen["source"], frozen["layout"]
    if damage == "different_y":
        row = next(row for row in layout["pages"]["60"]["lines"] if row["line"] == 36)
        row["amounts"][0]["bbox"][1] += 5
    elif damage == "wrong_hash":
        layout["pdf_sha256"] = "wrong"
    elif damage == "no_layout":
        layout = None
    result = extract_field(
        "capex", FIELDS["capex"], locate_sections(s["pages"]), s["metadata"], layout
    )
    if damage:
        assert result["status"] != "obtained"
    else:
        assert result["amount"] == "15668941501"
        assert result["comparative_amount"] == "21638557857"
        assert (
            result["coordinate_evidence"]["method"]
            == "split_text_same_pdf_row_two_columns/1"
        )
