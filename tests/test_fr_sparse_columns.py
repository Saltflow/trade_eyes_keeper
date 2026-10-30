"""Real PDF coordinate regressions; synthetic mutations are adversarial cases."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import extract_field, locate_sections
from src.fundamental_data.disclosure.contract import FIELDS
from src.fundamental_data.disclosure.layout import sparse_column

FIXTURES = Path(__file__).parent / "fixtures/fr_sparse_columns"


def frozen(code="000858"):
    path = next(FIXTURES.glob(f"{code}_*.json"))
    value = json.loads(path.read_text(encoding="utf-8"))
    return value["source"], value["layout"]


def extract(source, layout, metric):
    return extract_field(
        metric,
        FIELDS[metric],
        locate_sections(source["pages"]),
        source["metadata"],
        layout,
    )


@pytest.mark.parametrize(
    "metric,value,page",
    [
        ("treasury_stock", "200021342.74", 35),
        ("investment_income_cash", "4000000.00", 42),
    ],
)
def test_actual_wuliangye_current_only_cells(metric, value, page):
    source, layout = frozen()
    field = extract(source, layout, metric)
    assert field["status"] == "obtained"
    assert field["amount"] == value
    assert field["comparative_amount"] is None
    assert field["cell_status"]["comparative"] == "blank_in_statement"
    assert field["coordinate_evidence"]["page"] == page
    assert field["coordinate_evidence"]["pdf_sha256"] == source["metadata"]["sha256"]
    assert field["coordinate_evidence"]["amount"]["bbox"]


def test_actual_luzhou_comparative_bond_does_not_become_current():
    source, layout = frozen("000568")
    field = extract(source, layout, "bonds")
    assert field["status"] != "obtained"
    assert "amount" not in field
    assert field["cell_status"]["current"] == "blank_in_statement"
    evidence = next(c for c in field["candidates"] if c.get("coordinate_evidence"))
    assert evidence["comparative_amount"] == "1498716737.02"
    assert evidence["comparative_period_end"] == "2023-12-31"


def test_same_real_dual_columns_unchanged():
    source, layout = frozen()
    sections = locate_sections(source["pages"])
    for metric in ("cfo", "capex", "inventory", "parent_equity", "cfi"):
        before = extract_field(metric, FIELDS[metric], sections, source["metadata"])
        after = extract(source, layout, metric)
        assert before["status"] == "obtained"
        assert before == after


def test_missing_coordinates_never_guess_first_number():
    source, layout = frozen()
    layout["pages"].pop("35")
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_text_layout_mismatch_is_not_accepted():
    source, layout = frozen()
    line = next(r for r in layout["pages"]["35"]["lines"] if "减：库存股" in r["text"])
    line["text"] += "篡改"
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_unstable_or_third_column_alignment_is_not_guessed():
    source, layout = frozen()
    for row in layout["pages"]["35"]["lines"]:
        if len(row["amounts"]) == 2:
            row["amounts"][0]["bbox"][2] += row["line"]
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_reversed_header_not_hidden_by_current_report_title():
    source, layout = frozen()
    source["pages"]["33"] = source["pages"]["33"].replace(
        "期末余额 期初余额", "期初余额 期末余额"
    )
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_parent_company_statement_cannot_supply_consolidated_sparse_value():
    source, layout = frozen()
    source["pages"]["33"] = source["pages"]["33"].replace(
        "合并资产负债表", "母公司资产负债表"
    )
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_cross_page_windows_not_combined_for_sparse_cell():
    source, layout = frozen()
    section = next(
        s for s in locate_sections(source["pages"]) if s["kind"] == "balance"
    )
    target = next(r for r in section["lines"] if "减：库存股" in r["text"])
    window = [deepcopy(target), deepcopy(target)]
    window[0]["page"] = 34
    assert sparse_column(window, section, layout, 2026) is None


def test_three_period_header_refuses_sparse_column():
    source, layout = frozen()
    header = next(
        r for r in layout["pages"]["33"]["lines"] if "项目 期末余额" in r["text"]
    )
    header["headings"].append({"text": "年初余额", "bbox": [510, 500, 540, 510]})
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_wrong_pdf_identity_cannot_supply_coordinates():
    source, layout = frozen()
    layout["pdf_sha256"] = "wrong-document"
    assert extract(source, layout, "treasury_stock")["status"] != "obtained"


def test_valid_layout_cache_avoids_pdf_parser_and_checks_source_hash(
    tmp_path, monkeypatch
):
    import hashlib

    import pypdfium2

    from src.fundamental_data.disclosure.layout import _hash, layout_for_source

    source, layout = frozen()
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"cache-contract-fixture-only")
    source["metadata"]["path"] = str(pdf)
    source["metadata"]["sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    layout["pdf_sha256"] = source["metadata"]["sha256"]
    layout["version"] = "fr-statement-layout/2"
    for page in layout["pages"].values():
        for row in page["lines"]:
            row.setdefault("cell_glyphs", [])
    layout["layout_sha256"] = _hash(layout["pages"])
    (tmp_path / f"fr_statement_layout_v2_{layout['pdf_sha256']}.json").write_text(
        json.dumps(layout), encoding="utf-8"
    )
    monkeypatch.setattr(
        pypdfium2, "PdfDocument", lambda *_: pytest.fail("cached layout reparsed")
    )
    result = layout_for_source(source, locate_sections(source["pages"]))
    assert result["status"] == "obtained" and result["cache_reused"]
    pdf.write_bytes(b"changed-original")
    result = layout_for_source(source, locate_sections(source["pages"]))
    assert result["status"] == "parse_failed"
    assert "pdf_source_hash_mismatch" in result["reason"]


@pytest.mark.parametrize("corruption", ["hash", "invalid_json"])
def test_invalid_layout_cache_cannot_supply_coordinates(
    tmp_path, monkeypatch, corruption
):
    import hashlib

    import pypdfium2

    from src.fundamental_data.disclosure.layout import layout_for_source

    source, layout = frozen()
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"cache-contract-fixture-only")
    source["metadata"].update(
        path=str(pdf), sha256=hashlib.sha256(pdf.read_bytes()).hexdigest()
    )
    layout.update(pdf_sha256=source["metadata"]["sha256"], layout_sha256="corrupted")
    (tmp_path / f"fr_statement_layout_v2_{layout['pdf_sha256']}.json").write_text(
        "{broken" if corruption == "invalid_json" else json.dumps(layout),
        encoding="utf-8",
    )

    def invalid_pdf(*args):
        raise ValueError("fresh_parser_attempted")

    monkeypatch.setattr(pypdfium2, "PdfDocument", invalid_pdf)
    result = layout_for_source(source, locate_sections(source["pages"]))
    assert result["status"] == "parse_failed"
    assert "fresh_parser_attempted" in result["reason"]


def test_distinct_pdfs_same_directory_each_reuse_own_layout(tmp_path, monkeypatch):
    import hashlib

    import pypdfium2

    from src.fundamental_data.disclosure.layout import _hash, layout_for_source

    documents = []
    for code in ("000858", "000568"):
        source, layout = frozen(code)
        pdf = tmp_path / f"{code}.pdf"
        pdf.write_bytes(f"cache-contract-fixture-{code}".encode())
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        source["metadata"].update(path=str(pdf), sha256=sha)
        for page in layout["pages"].values():
            for row in page["lines"]:
                row.setdefault("cell_glyphs", [])
        layout.update(
            version="fr-statement-layout/2",
            pdf_sha256=sha,
            layout_sha256=_hash(layout["pages"]),
        )
        (tmp_path / f"fr_statement_layout_v2_{sha}.json").write_text(
            json.dumps(layout), encoding="utf-8"
        )
        documents.append(source)
    monkeypatch.setattr(
        pypdfium2, "PdfDocument", lambda *_: pytest.fail("separate cached PDF reparsed")
    )
    for source in documents + documents:
        result = layout_for_source(source, locate_sections(source["pages"]))
        assert result["status"] == "obtained" and result["cache_reused"]
        assert result["pdf_sha256"] == source["metadata"]["sha256"]
