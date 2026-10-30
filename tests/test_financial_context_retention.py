"""Stage-two display retention includes locations, exceptions and non-cell data."""

import socket
from copy import deepcopy

import pytest

from src.fundamental_data.model_context import assemble, audit, reading
from tests.test_financial_model_context import record


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network attempted during financial-context retention tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def escaped(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("\n", "<br>")
    )


def render(entries):
    before = deepcopy(entries)
    plan = assemble(entries)
    text, cells, _ = reading(entries, plan)
    result = audit(entries, plan, cells, text)
    assert result["passed"], result["errors"]
    assert result["body_positions_checked"]
    assert entries == before
    return plan, text, cells, result


def assert_positions(text, cells):
    for cell in cells:
        start, end = cell["span"]
        row_start, row_end = cell["row_span"]
        header_start, header_end = cell["header_span"]
        assert 0 <= header_start < header_end <= row_start <= start < end <= row_end
        assert text[start:end] == escaped(cell["display"])
        assert text[row_start:row_end] == cell["row_display"]
        assert text[header_start:header_end] == cell["header_display"]
        assert cell["row_display"].startswith("|")
        assert cell["header_display"].startswith("|")


def test_numeric_column_and_original_column_key_cell_exceptions_are_visible():
    data = record(row_count=1)
    table = data["tables"][0]
    table["columns"]["v"]["context"] = {"unit": "千元", "currency": "USD"}
    table["rows"][0]["cell_context"] = {
        "v": {"currency": "EUR", "period": {"kind": "instant", "end": "2023-06-30"}}
    }
    plan, text, cells, _ = render([("2023", data)])
    assert plan["sections"][0]["aligned"]
    assert "千元" in text and "USD" in text
    assert "EUR" in text and "2023-06-30" in text
    assert_positions(text, cells)


def test_unicode_and_markdown_escaped_cells_have_actual_character_spans():
    data = record(row_count=1)
    data["tables"][0]["rows"][0]["path"] = ["中文项目甲"]
    data["tables"][0]["rows"][0]["values"]["v"] = "甲|乙\\丙\n丁"
    _, text, cells, _ = render([("2023", data)])
    assert_positions(text, cells)
    assert "甲\\|乙\\\\丙<br>丁" in text


@pytest.mark.parametrize("corruption", ["delete_row", "number_only", "wrong_header"])
def test_same_value_elsewhere_cannot_mask_missing_row_or_column(corruption):
    data = record(row_count=2)
    for row in data["tables"][0]["rows"]:
        row["values"]["v"] = "123.45"
    entries = [("2023", data)]
    plan, text, cells, _ = render(entries)
    assert len({tuple(cell["row_span"]) for cell in cells}) == 2
    assert_positions(text, cells)
    if corruption == "delete_row":
        start, end = cells[0]["row_span"]
        corrupted = text[:start] + text[end:]
        assert "123.45" in corrupted
    elif corruption == "number_only":
        corrupted = "123.45"
    else:
        start, end = cells[0]["header_span"]
        header = text[start:end]
        assert "2023" in header
        corrupted = text[:start] + header.replace("2023", "2099") + text[end:]
    result = audit(entries, plan, cells, corrupted)
    assert not result["passed"]
    assert result["errors"]


def test_cell_location_cannot_be_redirected_to_an_equal_value_in_another_row():
    data = record(row_count=2)
    for row in data["tables"][0]["rows"]:
        row["values"]["v"] = "123.45"
    entries = [("2023", data)]
    plan, text, cells, _ = render(entries)
    corrupted = deepcopy(cells)
    corrupted[0]["span"] = list(cells[1]["span"])
    assert not audit(entries, plan, corrupted, text)["passed"]


def test_empty_value_row_keeps_its_issues_and_other_original_fields():
    data = record(row_count=2)
    empty = data["tables"][0]["rows"][0]
    empty["values"] = {}
    empty["issues"] = [{"detail": "空分组说明仍须保留"}]
    empty["extra_original"] = "空行的额外原字段"
    _, text, _, result = render([("2023", data)])
    assert result["input_cells"] == 1
    assert "空分组说明仍须保留" in text
    assert "空行的额外原字段" in text


def test_unused_declared_column_keeps_label_and_context_without_inventing_cell():
    data = record(row_count=1)
    data["tables"][0]["columns"]["unused"] = {
        "label": "原表未使用列定义",
        "identity_code": "VALUE",
        "period": {"kind": "instant", "end": "2023-03-31"},
        "context": {"currency": "JPY", "unit": "百万元"},
        "original_note": "未使用列的原说明",
    }
    _, text, _, result = render([("2023", data)])
    assert result["input_cells"] == 1
    for retained in (
        "原表未使用列定义",
        "2023-03-31",
        "JPY",
        "百万元",
        "未使用列的原说明",
    ):
        assert retained in text


def test_different_scope_prevents_cross_year_table_merging():
    first, second = record(row_count=1), record(2024, row_count=1)
    second["tables"][0]["scope"] = {
        "kind": "parent",
        "entity_code": "parent",
        "entity": "示例母公司",
    }
    plan, text, cells, result = render([("2023", first), ("2024", second)])
    assert len(plan["sections"]) == 2
    assert "集团" in text and "母公司" in text and "示例母公司" in text
    assert result["input_cells"] == 2
    assert_positions(text, cells)


def test_different_units_and_statistics_are_not_merged_or_silently_inherited():
    first, second = record(row_count=1), record(2024, row_count=1)
    row = second["tables"][0]["rows"][0]
    row.update(unit="万元", unit_code="CURRENCY_10000", currency="USD")
    row["statistic"] = {
        "kind": "period_average",
        "method": {"state": "stated", "text": "日均"},
    }
    plan, text, cells, result = render([("2023", first), ("2024", second)])
    assert len(plan["sections"]) == 1
    assert len(plan["sections"][0]["rows"]) == 2
    for retained in ("万元", "CNY", "USD", "日均"):
        assert retained in text
    assert result["input_cells"] == 2
    assert_positions(text, cells)


@pytest.mark.parametrize("later_value", ["100.00", "150.00"])
def test_same_actual_period_preserves_both_disclosure_versions(later_value):
    first, second = record(row_count=1), record(2024, row_count=1)
    first["tables"][0]["rows"][0]["values"]["v"] = "100.00"
    second["tables"][0]["rows"][0]["values"]["v"] = later_value
    second["tables"][0]["columns"]["v"]["period"] = deepcopy(
        first["tables"][0]["columns"]["v"]["period"]
    )
    entries = [("2023", first), ("2024", second)]
    plan, text, cells, result = render(entries)
    assert len(plan["sections"]) == 1
    assert result["input_cells"] == result["displayed_input_cells"] == 2
    assert {tuple(origin) for cell in cells for origin in cell["origins"]} == {
        ("2023", 0, 0, "v"),
        ("2024", 0, 0, "v"),
    }
    assert "FY2023" in text and "FY2024" in text
    assert "100.00" in text and later_value in text
    assert_positions(text, cells)
