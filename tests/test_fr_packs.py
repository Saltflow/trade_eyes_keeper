"""Contract regressions plus optional replay of the frozen real reports."""

from copy import deepcopy
from pathlib import Path

import pytest

from src.fundamental_data.disclosure import (
    apply_policy_reference,
    audit_all,
    build_all,
    digest,
    extract_field,
    locate_sections,
    make_pack,
    read_json,
    verified_pack,
    write_json,
)
from src.fundamental_data.disclosure.contract import FIELDS


def metadata(kind="annual"):
    return {
        "short_id": "A",
        "document_id": "doc-A",
        "issuer_id": "a_share:test",
        "company_name": "测试公司",
        "title": "测试财报",
        "kind": kind,
        "period_end": "2025-12-31" if kind == "annual" else "2026-06-30",
        "published_at": "2026-04-01" if kind == "annual" else "2026-08-01",
    }


def field(text, metric="investment_income", kind="income", meta=None):
    meta = meta or metadata()
    section = {
        "kind": kind,
        "lines": [
            {"page": 1, "line": i + 1, "text": s}
            for i, s in enumerate(text.splitlines())
        ],
    }
    return extract_field(metric, FIELDS[metric], [section], meta)


def test_cash_negative_and_zero_are_not_missing():
    f = field("单位：元 币种：人民币\n项目 2025年度 2024年度\n投资收益 (100.00) 0.00")
    assert f["amount"] == "-100.00"
    assert f["comparative_amount"] == "0.00"
    f = field(
        "单位：万元 币种：人民币\n项目 2025年度 2024年度\n投资收益 −100.00 200.00"
    )
    assert f["amount"] == "-1000000.00"


def test_blank_column_cannot_borrow_next_row_or_become_zero():
    f = field(
        "单位：元\n项目 2025年度 2024年度\n投资收益 100.00\n公允价值变动收益 200.00 300.00"
    )
    assert f["status"] == "parse_failed"
    assert "amount" not in f


def test_balance_beginning_vs_halfyear_comparison():
    text = "2026年6月30日\n单位：元\n项目 期末余额 期初余额\n存货 100.00 90.00"
    f = field(text, "inventory", "balance", metadata("interim"))
    assert f["status"] == "obtained"
    assert f["comparative_period_end"] == "2025-12-31"
    cfo = field(
        "单位：元\n项目 2026年半年度 2025年半年度\n经营活动产生的现金流量净额 100.00 90.00",
        "cfo",
        "cashflow",
        metadata("interim"),
    )
    assert cfo["comparative_period_end"] == "2025-06-30"


def test_incorrect_dates_and_missing_units_fail_visibly():
    f = field("单位：元\n项目 2024年度 2023年度\n投资收益 100.00 90.00")
    assert f["status"] == "parse_failed"
    f = field("项目 2025年度 2024年度\n投资收益 100.00 90.00")
    assert f["status"] == "parse_failed"


def test_conflicting_rows_not_silently_selected():
    f = field(
        "单位：元\n项目 2025年度 2024年度\n投资收益 100.00 90.00\n投资收益 200.00 90.00"
    )
    assert f["status"] == "conflict"


def test_missing_currency_is_not_invented():
    f = field("单位：元\n项目 2025年度 2024年度\n投资收益 100.00 90.00")
    assert f["currency"] is None
    assert f["currency_status"] == "presentation_currency_not_explicit_in_table_header"


def test_complete_chapters_ascii_policy_titles_and_parent_exclusion():
    pages = {
        "1": "第三节 管理层讨论与分析........8\n第三节 管理层讨论与分析\n首段\n跨页表头",
        "2": "续表内容\n最后一段\n第四节 公司治理\n不属MD&A\n五、重要会计政策及会计估计\n1. 收入\n控制权确认，净额。\n2. 存货\n加权平均\n六、税项\n所得税\n七、合并财务报表项目注释\n1、投资收益\n权益法 100.00 90.00",
    }
    sections = locate_sections(pages)
    mda = [s for s in sections if s["kind"] == "mda"]
    assert len(mda) == 1
    assert "最后一段" in mda[0]["text"] and "不属MD&A" not in mda[0]["text"]
    assert any(s["title"] == "1. 收入" and s["kind"] == "policy" for s in sections)
    pages = {
        "1": "合并现金流量表\n2025年度 2024年度\n单位：元\n经营活动产生的现金流量净额 100.00 90.00\n母公司现金流量表\n经营活动产生的现金流量净额 999.00 900.00"
    }
    f = extract_field("cfo", FIELDS["cfo"], locate_sections(pages), metadata())
    assert f["amount"] == "100.00"


