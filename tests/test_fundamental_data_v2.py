"""Contract, persistence and offline assembly tests; real network is forbidden."""

import csv
import hashlib
import json
import socket
from collections import Counter
from copy import deepcopy
from html.parser import HTMLParser
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from jsonschema import Draft202012Validator

from src.fundamental_data import v2_history, v2_schema, v2_transport, v2_view
from src.fundamental_data.v2_history import assemble
from src.fundamental_data.v2_schema import (
    attribute_context,
    combine_blocks,
    parse,
    peer_errors,
    peer_input,
    validate,
)
from src.fundamental_data.v2_view import publish

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/fundamental_data_v2.json").read_text("utf8"))
SPEC = spec_from_file_location(
    "structured_cli", ROOT / "scripts/build_fundamental_data_v2.py"
)
CLI = module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Real HTTP/socket connection attempted in offline acceptance")

    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def period(year=2025, instant=False):
    return {
        "kind": "instant" if instant else "duration",
        "start": None if instant else f"{year}-01-01",
        "end": f"{year}-12-31",
        "coverage": "not_applicable" if instant else "full_year",
    }


def scope(kind="group"):
    return {
        "kind": kind,
        "entity": "Company A",
        "entity_code": kind if kind in {"group", "parent"} else "unknown",
    }


def statistic(kind="point"):
    return {
        "kind": kind,
        "method": {"state": "not_applicable", "text": None},
        "annualized": "not_applicable",
    }


def numeric(year=2025, value="123.456789", role="BS"):
    if role == "capital":
        template = next(
            t for t in EXAMPLES["capital"]["tables"] if t["kind"] == "numeric"
        )
    else:
        template = next(
            t
            for t in EXAMPLES["statements"]["tables"]
            if t["role"] == (role if role in {"BS", "IS", "CFS"} else "BS")
        )
    table = deepcopy(template)
    row = deepcopy(next(row for row in table["rows"] if row["row_kind"] != "heading"))
    column = deepcopy(next(iter(table["columns"].values())))
    instant = role == "BS" or role == "capital"
    column.update(
        label=str(year),
        period=period(year, instant),
        adjustment={"status": "original", "label": None},
        comparison=None,
        identity_code="VALUE",
    )
    column.pop("reported_in", None)
    row.update(values={"c1": value}, missing_reasons={}, issues=[], period_overrides={})
    row["statistic"] = statistic("point" if instant else "period_total")
    row["identity_code"] = {
        "BS": "BS_CASH",
        "IS": "IS_REVENUE",
        "CFS": "CFS_NET_OPERATING",
    }.get(role, row.get("identity_code"))
    row["unit_code"] = row.get("unit_code", "CURRENCY_1E6")
    table.update(
        role=role,
        scope=scope(),
        accounting_basis="企业会计准则",
        accounting_basis_code="CAS",
        columns={"c1": column},
        rows=[row],
        issues=[],
    )
    table["identity_code"] = (
        role
        if role in {"BS", "IS", "CFS"}
        else "CAPITAL_POINT"
        if role == "capital"
        else table.get("identity_code")
    )
    return table


def attributes():
    return {
        "kind": "attributes",
        "identity_code": None,
        "accounting_basis_code": "CAS",
        "module": "common",
        "topic": "consolidation",
        "title": "子公司",
        "part": None,
        "scope": scope(),
        "accounting_basis": "企业会计准则",
        "context": {"currency": "CNY", "unit": "百万元"},
        "columns": {
            "c1": {
                "label": "注册资本",
                "value_type": "amount",
                "context": {"unit": "万元"},
            }
        },
        "rows": [
            {
                "path": ["子公司甲"],
                "values": {"c1": "100"},
                "missing_reasons": {},
                "cell_context": {"c1": {"currency": "HKD"}},
                "issues": [],
            }
        ],
        "issues": [],
    }


def parts(year=2025):
    result = {}
    for name in CONFIG["base_blocks"]:
        properties = CONFIG[name + "_schema"]["properties"]
        result[name] = {key: [] for key in properties if key != "schema_version"}
        result[name]["schema_version"] = CONFIG["schema_version"]
        result[name]["issues"] = [
            {
                "target": "topics/" + topic,
                "reason": "not_found",
                "detail": "本测试仅验证三表",
            }
            for topic in CONFIG["jobs"][name]["topics"]
        ]
    result["statements"].update(
        company={"name": "Company A", "code": "000001"},
        report_period=f"FY{year}",
        tables=[numeric(year, role=role) for role in ("BS", "IS", "CFS")],
    )
    result["metadata"]["metadata"] = [
        {
            "period": period(year),
            "scope": scope(),
            "industry_classifications": [],
            "businesses": ["工程"],
            "products_services": [],
            "operating_model": [],
            "customers": [],
            "geographies": [],
            "share_classes": [],
            "accounting_basis": "企业会计准则",
            "fiscal_year_end": "12-31",
            "issues": [],
        }
    ]
    result["capital"] = json.loads(
        json.dumps(EXAMPLES["capital"], ensure_ascii=False)
        .replace("2025", str(year))
        .replace("示例集团（虚构）", "Company A")
    )
    return result


def record(year=2025, report_id=None):
    return combine_blocks(
        CONFIG, parts(year), "Company A", year, report_id=report_id or f"report-{year}"
    )


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf8")
    return path


