"""Regression replay of engineering financial layouts, not investment tests."""

import json
from pathlib import Path

import pytest

from src.fundamental_quant.fr_pack import extract_field, locate_sections
from src.fundamental_quant.fr_pack_contract import FIELDS

PACKET = Path(
    "data/fundamental_quant/longitudinal_research_20261010/construction/packet.json"
)


def real_document(issuer, period):
    if not PACKET.exists():
        pytest.skip("frozen engineering originals unavailable")
    payload = json.loads(PACKET.read_text(encoding="utf8"))
    document = next(
        (
            d
            for d in payload["documents"]
            if d["issuer_id"] == issuer and d["period_end"] == period
        ),
        None,
    )
    if document is None:
        pytest.skip("requested frozen engineering original unavailable")
    return document


def read_fields(document):
    meta = {
        "short_id": "test",
        "period_end": document["period_end"],
        "company_name": document["company_name"],
    }
    sections = locate_sections(document["pages"])
    return {
        metric: extract_field(metric, spec, sections, meta)
        for metric, spec in FIELDS.items()
    }


@pytest.mark.parametrize(
    "period", ["2023-12-31", "2024-12-31", "2025-12-31", "2026-06-30"]
)
def test_sichuan_real_note_references_and_wrapped_rows(period):
    fields = read_fields(real_document("a_share:600039", period))
    for metric in (
        "revenue",
        "cost",
        "net_profit_parent",
        "cfo",
        "capex",
        "parent_equity",
    ):
        assert fields[metric]["status"] == "obtained", (metric, fields[metric])
    if period == "2023-12-31":
        assert fields["capex"]["amount"] == "7745804432.16"
        assert fields["capex"]["amount"] != "1422843959.31"  # preceding disposal row
    if period == "2025-12-31":
        assert fields["revenue"]["amount"] == "115111412828.74"
        assert fields["cost"]["amount"] == "98585221907.08"
        assert "七、61" in fields["revenue"]["quote"]


def test_cccc_grouped_integer_unit_parent_boundary_and_cash_payment_sign():
    fields = read_fields(real_document("a_share:601800", "2023-12-31"))
    assert fields["revenue"]["amount"] == "758676426548"
    assert fields["cost"]["amount"] == "663150168954"
    assert fields["cfo"]["amount"] == "12074258093"
    assert fields["cfo"]["source_ref"]["pages"] == [139]
    assert fields["capex"]["amount"] == "38956518113"
    assert fields["capex"]["value"] == "-38956518113"
    assert fields["capex"]["reported_amount"] == "-38956518113"
    assert fields["capex"]["sign_convention"] == "cash_paid_positive_refund_negative"
    assert fields["capex"]["currency"] == "CNY"
    assert fields["parent_equity"]["amount"] == "301734470210"


def test_scanned_financial_pages_remain_visible_gap_not_fabricated():
    fields = read_fields(real_document("a_share:601800", "2025-12-31"))
    assert fields["revenue"]["status"] == "not_located"
    assert fields["capex"]["status"] == "not_located"


def test_report_title_cannot_reverse_actual_year_columns():
    text = "合并利润表\n2025年度财务报表\n单位：元 币种：人民币\n项目 2024年度 2025年度\n营业收入 100.00 200.00"
    field = extract_field(
        "revenue",
        FIELDS["revenue"],
        locate_sections({"1": text}),
        {"short_id": "test", "period_end": "2025-12-31", "company_name": "测试"},
    )
    assert field["status"] == "parse_failed"
    assert not field["candidates"][0]["columns_verified"]


@pytest.mark.parametrize(
    "negative_table,capex,expected",
    [(False, "-10.00", "-10.00"), (True, "10.00", "-10.00"), (True, "-10.00", "10.00")],
)
def test_cash_payment_sign_requires_table_evidence_and_keeps_refunds(
    negative_table, capex, expected
):
    sign = "-" if negative_table else ""
    text = f"合并现金流量表\n单位：元 币种：人民币\n项目 2025年度 2024年度\n购买商品、接受劳务支付的现金 {sign}100.00 {sign}90.00\n支付的各项税费 {sign}20.00 {sign}18.00\n经营活动现金流出小计 {sign}120.00 {sign}108.00\n购建固定资产、无形资产和其他长期资产支付的现金 {capex} {capex}"
    field = extract_field(
        "capex",
        FIELDS["capex"],
        locate_sections({"1": text}),
        {"short_id": "test", "period_end": "2025-12-31", "company_name": "测试"},
    )
    assert field["status"] == "obtained"
    assert field["amount"] == expected
    assert field["reported_amount"] == capex


