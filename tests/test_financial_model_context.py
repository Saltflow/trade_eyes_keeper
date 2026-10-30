"""Stage two keeps cells and semantics without row-dependent column growth."""

import socket
from copy import deepcopy
from pathlib import Path

import pytest

from src.fundamental_data.model_context import assemble, audit, reading


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network attempted during stage-two acceptance")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def record(year=2023, row_count=2):
    column = {
        "label": f"{year}年末",
        "identity_code": "VALUE",
        "period": {"kind": "instant", "start": None, "end": f"{year}-12-31"},
        "adjustment": {"status": "original", "label": None},
        "reported_in": {"report_id": str(year), "report_period": f"FY{year}"},
    }
    rows = [
        {
            "path": ["科目" + str(i)],
            "row_kind": "detail",
            "identity_code": "ROW_" + str(i),
            "unit": "元",
            "unit_code": "CURRENCY_1",
            "currency": "CNY",
            "dimensions": [],
            "statistic": {"kind": "point"},
            "values": {"v": str(year + i)},
            "missing_reasons": {},
            "period_overrides": {},
            "issues": [],
        }
        for i in range(row_count)
    ]
    table = {
        "kind": "numeric",
        "role": "BS",
        "title": "合并资产负债表",
        "identity_code": "BS",
        "scope": {"kind": "group", "entity_code": "group", "entity": "示例公司"},
        "accounting_basis_code": "CAS",
        "accounting_basis": "企业会计准则",
        "columns": {"v": column},
        "rows": rows,
        "issues": [],
    }
    return {
        "schema_version": "4.0",
        "company": {"name": "示例公司", "code": "TEST"},
        "tables": [table],
        "policies": [],
        "unstructured_blocks": {},
    }


def run(entries):
    plan = assemble(entries)
    text, cells, stats = reading(entries, plan)
    result = audit(entries, plan, cells, text)
    assert result["passed"], result["errors"]
    return plan, text, result, stats


def test_three_years_align_without_losing_versions_or_cells():
    plan, text, result, _ = run([(str(y), record(y)) for y in (2023, 2024, 2025)])
    section = plan["sections"][0]
    assert len(plan["sections"]) == 1
    assert len(section["rows"]) == 2
    assert len(section["columns"]) == 3
    assert result["input_cells"] == 6
    assert "2023-12-31时点〔FY2023披露〕" in text


@pytest.mark.parametrize("count", [1, 100])
def test_ambiguous_measure_columns_stay_two_for_any_number_of_rows(count):
    data = record(row_count=count)
    table = data["tables"][0]
    table["columns"]["w"] = deepcopy(table["columns"]["v"])
    table["columns"]["v"]["label"] = "账面余额"
    table["columns"]["w"]["label"] = "坏账准备"
    for row in table["rows"]:
        row["values"]["w"] = "3.10%"
    plan, text, result, _ = run([("2023", data)])
    assert len(plan["sections"][0]["columns"]) == 2
    assert not plan["sections"][0]["aligned"]
    assert result["input_cells"] == count * 2
    assert "3.10%" in text and "不逐行造列" in text


def test_different_subtables_same_catalog_identity_never_share_a_matrix():
    data = record()
    table = deepcopy(data["tables"][0])
    table["title"] = "现金流量补充资料"
    data["tables"].append(table)
    plan, _, _, _ = run([("2023", data)])
    assert len(plan["sections"]) == 2


def test_declared_accounting_code_matches_display_variants_but_not_regulatory_definitions():
    first, second = record(), record(2024)
    second["tables"][0]["accounting_basis"] = "中国企业会计准则"
    plan, text, _, _ = run([("2023", first), ("2024", second)])
    assert len(plan["sections"]) == 1
    assert "原表述" in text and "2024=中国企业会计准则" in text
    for item in (first, second):
        item["tables"][0]["accounting_basis_code"] = "REGULATORY"
    assert len(assemble([("2023", first), ("2024", second)])["sections"]) == 2


def test_unknown_company_does_not_block_other_years_or_get_filled():
    data = record(2024)
    data["company"] = {"name": None, "code": None}
    plan, text, result, _ = run([("2023", record()), ("2024", data)])
    assert len(plan["sections"]) == 2
    assert "公司身份未定" in text
    assert data["company"] == {"name": None, "code": None}
    assert result["input_cells"] == 4