def response(data=None, raw=None, finish="stop", status=200):
    body = {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": raw
                    if raw is not None
                    else json.dumps(data, ensure_ascii=False)
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError("mock HTTP failure")

    return SimpleNamespace(
        status_code=status,
        content=json.dumps(body, ensure_ascii=False).encode(),
        raise_for_status=raise_for_status,
    )


def recording(tmp_path, values=None, finish="stop"):
    source = tmp_path / "recorded"
    source.mkdir()
    dump(source / "summary.json", {"pdf_pages": 1})
    for name, value in (values or parts()).items():
        folder = source / "blocks" / name
        dump(folder / "request.json", {"messages": [], "max_tokens": 32768})
        (folder / "response.body").write_bytes(response(value, finish=finish).content)
        dump(
            folder / "receipt.json",
            {"http_status": 200, "usage": {"prompt_tokens": 100}},
        )
    return source


def hashes(folder):
    return {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in folder.rglob("*")
        if path.is_file()
    }


def all_tables(data):
    return [table for table in data.get("tables", []) if table.get("kind") == "numeric"]


def forbid_business_audit(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Business audit was executed inside the retention pipeline")

    for module in (CLI, v2_schema, v2_history, v2_view):
        for name in ("validate", "table_notes", "item_errors", "_table_errors"):
            monkeypatch.setattr(module, name, forbidden, raising=False)


def test_schemas_are_self_consistent():
    for name, schema in CONFIG.items():
        if name.endswith("_schema"):
            Draft202012Validator.check_schema(schema)


def test_peer_example_matches_final_schema():
    assert validate(CONFIG, "peers", CONFIG["peers_example"]) == []


def test_history_package_matches_its_own_derived_schema():
    history = assemble([record(2024), record(2025)])
    assert validate(CONFIG, "history", history) == []


EXAMPLE_DIR = ROOT / "tests/fixtures/fundamental_data_v2_contract"
EXAMPLES = json.loads((EXAMPLE_DIR / "examples.json").read_text("utf8"))["blocks"]
INVALID_CASES = json.loads((EXAMPLE_DIR / "invalid_cases.json").read_text("utf8"))[
    "cases"
]


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_all_frozen_prompt_examples_match_schema_and_semantics(name):
    assert validate(CONFIG, name, EXAMPLES[name]) == []


@pytest.mark.parametrize("case", INVALID_CASES, ids=lambda case: case["name"])
def test_published_invalid_examples_are_rejected(case):
    assert validate(CONFIG, case["stage"], case["data"])


def test_topics_have_one_owner_and_all_financial_profiles_are_present():
    assert {
        "bank",
        "insurance",
        "securities",
        "futures",
        "asset_management",
        "trust",
        "financial_leasing",
        "credit_finance",
        "amc_guarantee",
        "financial_infrastructure",
    } <= set(CONFIG["industry_profiles"])
    for key in ("topic_catalog", "industry_topics"):
        for topic, declaration in CONFIG[key].items():
            owners = [
                name for name, job in CONFIG["jobs"].items() if topic in job["topics"]
            ]
            assert owners == [declaration["owner"]]
    for profile in CONFIG["industry_profiles"].values():
        owned = {
            topic
            for name in profile["blocks"]
            for topic in CONFIG["jobs"][name]["topics"]
        }
        assert owned == set(profile["topics"])


def test_instruction_and_examples_are_in_actual_payload():
    for name in ("statements", "capital", "notes_bank_2", "metadata"):
        request = v2_transport.payload(
            CONFIG, name, [{"type": "text", "text": "untrusted PDF"}]
        )
        text = "\n".join(
            block["text"]
            for block in request["messages"][-1]["content"]
            if block["type"] == "text"
        )
        assert CONFIG["chunk_common_prompt"] in request["messages"][0]["content"]
        assert CONFIG[name + "_prompt"] in text
        if name != "metadata":
            assert '"c1"' in text
        assert "虚构" in text
        assert request["max_tokens"] == CONFIG["jobs"][name]["max_tokens"]


def test_phase1_uses_reviewed_output_limits_and_enabled_thinking():
    for name in CONFIG["base_blocks"] + CONFIG["industry_profiles"]["bank"]["blocks"]:
        request = v2_transport.payload(CONFIG, name, [])
        assert request["max_tokens"] == CONFIG["jobs"][name]["max_tokens"]
        assert request["max_tokens"] <= 131072
        assert request["thinking"] == {"type": "enabled"}


def test_new_contract_and_explicit_column_refs_are_required():
    block = parts()["statements"]
    assert validate(CONFIG, "statements", block) == []
    legacy = deepcopy(block)
    legacy["tables"][0]["columns"] = ["FY2025"]
    assert validate(CONFIG, "statements", legacy)


def test_capital_minimum_items_are_covered_by_explicit_tags():
    capital = deepcopy(EXAMPLES["capital"])
    assert validate(CONFIG, "capital", capital) == []
    for table in capital["tables"]:
        for row in table.get("rows", []):
            if row.get("requested_item") == "扣非ROE":
                row["requested_item"] = None
    assert validate(CONFIG, "capital", capital)


@pytest.mark.parametrize("date_value", ["2025", "2025-12", "2025-12-31"])
def test_reported_date_precision_is_kept_without_inventing_dates(date_value):
    block = parts()["statements"]
    column = block["tables"][0]["columns"]["c1"]
    column["period"]["end"] = date_value
    assert validate(CONFIG, "statements", block) == []
    assert column["period"]["end"] == date_value


@pytest.mark.parametrize("date_value", ["2025-13", "2025-02-30", "tomorrow"])
def test_invalid_dates_are_structure_errors(date_value):
    block = parts()["statements"]
    block["tables"][0]["columns"]["c1"]["period"]["end"] = date_value
    assert validate(CONFIG, "statements", block)


@pytest.mark.parametrize("location", ["metadata", "attribute_context"])
def test_period_validation_applies_to_all_contexts(location):
    if location == "metadata":
        data = parts()["metadata"]
        data["metadata"][0]["period"]["end"] = "2025-02-30"
        stage = "metadata"
    else:
        table = attributes()
        table["context"]["period"] = period()
        table["context"]["period"]["end"] = "2025-02-30"
        data = {
            "schema_version": CONFIG["schema_version"],
            "tables": [table],
            "policies": [],
            "changes": [],
            "issues": [],
        }
        stage = "notes"
    assert validate(CONFIG, stage, data)


@pytest.mark.parametrize(
    "value", ["22.51%", "10-45", "—", "二审已审结", "0", "(1,234.50)"]
)
def test_model_scalars_are_preserved_without_numeric_normalization(tmp_path, value):
    block = parts()
    block["statements"]["tables"][0]["rows"][0]["values"]["c1"] = value
    before = deepcopy(block)
    data = combine_blocks(CONFIG, block, report_id="r1")
    publish(tmp_path, data, CONFIG["labels"])
    assert block == before
    assert value in (tmp_path / "阅读.md").read_text("utf8")
    assert value in (tmp_path / "data.json").read_text("utf8")


@pytest.mark.parametrize(
    "reason", ["not_applicable", "not_disclosed", "not_found", "context_uncertain"]
)
def test_null_has_explicit_reason(reason):
    block = parts()["statements"]
    row = block["tables"][0]["rows"][0]
    row["values"]["c1"] = None
    row["missing_reasons"]["c1"] = {"reason": reason, "detail": "测试原因"}
    assert validate(CONFIG, "statements", block) == []
    del row["missing_reasons"]["c1"]
    assert validate(CONFIG, "statements", block)


@pytest.mark.parametrize(
    "mutation", ["unknown_ref", "missing_ref", "spurious_reason", "missing_method"]
)
def test_semantic_structure_errors_are_reported(mutation):
    block = parts()["statements"]
    row = block["tables"][0]["rows"][0]
    if mutation == "unknown_ref":
        row["values"]["c2"] = "orphan-value"
    elif mutation == "missing_ref":
        row["values"] = {}
    elif mutation == "spurious_reason":
        row["missing_reasons"]["c1"] = {"reason": "not_found", "detail": None}
    else:
        row["statistic"]["method"] = {"state": "stated", "text": None}
    assert validate(CONFIG, "statements", block)


def test_display_uses_refs_instead_of_values_dictionary_order(tmp_path):
    data = record()
    table = all_tables(data)[0]
    table["columns"]["c2"] = deepcopy(table["columns"]["c1"])
    table["columns"]["c2"]["label"] = "FY2024"
    table["columns"]["c1"]["label"] = "FY2025"
    table["rows"][0]["values"] = {"c2": "old-value", "c1": "new-value"}
    publish(tmp_path, data, CONFIG["labels"])
    text = (tmp_path / "阅读.md").read_text("utf8")
    row_line = next(line for line in text.splitlines() if "old-value" in line)
    assert row_line.index("new-value") < row_line.index("old-value")


def test_legacy_wrong_columns_and_orphan_values_remain_raw(tmp_path):
    data = {
        "financials": {
            "BS": {
                "title": "旧表",
                "columns": ["项目", "2025Q4"],
                "rows": [{"item": "流动性覆盖率", "values": ["175.51%"]}],
            }
        }
    }
    original = deepcopy(data)
    publish(tmp_path, data, CONFIG["labels"])
    assert data == original
    assert parse((tmp_path / "data.json").read_text("utf8")) == original
    text = (tmp_path / "阅读.md").read_text("utf8")
    assert "175.51%" in text
    assert "项目" in text and "2025Q4" in text
    assert "n/a" not in text


def test_unknown_units_are_not_parsed_from_names(tmp_path):
    data = record()
    row = all_tables(data)[0]["rows"][0]
    row["path"] = ["货币资金（人民币百万元）"]
    row["unit"] = None
    row["issues"] = [
        {"target": "unit", "reason": "context_uncertain", "detail": "未定"}
    ]
    publish(tmp_path, data, CONFIG["labels"])
    saved = parse((tmp_path / "data.json").read_text("utf8"))
    assert all_tables(saved)[0]["rows"][0]["unit"] is None


def test_attribute_unit_currency_context_is_explicit_and_not_guessed(tmp_path):
    data = record()
    table = attributes()
    assert (
        validate(
            CONFIG,
            "notes",
            {
                "schema_version": CONFIG["schema_version"],
                "tables": [table],
                "policies": [],
                "changes": [],
                "issues": [],
            },
        )
        == []
    )
    data["tables"].append(table)
    before = deepcopy(data)
    publish(tmp_path, data, CONFIG["labels"])
    text = (tmp_path / "阅读.md").read_text("utf8")
    assert "HKD" in text and "万元" in text
    assert data == before
    effective = attribute_context(table, table["rows"][0], "c1")
    assert effective["unit"] == "万元" and effective["currency"] == "HKD"
    assert effective["scope"] == scope()


def test_history_keeps_report_versions_and_restatement():
    older, newer = record(2024), record(2025)
    old = all_tables(older)[0]
    new = all_tables(newer)[0]
    old["rows"][0]["values"]["c1"] = "100"
    new["columns"]["c1"]["period"] = period(2024, True)
    new["columns"]["c1"]["adjustment"] = {"status": "restated", "label": "重述"}
    new["rows"][0]["values"]["c1"] = "98"
    before = deepcopy([older, newer])
    result = assemble([older, newer])
    table = next(t for t in all_tables(result) if t.get("role") == "BS")
    assert set(table["rows"][0]["values"].values()) == {"100", "98"}
    assert len(table["columns"]) == 2
    assert {c["reported_in"]["report_id"] for c in table["columns"].values()} == {
        "report-2024",
        "report-2025",
    }
    assert [older, newer] == before


@pytest.mark.parametrize(
    "difference",
    ["path", "unit", "currency", "scope", "statistic", "annualized", "unknown"],
)
def test_history_does_not_guess_row_equivalence(difference):
    left, right = record(2024), record(2025)
    table = all_tables(right)[0]
    row = table["rows"][0]
    if difference == "path":
        row["path"] = ["资产", "现金及现金等价物"]
        row["identity_code"] = None
    elif difference in {"unit", "currency"}:
        row[difference] = "万元" if difference == "unit" else "HKD"
        if difference == "unit":
            row["unit_code"] = None
    elif difference == "scope":
        table["scope"] = scope("parent")
    elif difference == "statistic":
        row["statistic"]["kind"] = "period_average"
        row["statistic"]["method"] = {"state": "stated", "text": "简单算术平均"}
        table["columns"]["c1"]["period"] = period(2025)
    elif difference == "annualized":
        for source in (left, right):
            source_table = all_tables(source)[0]
            source_table["columns"]["c1"]["period"] = period(2025)
            source_table["rows"][0]["statistic"]["kind"] = "period_measure"
            source_table["rows"][0]["statistic"]["annualized"] = "no"
        row["statistic"]["annualized"] = "yes"
    else:
        for record_data in (left, right):
            all_tables(record_data)[0]["scope"] = {"kind": "unknown", "entity": None}
    row["values"]["c1"] = "987.654321"
    result = assemble([left, right])
    bs_rows = [
        r for t in all_tables(result) if t.get("role") == "BS" for r in t["rows"]
    ]
    assert len(bs_rows) == 2
    assert all(len(r["values"]) == 1 for r in bs_rows)


def test_parent_paths_distinguish_other_and_totals():
    data = record()
    table = all_tables(data)[0]
    base = table["rows"][0]
    table["rows"] = []
    for path, value in [
        (["贷款", "其他"], "1"),
        (["投资", "其他"], "2"),
        (["贷款", "合计"], "3"),
    ]:
        row = deepcopy(base)
        row["path"] = path
        row["values"]["c1"] = value
        row["row_kind"] = "total" if path[-1] == "合计" else "detail"
        table["rows"].append(row)
    result = assemble([data])
    merged = next(t for t in all_tables(result) if t.get("role") == "BS")
    assert [r["path"] for r in merged["rows"]] == [
        ["贷款", "其他"],
        ["投资", "其他"],
        ["贷款", "合计"],
    ]


def test_ambiguous_duplicate_rows_are_not_matched_by_occurrence_order():
    older, newer = record(2024), record(2025)
    for data, values in ((older, ["11", "12"]), (newer, ["22", "21"])):
        table = all_tables(data)[0]
        prototype = deepcopy(table["rows"][0])
        table["rows"] = []
        for value in values:
            row = deepcopy(prototype)
            row["values"] = {"c1": value}
            table["rows"].append(row)
    result = assemble([older, newer])
    rows = [r for t in all_tables(result) if t.get("role") == "BS" for r in t["rows"]]
    assert len(rows) == 4
    assert all(len(row["values"]) == 1 for row in rows)
    assert {next(iter(row["values"].values())) for row in rows} == {
        "11",
        "12",
        "21",
        "22",
    }


def test_history_uncovered_observation_is_not_rewritten_as_null(tmp_path):
    older, newer = record(2024), record(2025)
    all_tables(newer)[0]["rows"][0]["path"] = ["资产", "新增项目"]
    all_tables(newer)[0]["rows"][0]["identity_code"] = "BS_ACCOUNTS_RECEIVABLE"
    data = assemble([older, newer])
    table = next(table for table in all_tables(data) if table.get("role") == "BS")
    assert len(table["columns"]) == 2
    assert all(len(row["values"]) == 1 for row in table["rows"])
    assert all(
        value is not None for row in table["rows"] for value in row["values"].values()
    )
    publish(tmp_path, data, CONFIG["labels"])
    assert "未覆盖" in (tmp_path / "阅读.md").read_text("utf8")


def test_policy_dedup_does_not_imply_continuous_applicability():
    policy = {
        "module": "common",
        "topic": "revenue_contracts",
        "scope": scope(),
        "accounting_basis": "企业会计准则",
        "text": "相同具体政策",
        "issues": [],
    }
    records = []
    for year in (2023, 2025):
        blocks = parts(year)
        blocks["notes_operations"]["policies"] = [deepcopy(policy)]
        records.append(combine_blocks(CONFIG, blocks, report_id=f"report-{year}"))
    result = assemble(records)
    encoded = json.dumps(result.get("shared_policies"), ensure_ascii=False)
    assert "2023" in encoded and "2025" in encoded and "2024" not in encoded


def test_history_cli_is_offline_and_preserves_input_files(tmp_path, monkeypatch):
    sources = [
        dump(tmp_path / f"report-{year}.json", record(year)) for year in (2024, 2025)
    ]
    before = {path: path.read_bytes() for path in sources}
    monkeypatch.setattr(
        v2_transport,
        "dotenv_values",
        lambda *args: pytest.fail("history credentials read"),
    )
    output = tmp_path / "history"
    assert (
        CLI.main(["history", "--source", *map(str, sources), "--output", str(output)])
        == 0
    )
    saved = parse((output / "data.json").read_text("utf8"))
    assert saved["stage"] == "history"
    assert {report["report_id"] for report in saved["reports"]} == {
        "report-2024",
        "report-2025",
    }
    assert parse((output / "summary.json").read_text("utf8"))["model_calls"] == 0
    assert not (output / "request.json").exists()
    assert all(path.read_bytes() == value for path, value in before.items())


def test_peer_projection_excludes_financial_values_and_keeps_snapshots():
    data = assemble([record(2024), record(2025)])
    projection = peer_input([data])
    text = json.dumps(projection, ensure_ascii=False)
    assert "123.456789" not in text
    assert "2024" in text and "2025" in text
    assert projection["companies"][0]["metadata"] == data["metadata"]


def test_peer_coverage_cannot_expand_candidate_universe():
    companies = [{"company": {"name": "A"}}, {"company": {"name": "B"}}]
    data = {
        "peer_pairs": [],
        "groups": [{"companies": ["A", "outside"]}],
        "research_scope": {"companies": ["A", "B", "outside"]},
    }
    errors = peer_errors(data, companies)
    assert "peer_pair_coverage" in errors
    assert "research_scope_companies" in errors
    assert "unknown_group_company" in errors


@pytest.mark.parametrize(
    "raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', "```json\n{}\n```", '{"x":']
)
def test_strict_json_never_repairs(raw):
    with pytest.raises(ValueError):
        parse(raw)


def test_replay_never_reads_pdf_tokenizer_credentials_or_network(tmp_path, monkeypatch):
    source = recording(tmp_path)
    before = hashes(source)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(CLI, "read_pdf", lambda *a: pytest.fail("PDF read"))
    monkeypatch.setattr(
        v2_transport, "dotenv_values", lambda *a: pytest.fail("credentials read")
    )
    monkeypatch.setattr(
        v2_transport.Tokenizer, "from_file", lambda *a: pytest.fail("tokenizer read")
    )
    output = tmp_path / "replay"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    summary = parse((output / "summary.json").read_text("utf8"))
    assert summary["model_calls"] == 0
    assert summary["replayed_responses"] == len(parts())
    assert hashes(source) == before
    for name, value in parts().items():
        folder = output / "blocks" / name
        assert (folder / "response.body").read_bytes() == (
            source / "blocks" / name / "response.body"
        ).read_bytes()
        assert parse((folder / "parsed.json").read_text("utf8")) == value


@pytest.fixture
def prepared_pdf(tmp_path, monkeypatch):
    source = tmp_path / "annual.pdf"
    source.write_bytes(b"not a PDF; parser is stubbed for transport tests")
    monkeypatch.setattr(
        CLI,
        "read_pdf",
        lambda path: (
            [{"type": "text", "text": "complete synthetic annual report"}],
            1,
        ),
    )
    tokenizer = SimpleNamespace(encode=lambda text: SimpleNamespace(ids=[1] * 100))
    monkeypatch.setattr(v2_transport.Tokenizer, "from_file", lambda path: tokenizer)
    return source


def test_preflight_freezes_all_requests_without_http_or_key(
    prepared_pdf, tmp_path, monkeypatch
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(
        v2_transport,
        "dotenv_values",
        lambda *a: pytest.fail("preflight credentials read"),
    )
    output = tmp_path / "prepared"
    assert (
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(output),
                "--profile",
                "bank",
                "--company",
                "Company A",
                "--year",
                "2025",
            ]
        )
        == 0
    )
    summary = parse((output / "summary.json").read_text("utf8"))
    wanted = set(CONFIG["base_blocks"] + CONFIG["industry_profiles"]["bank"]["blocks"])
    assert set(summary["blocks"]) == wanted
    assert summary["model_calls"] == 0 and summary["max_calls"] == len(wanted)
    assert all((output / "blocks" / name / "request.json").exists() for name in wanted)
    assert (output / "freeze.json").exists()
    assert not (output / "data.json").exists()
    requests_saved = [
        parse((output / "blocks" / name / "request.json").read_text("utf8"))
        for name in wanted
    ]
    first = requests_saved[0]["messages"]
    assert all(request["messages"][0] == first[0] for request in requests_saved)
    assert all(
        request["messages"][1]["content"][:-1] == first[1]["content"][:-1]
        for request in requests_saved
    )


def test_extract_requires_explicit_industry_profile(prepared_pdf, tmp_path):
    with pytest.raises(SystemExit):
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(tmp_path / "unspecified"),
            ]
        )