def test_mixed_payment_signs_do_not_establish_negative_table_convention():
    text = "合并现金流量表\n单位：元\n项目 2025年度 2024年度\n购买商品、接受劳务支付的现金 -100.00 -90.00\n支付的各项税费 -20.00 -18.00\n投资支付的现金 50.00 40.00\n经营活动现金流出小计 -120.00 -108.00\n购建固定资产、无形资产和其他长期资产支付的现金 -10.00 -9.00"
    field = extract_field(
        "capex",
        FIELDS["capex"],
        locate_sections({"1": text}),
        {"short_id": "test", "period_end": "2025-12-31", "company_name": "测试"},
    )
    assert field["amount"] == "-10.00"
    basis = field["cash_payment_sign_basis"]["current"]
    assert not basis["confirmed"]
    assert basis["positive_rows_conflicting_with_negative_convention"]


@pytest.mark.parametrize(
    "period,cfo,prior_cfo,capex",
    [
        ("2020-12-31", "13851445641", "5382620014", "92184411963"),
        ("2021-12-31", "-12642685470", "13851445641", "36961325379"),
    ],
)
def test_cccc_old_note_punctuation_and_unprefixed_parent_statement_boundary(
    period, cfo, prior_cfo, capex
):
    fields = read_fields(real_document("a_share:601800", period))
    assert fields["cfo"]["status"] == "obtained"
    assert fields["cfo"]["amount"] == cfo
    assert fields["cfo"]["comparative_amount"] == prior_cfo
    assert fields["capex"]["status"] == "obtained"
    assert fields["capex"]["amount"] == capex


def test_sichuan_wrapped_parent_profit_cannot_become_consolidated_profit():
    document = real_document("a_share:600039", "2021-12-31")
    field = extract_field(
        "net_profit_total",
        ("合并净利润", "income", "净利润"),
        locate_sections(document["pages"]),
        {"short_id": "test", "period_end": "2021-12-31", "company_name": "四川路桥"},
    )
    assert field["status"] == "obtained"
    assert field["amount"] == "5694198464.88"
    assert field["comparative_amount"] == "3035524277.25"


def test_cccc_2019_numbered_note_does_not_hide_actual_cfo():
    fields = read_fields(real_document("a_share:601800", "2019-12-31"))
    assert fields["cfo"]["status"] == "obtained"
    assert fields["cfo"]["amount"] == "5931260061"
    assert "69.(1)" in fields["cfo"]["quote"]


@pytest.mark.parametrize("period", ["2022-12-31", "2023-12-31"])
def test_actual_pdf_restated_header_is_aligned_to_comparative_column(period):
    from src.fundamental_quant.statement_restatement import comparative_header_basis

    document = real_document("a_share:601800", period)
    fields = read_fields(document)
    meta = {
        "path": document["source_path"],
        "sha256": document["source_sha256"],
        "period_end": period,
        "published_at": document["published_at"],
    }
    for metric in ["revenue", "cfo", "capex", "parent_equity"]:
        field = fields[metric]
        assert field["status"] == "obtained", (metric, field)
        proof = comparative_header_basis(field, meta, document["document_id"])
        assert (
            proof and proof["method"] == "pdf_aligned_comparative_restatement_header/v1"
        ), metric
        assert proof["scope"] == "this_statement_comparative_column_only"
        assert (
            proof["geometry"]["comparative_column"]["x0"]
            > proof["geometry"]["current_column"]["x0"]
        )
        assert proof["published_at"] == document["published_at"]


def test_no_restated_marker_does_not_authorize_latest_value():
    from src.fundamental_quant.statement_restatement import comparative_header_basis

    document = real_document("a_share:601800", "2020-12-31")
    field = read_fields(document)["revenue"]
    meta = {
        "path": document["source_path"],
        "sha256": document["source_sha256"],
        "period_end": document["period_end"],
        "published_at": document["published_at"],
    }
    assert comparative_header_basis(field, meta, document["document_id"]) is None


def test_restatement_marker_under_current_column_cannot_authorize_comparative(
    tmp_path, monkeypatch
):
    import hashlib
    from types import SimpleNamespace

    import pdfplumber

    from src.fundamental_quant.statement_restatement import _aligned_marker

    original = tmp_path / "geometry.pdf"
    original.write_bytes(b"unit geometry fixture")
    words = [
        {"text": "2023年", "x0": 10, "x1": 50, "top": 10},
        {"text": "2022年", "x0": 110, "x1": 150, "top": 10},
        {"text": "(经重述)", "x0": 10, "x1": 50, "top": 24},
    ]

    class PDF:
        def __init__(self):
            self.pages = [SimpleNamespace(extract_words=lambda: words)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(pdfplumber, "open", lambda *args: PDF())
    assert (
        _aligned_marker(
            str(original),
            hashlib.sha256(original.read_bytes()).hexdigest(),
            1,
            "2023",
            "2022",
        )
        is None
    )
