"""The renderer distinguishes evidence, unreviewed drafts and accepted results."""

import copy
import re

from src.fundamental_quant.longitudinal_report import (
    chart,
    render_longitudinal_email,
    render_longitudinal_report,
)


def packet():
    return {
        "spec": {"title": "Peer <Group>", "as_of": "2026-10-10"},
        "rows": [
            {
                "issuer_id": "demo",
                "company_name": "甲公司",
                "period": f"{year}-12-31",
                "currency": "CNY",
                "values": {"revenue": value},
                "calculated": {},
            }
            for year, value in [(2022, 1e8), (2023, None), (2024, 2e8)]
        ],
        "facts": {
            "revenue-2024": {"value": 2e8, "refs": ["D01:p3"], "quote": "收入20000万元"}
        },
        "calculated_values": {
            "calc-growth": {
                "value": 0.4142,
                "expression": "(2/1)**(1/2)-1",
                "refs": ["revenue-2024"],
            }
        },
        "documents": [
            {
                "document_id": "D01",
                "company_name": "甲公司",
                "period_end": "2024-12-31",
                "pages": {"3": "收入20000万元"},
                "source_path": "https://example.com/annual.pdf",
                "gate": {"passed": True},
            }
        ],
        "coverage": [{"issuer_id": "demo", "years": [2022, 2024]}],
        "historical_valuations": {"rows": [], "issues": ["历史股份数缺失"]},
    }


def result(text="研究者判断"):
    return {
        "title": "阶段研究",
        "summary": text,
        "axes": {
            "long_term_value": {
                "status": "insufficient",
                "basis": "资料不足",
                "refs": ["revenue-2024"],
            }
        },
        "claims": [
            {
                "id": "claim1",
                "key": "normalized_earnings",
                "status": "supported",
                "basis": text,
                "refs": ["D01:p3"],
                "calc_refs": ["calc-growth"],
                "counterevidence": "反证仍需核查",
            }
        ],
        "bridges": [
            {
                "title": "现金桥接",
                "explanation": "说明",
                "residual": "10元尚未解释",
                "refs": ["D01:p3"],
            }
        ],
    }


def test_prepared_page_does_not_claim_research_acceptance(tmp_path):
    output = tmp_path / "report.html"
    document = render_longitudinal_report(packet(), output=output)
    assert output.read_text(encoding="utf-8") == document
    assert "尚未形成研究结论" in document
    assert "不是研究验收报告" in document
    assert "历史股份数缺失" in document
    assert "<svg" in document
    assert "已审查最终报告" not in document
    assert "20000万元" in document


def test_chart_breaks_missing_values_and_excludes_half_year_and_currency():
    rows = packet()["rows"] + [
        {
            "issuer_id": "demo",
            "period": "2025-06-30",
            "currency": "CNY",
            "values": {"revenue": 9e10},
        },
        {
            "issuer_id": "other",
            "period": "2025-12-31",
            "currency": "USD",
            "values": {"revenue": 9e11},
        },
    ]
    document = chart(rows, "revenue", "收入")
    paths = re.findall(r'<path d="([^"]*)"', document)
    assert len(paths) == 1
    assert paths[0].count("M") == 2
    assert "L" not in paths[0]
    assert "2025" not in document
    assert "2023" in document  # Missing period stays on the axis.
    assert "没有按零值绘制" in chart([], "revenue", "收入")


def test_draft_cannot_become_accepted_from_status_alone():
    document = render_longitudinal_report(
        packet(), {"status": "accepted", "research_result": result()}
    )
    assert "最新研究草稿" in document
    assert "没有 approved_result" in document
    assert "已审查最终报告" not in document


def test_reviewed_stage_and_new_draft_stay_separate():
    document = render_longitudinal_report(
        packet(),
        {
            "status": "reviewing",
            "approved_result": result("旧版已审查"),
            "research_result": result("新版未审查"),
            "usage": {"input_tokens": 200, "unknown_usage_calls": 1},
        },
    )
    assert "已审查阶段结果" in document
    assert "最新研究草稿" in document
    assert document.index("旧版已审查") < document.index("新版未审查")
    assert 'href="#ref-' in document
    assert "未解析引用" not in document
    assert "unknown_usage_calls" in document


def test_revoked_content_is_not_current_result():
    document = render_longitudinal_report(
        packet(),
        {
            "status": "revoked",
            "approved_result": result("应隐藏投资判断"),
            "research_result": result("应隐藏投资判断"),
        },
    )
    assert "旧结论已失效" in document
    assert "应隐藏投资判断" not in document


def test_html_is_escaped_and_unsafe_links_are_never_active():
    data = copy.deepcopy(packet())
    data["documents"][0]["source_path"] = "javascript:alert(1)"
    data["documents"][0]["pages"]["3"] = '<script>alert("x")</script>'
    draft = result('<img src=x onerror="alert(1)">')
    draft["claims"][0]["refs"] = ['<img src=x onerror="alert(1)">']
    document = render_longitudinal_report(data, {"research_result": draft})
    assert "<script>" not in document
    assert "<img " not in document
    assert 'href="javascript:' not in document
    assert "&lt;script&gt;" in document
    assert "未解析引用" in document
    assert "Peer &lt;Group&gt;" in document