@pytest.mark.parametrize(
    "profiles, block",
    [
        (["bank"], "capital"),
        (["bank"], "metadata"),
        (["bank"], "notes_bank_1"),
        (["bank"], "notes_risk"),
        (["insurance"], "notes_insurance_1"),
        (["construction"], "notes_construction"),
        (["bank", "insurance", "bank"], "notes_insurance_2"),
    ],
)
def test_single_block_keeps_full_contract_and_identical_request(
    prepared_pdf, tmp_path, monkeypatch, profiles, block
):
    monkeypatch.setattr(
        v2_transport,
        "dotenv_values",
        lambda *a: pytest.fail("preflight credentials read"),
    )
    full, single = tmp_path / "full", tmp_path / "single"
    args = [
        "extract",
        "--source",
        str(prepared_pdf),
        "--company",
        "Company A",
        "--year",
        "2025",
    ]
    for profile in profiles:
        args += ["--profile", profile]
    assert CLI.main([*args, "--output", str(full)]) == 0
    assert CLI.main([*args, "--output", str(single), "--block", block]) == 0

    # Compare complete serialized requests, including the unchanged task footer.
    request_path = Path("blocks") / block / "request.json"
    assert (single / request_path).read_bytes() == (full / request_path).read_bytes()
    request = parse((single / request_path).read_text("utf8"))
    identity = json.loads(request["messages"][1]["content"][0]["text"])
    expected = list(CONFIG["base_blocks"])
    for profile in dict.fromkeys(profiles):
        expected += [
            name
            for name in CONFIG["industry_profiles"][profile]["blocks"]
            if name not in expected
        ]
    assert list(identity["frozen_job_ownership"]) == expected
    assert "execution_blocks" not in identity
    assert request["messages"][1]["content"][-1]["text"].startswith(
        f"[block:{block}]\n"
    )
    selection = {"ownership_blocks": expected, "execution_blocks": [block]}
    summary = parse((single / "summary.json").read_text("utf8"))
    plan = parse((single / "call_plan.json").read_text("utf8"))
    freeze = parse((single / "freeze.json").read_text("utf8"))
    assert summary["execution_plan"] == plan["execution_plan"] == selection
    assert list(summary["blocks"]) == freeze["blocks"] == [block]
    assert summary["budget"]["calls"] == summary["max_calls"] == 1
    assert freeze["max_calls"] == 1
    assert summary["model_calls"] == 0
    assert [p.name for p in (single / "blocks").iterdir()] == [block]
    assert not list(single.rglob("attempt.json"))