def fixture_source(tmp_path, id="A", kind="annual", policy=True):
    meta = metadata(kind)
    meta.update(short_id=id, document_id="doc-" + id, page_count=1)
    year = int(meta["period_end"][:4])
    text = f"测试公司\n第三节 管理层讨论与分析\n成本变化，完整讨论。\n第四节 财务报告\n合并现金流量表\n单位：元 币种：人民币\n项目 {year}年半年度 {year - 1}年半年度\n经营活动产生的现金流量净额 100.00 90.00\n购建固定资产、无形资产和其他长期资产支付的现金 10.00 8.00\n母公司现金流量表\n五、重要会计政策及会计估计\n"
    if policy:
        text += "1. 收入\n控制权确认。\n"
    text += (
        "六、税项\n所得税\n七、合并财务报表项目注释\n1、投资收益\n权益法 100.00 90.00"
    )
    folder = tmp_path / id
    folder.mkdir()
    pdf = folder / "original.pdf"
    import pypdfium2

    with pypdfium2.PdfDocument.new() as doc:
        page = doc.new_page(100, 100)
        page.close()
        doc.save(str(pdf))
    meta.update(path=str(pdf), sha256=digest(pdf.read_bytes()))
    raw = "--- PAGE 1 ---" + text
    (folder / "document.txt").write_text(raw, encoding="utf8")
    meta["text_sha256"] = digest(raw.encode())
    return {"metadata": meta, "pages": {"1": text}}


def test_fcf_proxy_is_calculated_and_not_normalized(tmp_path):
    source = fixture_source(tmp_path)
    pack = make_pack(source, "x")
    assert pack["fcf_proxy"]["amount"] == "90.00"
    assert pack["fcf_proxy"]["pending_adjustments"]
    assert pack["supplements"]["disclosed_fcf"]["status"] == "not_located"


def test_policy_reference_requires_same_company_and_available_date(tmp_path):
    annual = make_pack(fixture_source(tmp_path), "annual")
    interim = make_pack(
        fixture_source(tmp_path, "B", "interim", False), "interim", annual
    )
    topic = interim["accounting_topics"]["revenue"]
    assert topic["policy_reference"]["pack_key"] == "annual"
    assert topic["policy_origin"] != "current_report"
    bad = deepcopy(annual)
    bad["metadata"]["published_at"] = "2027-01-01"
    fresh = make_pack(fixture_source(tmp_path, "C", "interim", False), "fresh", bad)
    assert "policy_reference" not in fresh["accounting_topics"]["revenue"]
    bad["metadata"]["published_at"] = "2026-04-01"
    bad["metadata"]["issuer_id"] = "other"
    apply_policy_reference(fresh, bad)
    assert "policy_reference" not in fresh["accounting_topics"]["revenue"]


def test_cached_build_restart_subset_dependency_and_corruption(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda *a, **kw: pytest.fail("network forbidden"),
    )
    src, out = tmp_path / "src", tmp_path / "out"
    a, b = fixture_source(tmp_path), fixture_source(tmp_path, "B", "interim", False)
    write_json(src / "sources" / "A.json", a)
    write_json(src / "sources" / "B.json", b)
    first = build_all(src, out, ["B"])
    assert first["built"] == 2
    assert any(e.get("dependency_only") for e in first["entries"])
    files = list((out / "packs").glob("*/pack.json"))
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
    second = build_all(src, out, ["B"])
    assert second["cache_hits"] == 2 and second["built"] == 0
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
    assert audit_all(out)["passed"]
    child = next(e for e in second["entries"] if e["document"] == "B")
    folder = out / "packs" / child["pack_key"]
    snapshot = read_json(folder / "source.json")
    snapshot["pages"]["1"] += "forged"
    write_json(folder / "source.json", snapshot)
    with pytest.raises(ValueError, match="source_corrupt"):
        verified_pack(folder)
    assert not audit_all(out)["passed"]