def test_accepted_result_not_duplicated_as_draft():
    research = result()
    document = render_longitudinal_report(
        packet(),
        {
            "status": "accepted",
            "approved_result": research,
            "research_result": research,
            "report_version": "version123",
        },
    )
    assert "已审查最终报告" in document
    assert "最新研究草稿" not in document
    assert "version123" in document


def test_absent_calendar_year_and_unknown_currency_stay_explicit():
    rows = [row for row in packet()["rows"] if row["period"] != "2023-12-31"]
    document = chart(rows, "revenue", "收入")
    path = re.findall(r'<path d="([^"]*)"', document)[0]
    assert path.count("M") == 2
    assert "L" not in path
    data = packet()
    data["rows"][0]["currency"] = None
    document = render_longitudinal_report(data)
    assert "币种尚未明确" in document
    assert "不进行跨公司金额高低排名" in document


def test_new_calculations_estimates_and_supplemental_pages_have_real_links():
    data = packet()
    research = result()
    research["claims"][0]["calc_refs"] = ["fresh-calc", "estimate.base"]
    research["claims"][0]["refs"] = ["EXTRA:p1"]
    state = {
        "research_result": research,
        "calculations": {"fresh-calc": {"value": 20, "refs": ["EXTRA:p1"]}},
        "estimates": {
            "estimate": {"low": 10, "base": 20, "high": 30, "refs": ["EXTRA:p1"]}
        },
        "supplemental_pages": {"EXTRA:p1": "补充原文"},
        "error": "failed request has unknown usage",
    }
    document = render_longitudinal_report(data, state)
    assert "未解析引用" not in document
    assert "补充原文" in document
    assert "failed request has unknown usage" in document


def test_email_projection_preserves_approved_text_and_version_without_originals():
    data = packet()
    data["documents"][0]["pages"]["3"] = "LARGE_ORIGINAL_MARKER" * 10000
    research = result("模型经过核验的原始研究文字")
    state = {
        "status": "accepted",
        "approved_result": research,
        "usage": {"input_tokens": 123},
    }
    document = render_longitudinal_email(data, state, "release-version")
    assert "模型经过核验的原始研究文字" in document
    assert "release-version" in document
    assert "D01:p3" in document
    assert "LARGE_ORIGINAL_MARKER" not in document
    assert 'href="#ref-' not in document
    assert 'href="file:' not in document
    assert len(document) < 40000
    assert "真实调用与费用记录" in document


def test_email_projection_refuses_unreviewed_draft():
    import pytest

    with pytest.raises(ValueError, match="email_requires_accepted"):
        render_longitudinal_email(
            packet(), {"status": "reviewing", "research_result": result()}, "version"
        )


def test_monthly_valuation_chart_breaks_missing_month_and_preserves_multiples():
    rows = [
        {
            "issuer_id": "test",
            "period": period,
            "kind": "month",
            "currency": "CNY",
            "calculated": {"pe": value},
        }
        for period, value in [("2025-01-31", 10), ("2025-03-31", 12)]
    ]
    document = chart(rows, "pe", "PE", calculated=True, monthly=True, multiple=True)
    path = re.findall(r'<path d="([^"]*)"', document)[0]
    assert path.count("M") == 2
    assert "L" not in path
    assert "2025-02" in document
    assert "12.00 倍" in document
    assert "月末当时可知信息" in document


def test_snapshot_keeps_missing_company_and_full_management_text():
    data = packet()
    data["spec"]["companies"] = [
        {"issuer_id": "demo", "name": "甲公司"},
        {"issuer_id": "missing", "name": "乙公司"},
    ]
    data["documents"][0]["issuer_id"] = "demo"
    data["chapters"] = [
        {
            "id": "D01:mda",
            "document_id": "D01",
            "issuer_id": "demo",
            "kind": "mda",
            "title": "经营讨论",
            "pages": [3, 4],
            "text": "管理层说明回款减少。\n" + "完整续表" * 500,
        }
    ]
    rendered = render_longitudinal_report(data)
    assert "2024 年度横向比较" in rendered
    assert "乙公司" in rendered and "0/7" in rendered
    assert "完整续表" * 500 in rendered
    assert "管理层怎样解释变化" in rendered
    assert "不据此自动认定低估" in rendered
    assert "已审查最终报告" not in rendered


def test_original_coverage_does_not_count_comparative_financial_values(tmp_path):
    data = packet()
    data["spec"].update(
        group_id="test",
        companies=[{"issuer_id": "demo", "name": "甲公司"}],
        history_years=2,
    )
    data["documents"][0].update(
        issuer_id="demo", published_at="2025-03-30", source_sha256="a" * 64
    )
    output = tmp_path / "index.html"
    render_longitudinal_report(data, {"status": "prepared"}, output)
    html = output.read_text(encoding="utf-8")
    assert "取得 1/3 个期间的原报告" in html
    assert "2025-12-31、2026-06-30" in html
    assert "比较期数字齐备不代表当年管理层讨论或附注已取得" in html