def test_single_block_outside_profile_is_rejected_before_pdf_read(
    prepared_pdf, tmp_path, monkeypatch
):
    monkeypatch.setattr(CLI, "read_pdf", lambda *a: pytest.fail("PDF read"))
    with pytest.raises(ValueError, match="block_not_in_selected_profile"):
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(tmp_path / "invalid"),
                "--profile",
                "bank",
                "--block",
                "notes_insurance_1",
            ]
        )


def test_single_block_mock_dispatches_only_selected_job(
    prepared_pdf, tmp_path, monkeypatch
):
    output = tmp_path / "one-mocked-job"
    sent = []

    def post(*args, **kwargs):
        request = kwargs["json"]
        footer = request["messages"][-1]["content"][-1]["text"]
        name = footer.split("[block:", 1)[1].split("]", 1)[0]
        sent.append(name)
        identity = json.loads(request["messages"][1]["content"][0]["text"])
        assert len(identity["frozen_job_ownership"]) == 10
        return response(EXAMPLES[name])

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setattr(requests, "post", post)
    assert (
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(output),
                "--profile",
                "bank",
                "--block",
                "capital",
                "--paid",
                "--budget-limit-usd",
                "10",
            ]
        )
        == 0
    )
    assert sent == ["capital"]
    summary = parse((output / "summary.json").read_text("utf8"))
    assert summary["max_calls"] == summary["model_calls"] == 1
    assert list(summary["blocks"]) == ["capital"]


@pytest.mark.parametrize("budget", [None, "0", "0.000001"])
def test_paid_execution_needs_sufficient_explicit_budget_before_any_request(
    prepared_pdf, tmp_path, budget
):
    argv = [
        "extract",
        "--source",
        str(prepared_pdf),
        "--output",
        str(tmp_path / "budget"),
        "--profile",
        "bank",
        "--paid",
    ]
    if budget is not None:
        argv += ["--budget-limit-usd", budget]
    with pytest.raises((SystemExit, ValueError)):
        CLI.main(argv)


def test_mocked_api_executes_exact_frozen_plan_once(
    prepared_pdf, tmp_path, monkeypatch
):
    forbid_business_audit(monkeypatch)
    calls = []
    output = tmp_path / "mock"
    wanted = CONFIG["base_blocks"] + CONFIG["industry_profiles"]["bank"]["blocks"]

    def post(*args, **kwargs):
        request = kwargs["json"]
        footer = request["messages"][-1]["content"][-1]["text"]
        name = footer.split("[block:", 1)[1].split("]", 1)[0]
        calls.append(name)
        assert all(
            (output / "blocks" / key / "request.json").exists() for key in wanted
        )
        assert (output / "frozen_code/scripts/build_fundamental_data_v2.py").exists()
        return response(EXAMPLES[name])

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setattr(requests, "post", post)
    assert (
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(output),
                "--profile",
                "bank",
                "--company",
                "示例集团（虚构）",
                "--year",
                "2025",
                "--paid",
                "--budget-limit-usd",
                "10",
            ]
        )
        == 0
    )
    assert calls == wanted

    summary = parse((output / "summary.json").read_text("utf8"))
    assert summary["status"] == "completed"
    assert summary["usage_totals"]["prompt_tokens"] == len(wanted) * 100
    for name in wanted:
        assert (
            parse((output / "blocks" / name / "parsed.json").read_text("utf8"))
            == EXAMPLES[name]
        )
    with pytest.raises(FileExistsError):
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(output),
                "--profile",
                "bank",
                "--paid",
                "--budget-limit-usd",
                "10",
            ]
        )
    assert calls == wanted


def test_online_replay_render_and_history_never_execute_business_audit(
    tmp_path, monkeypatch
):
    source = recording(tmp_path)
    forbid_business_audit(monkeypatch)
    output = tmp_path / "retained"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    data = parse((output / "data.json").read_text("utf8"))
    history_output = tmp_path / "history"
    assert (
        CLI.main(["history", "--source", str(output), "--output", str(history_output)])
        == 0
    )
    assert "123.456789" in (history_output / "阅读.md").read_text("utf8")
    assert not (output / "blocks/statements/format_notes.json").exists()
    assert all_tables(data)