def test_source_restatement_new_version_and_bad_request(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    a = fixture_source(tmp_path)
    write_json(src / "sources" / "A.json", a)
    first = build_all(src, out)
    old = first["entries"][0]["pack_key"]
    a["metadata"]["published_at"] = "2026-04-02"
    write_json(src / "sources" / "A.json", a)
    revised = build_all(src, out)
    assert revised["entries"][0]["pack_key"] != old
    assert (out / "packs" / old / "pack.json").exists()
    missing = build_all(src, out, ["NOPE"])
    assert missing["entries"][0]["status"] == "failed"
    a["pages"]["1"] += "forged text"
    write_json(src / "sources" / "A.json", a)
    bad = build_all(src, out)
    assert bad["entries"][0]["status"] == "source_blocked"




REAL = Path("data/fundamental_quant/deepseek_comparison_20261007")


@pytest.mark.skipif(
    not (REAL / "manifest.json").exists(), reason="local frozen real evidence absent"
)
def test_real_reports_core_numbers_and_interim_date():
    for id in ["D03", "D04", "D07", "D11"]:
        source = read_json(REAL / "sources" / f"{id}.json")
        pack = make_pack(source, id)
        old = read_json(REAL / "facts_rechecked" / f"{id}.json")
        for f in old["accepted"]:
            if f["metric"] not in {"revenue", "cfo", "capex", "parent_equity"}:
                continue
            got = pack["financial_fields"][f["metric"]]
            assert got["status"] == "obtained", (id, f["metric"])
            assert got["amount"] == f["amount"]
        assert all(t["policy_sections"] for t in pack["accounting_topics"].values())
        if id == "D04":
            assert (
                pack["financial_fields"]["inventory"]["comparative_period_end"]
                == "2025-12-31"
            )


def test_explicit_opening_january_date_not_relabelled_prior_december():
    meta = metadata()
    meta["period_end"] = "2023-12-31"
    f = field(
        "单位：元\n项目 2023年12月31日 2023年1月1日\n存货 100.00 90.00",
        "inventory",
        "balance",
        meta,
    )
    assert f["status"] == "obtained"
    assert f["comparative_period_end"] == "2023-01-01"
    f = field(
        "单位：元\n项目 2022年12月31日 2023年1月1日 调整金额\n存货 100.00 90.00",
        "inventory",
        "balance",
        meta,
    )
    assert f["status"] == "parse_failed"


@pytest.mark.parametrize(
    "body",
    [
        "投资收益（损失以“-”号填列）\n其中：对联营企业和合营企业的投资收益 100.00 200.00",
        "投资收益（损失以“-”号填列）100.00\n200.00",
        "投资收益（损失以“-”号填列）\n公允价值变动收益 100.00 200.00",
    ],
)
def test_never_borrow_amounts_from_next_row(body):
    f = field("单位：元\n项目 2025年度 2024年度\n" + body)
    assert f["status"] != "obtained"
    assert "amount" not in f


@pytest.mark.skipif(
    not (REAL / "manifest.json").exists(), reason="local frozen real evidence absent"
)
def test_real_cashflow_reconciliation_keeps_nested_table():
    for id in ["D03", "D07", "D11"]:
        pack = make_pack(read_json(REAL / "sources" / f"{id}.json"), id)
        refs = pack["supplements"]["cfo_reconciliation"]["section_refs"]
        text = "\n".join(s["text"] for s in pack["sections"] if s["id"] in refs)
        assert "将净利润调节为经营活动现金流量" in text
        assert "经营活动产生的现金流量净额" in text.replace("\n", "")
        assert (
            pack["financial_fields"]["cfo"]["value"]
            == pack["financial_fields"]["cfo"]["amount"]
        )
        assert f"{float(pack['financial_fields']['cfo']['value']):,.2f}" in text


@pytest.mark.parametrize("dash", ["-", "−", "－", "—"])
def test_real_loss_label_dash_variants(dash):
    f = field(
        f"单位：元\n项目 2025年度 2024年度\n投资收益（损失以“{dash}”号填列） {dash}100.00 200.00"
    )
    assert f["status"] == "obtained"
    assert f["amount"] == "-100.00"


def test_malformed_source_is_visible_without_blocking_other_company(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    write_json(src / "sources" / "A.json", fixture_source(tmp_path))
    write_json(src / "sources" / "broken.json", ["invalid shape"])
    result = build_all(src, out)
    assert any(e["status"] == "source_blocked" for e in result["entries"])
    assert any(e.get("pack_key") for e in result["entries"])
    assert result["model_calls"] == 0


def test_changed_one_source_keeps_other_cached(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    a, b = fixture_source(tmp_path), fixture_source(tmp_path, "B")
    b["metadata"]["issuer_id"] = "a_share:another"
    write_json(src / "sources" / "A.json", a)
    write_json(src / "sources" / "B.json", b)
    build_all(src, out)
    a["metadata"]["published_at"] = "2026-04-02"
    write_json(src / "sources" / "A.json", a)
    result = build_all(src, out)
    assert result["built"] == 1 and result["cache_hits"] == 1


def test_numbered_parent_profit_and_wrapped_label():
    f = field(
        "单位：元 币种：人民币\n项目 2025年度 2024年度\n1.归属于母公司股东的净利润\n123.00 100.00\n（净亏损以“-”号填列）\n2.少数股东损益 4.00 3.00",
        "net_profit_parent",
    )
    assert f["status"] == "obtained"
    assert f["amount"] == "123.00"
    assert f["comparative_amount"] == "100.00"