def test_explicit_unknown_period_missing_reasons_and_unmapped_keys_are_retained():
    data = record()
    row = data["tables"][0]["rows"][0]
    row["period_overrides"]["v"] = None
    row["values"].update(v=None, orphan="10–20%")
    row["missing_reasons"]["v"] = {
        "reason": "context_uncertain",
        "detail": "期间无法确定",
    }
    plan, text, result, _ = run([("2023", data)])
    assert not plan["sections"][0]["aligned"]
    assert "期间未定" in text and "期间无法确定" in text and "10–20%" in text
    assert result["input_cells"] == 3


def test_mixed_point_and_duration_overrides_keep_their_actual_periods():
    data = record()
    data["tables"][0]["rows"][1]["period_overrides"]["v"] = {
        "kind": "duration",
        "start": "2023-01-01",
        "end": "2023-12-31",
    }
    plan, text, _, _ = run([("2023", data)])
    assert len(plan["sections"][0]["columns"]) == 2
    assert "2023-01-01至2023-12-31" in text
    assert "2023-12-31时点" in text


def test_repeated_row_identity_is_not_paired_or_hidden():
    data = record()
    data["tables"][0]["rows"][1]["identity_code"] = "ROW_0"
    plan, text, result, _ = run([("2023", data)])
    assert len(plan["sections"][0]["rows"]) == 2
    assert len(plan["sections"][0]["columns"]) == 1
    assert "行身份未定或重复，独立保留" in text
    assert result["input_cells"] == 2


def test_context_is_common_once_with_column_and_cell_exceptions():
    data = record(row_count=1)
    table = data["tables"][0]
    table["kind"] = "attributes"
    table["context"] = {"unit": "元", "currency": "CNY"}
    table["columns"] = {
        "v": {"label": "金额", "value_type": "amount", "context": {}},
        "p": {"label": "比例", "value_type": "ratio", "context": {"unit": "%"}},
    }
    table["rows"][0]["values"] = {"v": "123.00", "p": "10%"}
    table["rows"][0]["cell_context"] = {
        "p": {"period": {"kind": "instant", "end": "2023-06-30"}}
    }
    _, text, _, _ = run([("2023", data)])
    assert text.count("币种：CNY") == 1
    assert "字段口径「比例」：单位：%" in text
    assert "2023-06-30" in text


def test_policy_exact_text_reuses_years_but_different_text_and_scope_do_not():
    first, second = record(), record(2024)
    policy = {
        "topic": "revenue",
        "text": "完全相同的政策原文",
        "scope": {"kind": "group"},
        "issues": [],
    }
    first["policies"] = [policy]
    second["policies"] = [{**policy, "issues": [{"detail": "额外说明"}]}]
    second["policies"].append({**policy, "text": "类似但不同的政策原文"})
    second["policies"].append({**policy, "scope": {"kind": "parent"}})
    _, text, _, stats = run([("2023", first), ("2024", second)])
    assert stats["policies"] == {"input": 4, "versions": 3}
    assert text.count("完全相同的政策原文") == 2
    assert "额外说明" in text


def test_audit_detects_corruption_and_omitted_display_cells():
    entries = [("2023", record())]
    plan = assemble(entries)
    _, cells, _ = reading(entries, plan)
    cells[0]["observation"]["value"] = "CORRUPTED"
    assert not audit(entries, plan, cells)["passed"]
    _, cells, _ = reading(entries, assemble(entries))
    assert not audit(entries, assemble(entries), cells[:-1])["passed"]


def test_no_frozen_extractor_imports_or_network_dependencies():
    path = Path(__file__).parents[1] / "src/fundamental_data/model_context.py"
    text = path.read_text(encoding="utf8")
    assert "v2_" not in text
    assert "requests" not in text and "socket" not in text


def test_existing_nine_reports_have_no_cell_loss_or_source_mutation():
    import hashlib
    import json
    import os

    configured = os.environ.get("TRADE_EYES_FINANCIAL_CONTEXT_CORPUS")
    root = Path(configured) if configured else (
        Path(__file__).parents[1]
        / "data/fundamental_data/three_companies_fixed4_20261024_115306/stage1"
    )
    if not configured and not root.is_dir():
        pytest.skip("Historical nine-report corpus is separate from source; set TRADE_EYES_FINANCIAL_CONTEXT_CORPUS to validate it")
    paths = sorted(root.glob("*/data.json"))
    assert len(paths) == 9
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for code in ("600039", "600820", "000568"):
        entries = [
            (p.parent.name, json.loads(p.read_text(encoding="utf8")))
            for p in paths
            if p.parent.name.startswith(code)
        ]
        _, text, result, _ = run(entries)
        assert "~~~json" not in text
        assert result["input_cells"] > 7000
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