def test_audit_is_explicit_offline_and_does_not_change_the_data(tmp_path, monkeypatch):
    data = record()
    next(t for t in data["tables"] if t.get("role") == "IS")["rows"][0]["statistic"][
        "kind"
    ] = "point"
    source = dump(tmp_path / "record.json", data)
    original_bytes = source.read_bytes()
    calls = []
    real_validate = v2_schema.validate

    def spy(config, stage, value):
        calls.append(stage)
        return real_validate(config, stage, value)

    monkeypatch.setattr(CLI, "validate", spy)
    output = tmp_path / "audit"
    assert CLI.main(["audit", "--source", str(source), "--output", str(output)]) == 0
    audit = parse((output / "audit.json").read_text("utf8"))
    assert calls and audit["reports"][0]["issues"]
    assert source.read_bytes() == original_bytes
    assert not (output / "data.json").exists()
    assert not (output / "阅读.html").exists()


def test_stable_identity_merges_display_name_changes_and_preserves_source_fields():
    older, newer = record(2024), record(2025)
    old_table, new_table = all_tables(older)[0], all_tables(newer)[0]
    new_table["title"] = "另一年度的表格名称"
    new_table["rows"][0]["path"] = ["另一年度的现金项目名称"]
    new_table["rows"][0]["unit"] = "人民币百万元"
    new_table["rows"][0]["values"]["c1"] = "2025-VALUE"
    original = deepcopy([older, newer])
    history = assemble([older, newer], CONFIG)
    matching = [t for t in history["tables"] if t.get("identity_code") == "BS"]
    assert len(matching) == 1
    assert len(matching[0]["rows"]) == 1
    row = matching[0]["rows"][0]
    assert set(row["values"].values()) == {"123.456789", "2025-VALUE"}
    assert {tuple(entry["fields"]["path"]) for entry in row["source_fields"]} == {
        tuple(old_table["rows"][0]["path"]),
        ("另一年度的现金项目名称",),
    }
    assert len(matching[0]["source_fields"]) == 2
    assert [older, newer] == original


def test_wide_segment_rows_have_one_member_and_keep_total_and_eliminations_separate():
    data = record()
    table = numeric(role="IS")
    table.update(
        role="note",
        identity_code="NOTE_SEGMENT_PERFORMANCE",
        topic="segments_geographies",
        title="业务分部收入",
    )
    table["rows"] = []
    for member, value in (
        ("批发金融", "100"),
        ("TOTAL", "200"),
        ("ELIMINATIONS", "(10)"),
    ):
        row = deepcopy(numeric(role="IS")["rows"][0])
        row.update(
            identity_code="REVENUE",
            path=["营业收入"],
            dimensions=[{"axis": "BUSINESS_SEGMENT", "member": member}],
            values={"c1": value},
        )
        table["rows"].append(row)
    data["tables"] = [table]
    before = deepcopy(data)
    history = assemble([data], CONFIG)
    kept = next(
        t
        for t in history["tables"]
        if t.get("identity_code") == "NOTE_SEGMENT_PERFORMANCE"
    )
    assert len(kept["rows"]) == 3
    assert {
        row["dimensions"][0]["member"]: next(iter(row["values"].values()))
        for row in kept["rows"]
    } == {"批发金融": "100", "TOTAL": "200", "ELIMINATIONS": "(10)"}
    assert history["conflicts"] == []
    assert data == before


def test_period_overrides_keep_open_close_and_explicit_unknown_without_date_arithmetic():
    data = record()
    table = numeric(role="CFS")
    prototype = deepcopy(table["rows"][0])
    table["rows"] = []
    cases = [
        (
            "CFS_OPENING_CASH",
            "OPEN",
            {
                "kind": "instant",
                "start": None,
                "end": "2025-01-01",
                "coverage": "not_applicable",
            },
        ),
        (
            "CFS_CLOSING_CASH",
            "CLOSE",
            {
                "kind": "instant",
                "start": None,
                "end": "2025-12-31",
                "coverage": "not_applicable",
            },
        ),
        ("CFS_NET_OPERATING", "UNKNOWN", None),
    ]
    for identity, value, override in cases:
        row = deepcopy(prototype)
        row.update(
            identity_code=identity,
            values={"c1": value},
            period_overrides={"c1": override},
            statistic=statistic("point"),
        )
        table["rows"].append(row)
    data["tables"] = [table]
    original = deepcopy(data)
    history = assemble([data], CONFIG)
    kept = next(t for t in history["tables"] if t.get("identity_code") == "CFS")
    observed = {
        value: kept["columns"][key]["period"]
        for row in kept["rows"]
        for key, value in row["values"].items()
    }
    assert observed == {value: override for identity, value, override in cases}
    assert data == original


def test_unknown_identity_keeps_outside_catalog_content_without_guessing():
    records = [record(2024), record(2025)]
    for data in records:
        table = all_tables(data)[0]
        table["identity_code"] = None
        row = table["rows"][0]
        row.update(
            identity_code=None, unit_code=None, path=["目录外新项目"], unit="目录外单位"
        )
    history = assemble(records, CONFIG)
    tables = [t for t in history["tables"] if t.get("role") == "BS"]
    assert len(tables) == 2
    assert all(t["identity_code"] is None for t in tables)
    assert all(
        t["rows"][0]["identity_code"] is None and t["rows"][0]["unit_code"] is None
        for t in tables
    )
    assert [t["rows"][0]["path"] for t in tables] == [
        ["目录外新项目"],
        ["目录外新项目"],
    ]


@pytest.mark.parametrize("uncertainty", ["statistic", "average_method", "annualized"])
def test_uncertain_statistic_isolated_per_row_without_blocking_known_rows(uncertainty):
    records = [record(2024), record(2025)]
    for year, data in zip((2024, 2025), records):
        table = all_tables(data)[0]
        row = deepcopy(table["rows"][0])
        row.update(
            identity_code="BS_BONDS_PAYABLE",
            path=["口径未定的债券"],
            values={"c1": str(year)},
        )
        if uncertainty == "statistic":
            row["statistic"]["kind"] = "unknown"
        else:
            row["period_overrides"] = {"c1": period(year)}
            row["statistic"]["kind"] = (
                "period_average"
                if uncertainty == "average_method"
                else "period_measure"
            )
            row["statistic"]["method"] = {"state": "not_specified", "text": None}
            if uncertainty == "annualized":
                row["statistic"]["annualized"] = None
        table["rows"].append(row)
    history = assemble(records, CONFIG)
    table = next(t for t in history["tables"] if t.get("identity_code") == "BS")
    known = [r for r in table["rows"] if r.get("identity_code") == "BS_CASH"]
    uncertain = [
        r for r in table["rows"] if r.get("identity_code") == "BS_BONDS_PAYABLE"
    ]
    assert len(known) == 1 and len(known[0]["values"]) == 2
    assert len(uncertain) == 2 and all(len(r["values"]) == 1 for r in uncertain)


@pytest.mark.parametrize("malformation", ["values_shape", "bad_column_descriptor"])
def test_one_unreadable_row_does_not_stop_other_rows_merging(tmp_path, malformation):
    records = [record(2024), record(2025)]
    originals = []
    for year, data in zip((2024, 2025), records):
        table = all_tables(data)[0]
        bad = deepcopy(table["rows"][0])
        bad.update(identity_code="BS_BONDS_PAYABLE", path=["局部未定债券"])
        if malformation == "values_shape":
            bad["values"] = f"BAD-ROW-{year}"
        else:
            table["columns"]["c9"] = "BAD-COLUMN-DESCRIPTOR"
            bad["values"] = {"c9": f"BAD-ROW-{year}"}
        table["rows"].append(bad)
        originals.append(deepcopy(bad))
        data["tables"] = [table]
    before = deepcopy(records)
    history = assemble(records, CONFIG)
    table = next(t for t in history["tables"] if t.get("identity_code") == "BS")
    known = [row for row in table["rows"] if row.get("identity_code") == "BS_CASH"]
    assert len(known) == 1 and len(known[0]["values"]) == 2
    bad_rows = [
        row for row in table["rows"] if row.get("identity_code") == "BS_BONDS_PAYABLE"
    ]
    assert len(bad_rows) == 2
    assert [row["values"] for row in bad_rows] == [row["values"] for row in originals]
    assert all(row["alignment"] == "independent" for row in bad_rows)
    publish(tmp_path, history, CONFIG["labels"])
    page = (tmp_path / "阅读.md").read_text("utf8")
    assert "BAD-ROW-2024" in page and "BAD-ROW-2025" in page
    assert records == before


def test_unknown_comparison_is_local_to_its_cells_and_preserves_all_observations():
    records = [record(2024), record(2025)]
    for year, data in zip((2024, 2025), records):
        table = all_tables(data)[0]
        table["columns"]["c2"] = deepcopy(table["columns"]["c1"])
        table["columns"]["c2"].update(
            identity_code="YOY", label="未定比较基期", comparison=None
        )
        table["rows"][0]["values"]["c2"] = f"UNDETERMINED-YOY-{year}"
    history = assemble(records, CONFIG)
    matching = [
        table for table in history["tables"] if table.get("identity_code") == "BS"
    ]
    assert len(matching) == 1
    assert len(matching[0]["rows"]) == 1
    row = matching[0]["rows"][0]
    assert len(row["values"]) == 4
    assert Counter(row["values"].values()) == Counter(
        ["123.456789", "123.456789", "UNDETERMINED-YOY-2024", "UNDETERMINED-YOY-2025"]
    )
    assert (
        len(
            [
                c
                for c in matching[0]["columns"].values()
                if c["identity_code"] == "VALUE"
            ]
        )
        == 2
    )


@pytest.mark.parametrize("raw", ['{"x":', '{"x":1,"x":2}', '{"x":NaN}'])
def test_parse_failures_keep_raw_and_logs_without_retry(tmp_path, raw):
    source = recording(tmp_path)
    folder = source / "blocks" / "capital"
    (folder / "response.body").write_bytes(response(raw=raw).content)
    output = tmp_path / "failure"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 2
    saved = output / "blocks" / "capital"
    assert (saved / "content.txt").read_text("utf8") == raw
    assert not (saved / "parsed.json").exists()
    steps = [
        json.loads(line)["step"]
        for line in (saved / "events.jsonl").read_text("utf8").splitlines()
    ]
    assert steps.count("attempt_started") == 1
    assert steps.index("content_saved") < steps.index("processing_failed")


def test_truncated_json_values_are_retained_and_run_is_partial(tmp_path):
    values = parts()
    values["capital"]["tables"] = [numeric(value="TRUNCATED-SECRET", role="capital")]
    source = recording(tmp_path, values, finish="length")
    output = tmp_path / "truncated"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 2
    raw = parse((output / "blocks/capital/parsed.json").read_text("utf8"))
    assert raw == values["capital"]
    data = parse((output / "data.json").read_text("utf8"))
    assert any("TRUNCATED-SECRET" in json.dumps(t) for t in all_tables(data))
    assert parse((output / "summary.json").read_text("utf8"))["status"] == "partial"


def test_local_schema_problem_preserves_the_affected_table_and_other_tables(tmp_path):
    values = parts()
    broken = values["statements"]["tables"][0]
    broken["rows"][0]["values"]["c99"] = "ORPHAN-VALUE"
    source = recording(tmp_path, values)
    output = tmp_path / "bad_table"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    assert (
        parse((output / "blocks/statements/parsed.json").read_text("utf8"))
        == values["statements"]
    )
    text = (output / "阅读.md").read_text("utf8")
    assert "ORPHAN-VALUE" in text
    data = parse((output / "data.json").read_text("utf8"))
    assert any(t.get("role") == "IS" for t in all_tables(data))
    affected = next(t for t in all_tables(data) if t.get("role") == "BS")
    assert affected["rows"][0]["values"]["c99"] == "ORPHAN-VALUE"
    assert "未声明列键：c99" in text
    assert not (output / "blocks/statements/format_notes.json").exists()
    block_text = (output / "blocks/statements/阅读.md").read_text("utf8")
    assert "| 科目及层级 |" in block_text  # Other tables still use normal formatting.
    assert "# 未完成或不符合新合约" not in block_text
    with (output / "financial_values.csv").open(encoding="utf-8-sig", newline="") as f:
        orphan = next(r for r in csv.DictReader(f) if r["value"] == "ORPHAN-VALUE")
    assert json.loads(orphan["period"]) == {"unmapped_column_key": "c99"}


@pytest.mark.parametrize(
    "problem", ["missing_issues", "period_conflict", "bad_kind", "supplied_version"]
)
def test_table_notes_never_remove_returned_rows(tmp_path, problem):
    values = parts()
    table = values["statements"]["tables"][-1]
    if problem == "missing_issues":
        table.pop("issues")
    elif problem == "period_conflict":
        table["rows"][0]["statistic"] = statistic("point")
    elif problem == "bad_kind":
        table["kind"] = "numeric_typo"
    else:
        table["columns"]["c1"]["reported_in"] = {"unexpected": "MODEL-VERSION"}
    original = deepcopy(table)
    source = recording(tmp_path, values)
    output = tmp_path / problem
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    data = parse((output / "data.json").read_text("utf8"))
    kept = data["tables"][-1]
    assert kept["rows"] == original["rows"]
    assert kept["kind"] == original["kind"]
    assert ("issues" in kept) == ("issues" in original)
    if problem == "supplied_version":
        assert kept["columns"]["c1"]["reported_in"] == {"unexpected": "MODEL-VERSION"}
    assert "statements" not in data["unstructured_blocks"]
    assert data["issues"]
    page = (output / "blocks/statements/阅读.html").read_text("utf8")
    assert "<table>" in page and "格式／口径检查" not in page
    assert (
        parse((output / "blocks/statements/parsed.json").read_text("utf8"))
        == values["statements"]
    )


@pytest.mark.parametrize(
    "block,collection",
    [
        ("notes_foundation", "policies"),
        ("notes_foundation", "changes"),
        ("capital", "events"),
        ("metadata", "metadata"),
        ("metadata", "management"),
        ("statements", "issues"),
    ],
)
def test_bad_record_presentation_does_not_hide_other_tables(
    tmp_path, block, collection
):
    values = parts()
    item = {"target": "tables/0"} if collection == "issues" else {"kept": "RAW-RECORD"}
    values[block][collection] = [item]
    source = recording(tmp_path, values)
    output = tmp_path / "record_fragment"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    page = (output / "blocks" / block / "阅读.html").read_text("utf8")
    assert "# 未完成或不符合新合约" not in page
    if collection != "issues":
        assert "RAW-RECORD" in page and "该条原始内容保留" in page
    else:
        assert "<table>" in page and "检查说明字段未提供" in page
    assert (
        parse((output / "blocks" / block / "parsed.json").read_text("utf8"))
        == values[block]
    )
    data = parse((output / "data.json").read_text("utf8"))
    assert len(next(t for t in data["tables"] if t.get("role") == "CFS")["rows"]) > 0


@pytest.mark.parametrize(
    "issues", [42, True, [{"target": "tables/0", "reason": ["bad"]}]]
)
def test_malformed_issue_shape_is_a_local_annotation(tmp_path, issues):
    values = parts()
    values["statements"]["issues"] = issues
    source = recording(tmp_path, values)
    output = tmp_path / "issue_shape"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    page = (output / "blocks/statements/阅读.html").read_text("utf8")
    assert "<table>" in page and "该条原始内容保留" in page
    raw = parse((output / "blocks/statements/parsed.json").read_text("utf8"))
    assert raw["issues"] == issues


def test_actual_cmb_response_keeps_all_parsed_tables_without_repairs(tmp_path):
    source = ROOT / "data/fundamental_data/v3_cmb_2025_first_20261024"
    if not source.is_dir():
        pytest.skip("Local frozen CMB response is not checked into repository")
    before = hashes(source)
    output = tmp_path / "cmb_annotations"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 2
    data = parse((output / "data.json").read_text("utf8"))
    raw_tables = []
    for name in parse((source / "summary.json").read_text("utf8"))["blocks"]:
        original = source / "blocks" / name
        copied = output / "blocks" / name
        assert (original / "response.body").read_bytes() == (
            copied / "response.body"
        ).read_bytes()
        if not (original / "parsed.json").exists():
            continue
        raw = parse((original / "parsed.json").read_text("utf8"))
        assert parse((copied / "parsed.json").read_text("utf8")) == raw
        raw_tables.extend(raw.get("tables", []))
    assert len(raw_tables) == len(data["tables"]) == 62
    assert sum(len(t["rows"]) for t in data["tables"]) == 693
    for original, table in zip(raw_tables, data["tables"]):
        kept = deepcopy(table)
        kept.pop("reported_in", None)
        for column in kept["columns"].values():
            column.pop("reported_in", None)
        assert kept == original
    cfs = next(t for t in data["tables"] if t.get("role") == "CFS")
    assert len(cfs["rows"]) == 58
    assert cfs["rows"][-1]["values"] == {"c1": "709,822", "c2": "756,527"}
    assert data["unstructured_blocks"].keys() == {"notes_operations", "notes_assets"}
    page = (output / "阅读.html").read_text("utf8")
    assert "175.51%" in page and "200.03%" in page
    assert "709,822" in page and "point_in_duration_column" not in page
    assert not (output / "blocks/statements/format_notes.json").exists()
    with (output / "financial_values.csv").open(encoding="utf-8-sig", newline="") as f:
        assert len([r for r in csv.DictReader(f) if r["role"] == "CFS"]) == sum(
            len(row["values"]) for row in cfs["rows"]
        )
    summary = parse((output / "summary.json").read_text("utf8"))
    assert summary["model_calls"] == 0 and summary["replayed_responses"] == 10
    assert summary["blocks"]["notes_operations"]["status"] == "truncated"
    assert hashes(source) == before


def test_three_year_history_keeps_current_cmb_problem_tables_and_values(tmp_path):
    path = (
        ROOT
        / "data/fundamental_data/v3_cmb_2025_annotation_replay_20261024_final/data.json"
    )
    if not path.is_file():
        pytest.skip("Local frozen CMB response is not checked into repository")
    current = parse(path.read_text("utf8"))
    previous = [record(2023), record(2024)]
    for item in previous:
        item["company"] = deepcopy(current["company"])
        for report in item["reports"]:
            report["company"] = deepcopy(current["company"])
    history = assemble([*previous, current], CONFIG)
    assert len(history["reports"]) == 3
    assert any(
        t.get("role") == "CFS" and len(t["rows"]) == 58 for t in history["tables"]
    )
    original = next(t for t in current["tables"] if t.get("kind") == "attribute")
    retained = next(
        t
        for t in history["tables"]
        if t.get("title") == original["title"] and t.get("kind") == original["kind"]
    )
    retained_fields = deepcopy(retained)
    assert retained_fields.pop("alignment") == "legacy_preserved"
    assert retained_fields == original
    original_observations = [
        (table.get("title"), row["path"], value)
        for table in current["tables"]
        for row in table["rows"]
        for value in row["values"].values()
    ]
    actual_observations = [
        (table.get("title"), row["path"], value)
        for table in history["tables"]
        for row in table["rows"]
        for value in row["values"].values()
    ]
    assert all(item in actual_observations for item in original_observations)
    publish(tmp_path, history, CONFIG["labels"])
    page = (tmp_path / "阅读.html").read_text("utf8")
    assert "709,822" in page and "175.51%" in page and "200.03%" in page


def test_single_report_history_keeps_each_cmb_cell_and_its_column_context():
    path = (
        ROOT
        / "data/fundamental_data/v3_cmb_2025_annotation_replay_20261024_final/data.json"
    )
    if not path.is_file():
        pytest.skip("Local frozen CMB response is not checked into repository")
    data = parse(path.read_text("utf8"))
    history = assemble([data], CONFIG)

    def observations(record):
        return Counter(
            json.dumps(
                [
                    table.get("role"),
                    table["title"],
                    table["part"],
                    row["path"],
                    row.get("dimensions"),
                    row.get("unit"),
                    row.get("currency"),
                    row.get("statistic"),
                    table["columns"][key],
                    value,
                    row.get("missing_reasons", {}).get(key),
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
            for table in record["tables"]
            for row in table["rows"]
            for key, value in row["values"].items()
        )

    assert sum(len(t["rows"]) for t in history["tables"]) == 693
    assert observations(data) == observations(history)
    assert sum(observations(history).values()) == 1938
    assert history["conflicts"] == []


@pytest.mark.parametrize("same_label", [False, True])
def test_same_period_columns_do_not_collapse_by_period_alone(same_label):
    data = record()
    table = data["tables"][0]
    table["columns"]["c2"] = deepcopy(table["columns"]["c1"])
    table["columns"]["c1"]["label"] = "2025年末交易余额"
    table["columns"]["c2"]["label"] = (
        "2025年末交易余额" if same_label else "2025年末占比"
    )
    table["rows"][0]["values"] = {"c1": "100", "c2": "3.10%"}
    history = assemble([data], CONFIG)
    kept = next(t for t in history["tables"] if t["title"] == table["title"])
    assert len(kept["columns"]) == 2
    assert kept["rows"][0]["values"] == {"c1": "100", "c2": "3.10%"}
    assert history["conflicts"] == []


@pytest.mark.parametrize("problem", ["missing_issues", "unknown_column", "wrong_kind"])
def test_history_retains_nonmergeable_tables_without_guessing(tmp_path, problem):
    data = record()
    table = data["tables"][0]
    if problem == "missing_issues":
        table.pop("issues")
        table["rows"][0].pop("issues")
    elif problem == "unknown_column":
        table["rows"][0]["values"]["c99"] = "UNMAPPED-RETAINED"
    else:
        table["kind"] = "numeric_typo"
    original = deepcopy(table)
    history = assemble([data], CONFIG)
    matching = next(t for t in history["tables"] if t.get("title") == table["title"])
    assert matching["rows"][0]["values"] == original["rows"][0]["values"]
    if problem == "unknown_column":
        row = matching["rows"][0]
        assert row["alignment"] == "independent"
        assert row["source_fields"][0]["columns"] == original["columns"]
    elif problem == "wrong_kind":
        kept = deepcopy(matching)
        assert kept.pop("alignment") == "independent"
        assert kept == original
    publish(tmp_path, history, CONFIG["labels"])
    assert "<table>" in (tmp_path / "阅读.html").read_text("utf8")


def test_unknown_model_fields_are_saved_and_shown_without_aliasing(tmp_path):
    values = parts()
    values["capital"]["extra_answer"] = "UNPLANNED-EXTRA"
    source = recording(tmp_path, values)
    output = tmp_path / "extra"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    raw = parse((output / "blocks/capital/parsed.json").read_text("utf8"))
    assert raw["extra_answer"] == "UNPLANNED-EXTRA"
    assert "UNPLANNED-EXTRA" in (output / "阅读.md").read_text("utf8")


@pytest.mark.parametrize(
    "malformation",
    ["capital_tables_null", "notes_issues_null", "topic_list", "requested_item_object"],
)
def test_malformed_optional_shapes_are_diagnostics_and_do_not_stop_replay(
    tmp_path, malformation
):
    values = parts()
    if malformation == "capital_tables_null":
        name = "capital"
        values[name]["tables"] = None
    elif malformation == "notes_issues_null":
        name = "notes_assets"
        values[name]["issues"] = None
    elif malformation == "topic_list":
        name = "notes_assets"
        table = numeric(role="note")
        table["topic"] = ["inventory"]
        values[name]["tables"] = [table]
    else:
        name = "capital"
        values[name]["tables"][0]["rows"][0]["requested_item"] = {"wrong": "shape"}
    assert validate(CONFIG, name, values[name])
    source = recording(tmp_path, values)
    output = tmp_path / "diagnosed"
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    summary = parse((output / "summary.json").read_text("utf8"))
    assert summary["replayed_responses"] == len(values)
    assert all((output / "blocks" / key / "receipt.json").exists() for key in values)
    assert (
        parse((output / "blocks" / name / "parsed.json").read_text("utf8"))
        == values[name]
    )


def test_history_retains_later_row_issues_with_report_context(tmp_path):
    older, newer = record(2024), record(2025)
    row = all_tables(newer)[0]["rows"][0]
    row["issues"] = [
        {
            "target": "definition",
            "reason": "not_found",
            "detail": "LATER-ROW-ISSUE-ONLY",
        }
    ]
    before = deepcopy([older, newer])
    merged = assemble([older, newer])
    row = next(t for t in all_tables(merged) if t["role"] == "BS")["rows"][0]
    assert any(
        issue["detail"] == "LATER-ROW-ISSUE-ONLY" and issue["target"] == "definition"
        for issue in row["issues"]
    )
    assert any(
        any(
            issue.get("detail") == "LATER-ROW-ISSUE-ONLY"
            for issue in entry["fields"].get("issues", [])
        )
        and any(
            isinstance(version, dict) and version.get("report_id") == "report-2025"
            for version in entry["reported_in"]
        )
        for entry in row["source_fields"]
    )
    publish(tmp_path, merged, CONFIG["labels"])
    assert "LATER-ROW-ISSUE-ONLY" in (tmp_path / "阅读.md").read_text("utf8")
    assert [older, newer] == before


def test_reader_displays_context_and_versions_for_non_numeric_records(tmp_path):
    data = record()

    def version(tag, year):
        return {"report_id": tag, "report_period": f"FY{year}"}

    def contextual_scope(tag):
        return {"kind": "parent", "entity": tag}

    def note(tag):
        return [{"target": "scope", "reason": "context_uncertain", "detail": tag}]

    data["policies"] = [
        {
            "module": "common",
            "topic": "reporting_basis",
            "scope": contextual_scope("POLICY-SCOPE"),
            "accounting_basis": "POLICY-BASIS",
            "text": "政策正文",
            "issues": [],
            "reported_in": version("p-v2021", 2021),
        }
    ]
    data["changes"] = [
        {
            "module": "common",
            "topic": "reporting_basis",
            "scope": contextual_scope("CHANGE-SCOPE"),
            "text": "变更正文",
            "reported_in": version("c-v2022", 2022),
        }
    ]
    data["events"] = [
        {
            "event_type": "dividend",
            "phase": "proposal",
            "date": {"value": "2023-05", "precision": "month"},
            "scope": contextual_scope("EVENT-SCOPE"),
            "description": "分派正文",
            "terms": None,
            "issues": note("EVENT-ISSUE"),
            "reported_in": version("e-v2023", 2023),
        }
    ]
    data["management"] = [
        {
            "topic": "展望",
            "kind": "management_plan",
            "text": "管理层计划正文",
            "period": period(2024),
            "scope": contextual_scope("MANAGE-SCOPE"),
            "issues": note("MANAGE-ISSUE"),
            "reported_in": version("m-v2024", 2024),
        }
    ]
    attribute = attributes()
    attribute["context"]["adjustment"] = {
        "status": "restated",
        "label": "ATTRIBUTE-ADJUSTMENT",
    }
    data["tables"].append(attribute)
    publish(tmp_path, data, CONFIG["labels"])
    text = (tmp_path / "阅读.md").read_text("utf8")
    for tag in (
        "POLICY-SCOPE",
        "POLICY-BASIS",
        "p-v2021",
        "CHANGE-SCOPE",
        "c-v2022",
        "EVENT-SCOPE",
        "EVENT-ISSUE",
        "e-v2023",
        "MANAGE-SCOPE",
        "MANAGE-ISSUE",
        "m-v2024",
        "2024-01-01",
        "2024-12-31",
        "ATTRIBUTE-ADJUSTMENT",
    ):
        assert tag in text


@pytest.mark.parametrize("tamper", ["future_request", "call_plan"])
def test_frozen_future_requests_and_call_plan_checked_before_next_mock_call(
    prepared_pdf, tmp_path, monkeypatch, tamper
):
    output = tmp_path / "tampered"
    calls = []

    def post(*args, **kwargs):
        footer = kwargs["json"]["messages"][-1]["content"][-1]["text"]
        name = footer.split("[block:", 1)[1].split("]", 1)[0]
        calls.append(name)
        target = (
            output / "call_plan.json"
            if tamper == "call_plan"
            else output / "blocks/metadata/request.json"
        )
        freeze = parse((output / "freeze.json").read_text("utf8"))
        assert str(target.resolve()) in freeze["files"]
        changed = parse(target.read_text("utf8"))
        changed["tampered"] = True
        target.write_text(json.dumps(changed), "utf8")
        return response(EXAMPLES[name])

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(ValueError, match="frozen"):
        CLI.main(
            [
                "extract",
                "--source",
                str(prepared_pdf),
                "--output",
                str(output),
                "--profile",
                "bank",
                "--paid",
                "--budget-limit-usd",
                "10",
            ]
        )
    assert calls == [CONFIG["base_blocks"][0]]


def test_replay_forbids_paid_and_writing_into_source(tmp_path):
    source = recording(tmp_path)
    with pytest.raises(SystemExit):
        CLI.main(
            [
                "replay",
                "--source",
                str(source),
                "--output",
                str(tmp_path / "paid"),
                "--paid",
            ]
        )
    with pytest.raises(SystemExit):
        CLI.main(
            ["replay", "--source", str(source), "--output", str(source / "inside")]
        )
    assert not (source / "inside").exists()


def test_missing_recording_has_no_network_fallback(tmp_path):
    source = recording(tmp_path)
    (source / "blocks/capital/response.body").unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        CLI.main(
            ["replay", "--source", str(source), "--output", str(tmp_path / "missing")]
        )


@pytest.mark.parametrize(
    "old_name", ["v2_luzhou_2025_chunks_first", "v2_cmb_2025_first"]
)
def test_actual_old_responses_replay_is_read_only_and_not_upgraded(tmp_path, old_name):
    source = ROOT / "data/fundamental_data" / old_name
    if not source.is_dir():
        pytest.skip("Local historical response is not checked into repository")
    before = hashes(source)
    output = tmp_path / old_name
    assert CLI.main(["replay", "--source", str(source), "--output", str(output)]) == 0
    summary = parse((output / "summary.json").read_text("utf8"))
    assert summary["model_calls"] == 0 and summary["status"] == "completed"
    for original in (source / "blocks").iterdir():
        if (original / "response.body").exists():
            copied = output / "blocks" / original.name
            assert (copied / "response.body").read_bytes() == (
                original / "response.body"
            ).read_bytes()
            if (original / "parsed.json").exists():
                assert parse((copied / "parsed.json").read_text("utf8")) == parse(
                    (original / "parsed.json").read_text("utf8")
                )
    assert hashes(source) == before


def test_all_31_recorded_cmb_responses_replay_without_audit_or_identity_migration(
    tmp_path, monkeypatch
):
    base = ROOT / "data/fundamental_data"
    sources = [
        base / "cmb_2023_2025_64k_20261024" / f"stage1_{year}" for year in (2023, 2024)
    ]
    sources.append(base / "v3_cmb_2025_first_20261024")
    single = base / "cmb_2023_operations_128k_thinking_20261024"
    if not all(path.is_dir() for path in [*sources, single]):
        pytest.skip("The 31 historical CMB responses are local immutable artifacts")
    old_hashes = {path: hashes(path) for path in [*sources, single]}
    wrapper = tmp_path / "single_recording"
    folder = wrapper / "blocks/notes_operations"
    folder.mkdir(parents=True)
    for filename in ("request.json", "response.body", "receipt.json"):
        (folder / filename).write_bytes((single / filename).read_bytes())
    dump(
        wrapper / "summary.json", {"blocks": {"notes_operations": {}}, "pdf_pages": 350}
    )
    sources.append(wrapper)
    forbid_business_audit(monkeypatch)
    response_count = 0
    for index, source in enumerate(sources):
        output = tmp_path / f"replay_{index}"
        assert CLI.main(
            ["replay", "--source", str(source), "--output", str(output)]
        ) in (0, 2)
        summary = parse((output / "summary.json").read_text("utf8"))
        assert summary["model_calls"] == 0
        response_count += summary["replayed_responses"]
        for original in (source / "blocks").iterdir():
            copied = output / "blocks" / original.name
            assert (copied / "response.body").read_bytes() == (
                original / "response.body"
            ).read_bytes()
            if (original / "parsed.json").exists():
                assert parse((copied / "parsed.json").read_text("utf8")) == parse(
                    (original / "parsed.json").read_text("utf8")
                )
            assert not (copied / "format_notes.json").exists()
        data = parse((output / "data.json").read_text("utf8"))
        assert all(
            "identity_code" not in table
            for table in data["tables"]
            if isinstance(table, dict)
        )
        if data.get("company", {}).get("code") or data.get("company", {}).get("name"):
            history = assemble([data], CONFIG)
            assert history["conflicts"] == []
            assert sum(
                len(table.get("rows", []))
                for table in history["tables"]
                if isinstance(table, dict)
            ) == sum(
                len(table.get("rows", []))
                for table in data["tables"]
                if isinstance(table, dict)
            )
    assert response_count == 31
    assert all(hashes(path) == before for path, before in old_hashes.items())


def test_html_sanitizes_model_text_without_changing_saved_data(tmp_path):
    data = record()
    attack = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    data["metadata"][0]["businesses"] = [attack]
    publish(tmp_path, data, CONFIG["labels"])
    html = (tmp_path / "阅读.html").read_text("utf8")

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag != "script"
            assert not any(name.startswith("on") for name, value in attrs)

    Tags().feed(html)
    assert parse((tmp_path / "data.json").read_text("utf8"))["metadata"][0][
        "businesses"
    ] == [attack]
