"""Generic comparison HTML; content comes exclusively from frozen inputs/results.

This module neither authorizes publication nor manufactures research conclusions.
An approved_result is displayed separately from a newer, unreviewed research_result.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from .reporting import _atomic

AXES = {
    "long_term_value": "长期价值创造",
    "operating_trend": "经营趋势",
    "price_expectations": "价格隐含预期",
}
CLAIMS = {
    "normalized_earnings": "正常化盈利",
    "capital_efficiency": "资本效率",
    "normalized_cashflow": "正常化股东现金流",
    "implied_return": "隐含回报",
    "peer_thesis": "同行比较",
    "thesis_breaker": "Thesis 与失效条件",
}
STATUS = {
    "prepared": "资料已准备 · 尚无研究结论",
    "researching": "研究中",
    "reviewing": "独立审查中",
    "revising": "修正中",
    "reviewed_stage": "已审查阶段结果",
    "accepted": "已审查最终结果",
    "blocked": "当前路径阻塞",
    "revoked": "已撤回",
    "superseded": "已被替代",
}
COLORS = ("#187d88", "#bc5b35", "#6855a5", "#708137", "#bc4774")
STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f3f5f5;color:#18333d;
font:16px/1.7 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1240px;
margin:auto;padding:32px 24px}h1{font-size:34px;line-height:1.35}h2{font-size:24px}
h3{font-size:18px}section,figure{background:white;border:1px solid #dce4e4;
border-radius:12px;padding:22px;margin:20px 0}figure{margin:0;min-width:0}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.axes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.axes article{border-top:3px solid #187d88;padding-top:10px}.banner{padding:18px;
background:#e5f0ed;border-left:5px solid #187d88}.draft,.warning{background:#fff1dd;
border-left:5px solid #b66822;padding:16px}.muted{color:#597078;font-size:14px}
.tag{display:inline-block;background:#edf1f3;border-radius:20px;padding:2px 10px;
font-size:13px}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;border-bottom:1px solid #dde5e5;padding:10px;vertical-align:top}
th{background:#edf4f4}a{color:#126573}pre,blockquote{white-space:pre-wrap;
overflow-wrap:anywhere;font-size:14px}blockquote{background:#f4f7f7;padding:14px;
margin:10px 0}.scroll{overflow:auto}svg{width:100%;height:auto}.legend{display:flex;
flex-wrap:wrap;gap:15px;font-size:13px}.claim{border-top:1px solid #dce4e4;padding:16px 0}
details{margin:12px 0}summary{cursor:pointer;color:#126573}nav{display:flex;gap:16px;
flex-wrap:wrap}footer{margin:30px 0;font-size:13px;color:#597078}
@media(max-width:800px){main{padding:18px 12px}.grid,.axes{grid-template-columns:1fr}}
"""


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _anchor(value: object) -> str:
    return "ref-" + hashlib.sha256(str(value).encode()).hexdigest()[:20]


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value if value is not None else "未提供")


def _safe_href(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 for char in raw):
        return None
    if urlsplit(raw).scheme.lower() not in {"", "https", "http", "file"}:
        # Native Windows paths are converted explicitly, never treated as schemes.
        if len(raw) > 2 and raw[1] == ":" and raw[2] in ("/", "\\"):
            return Path(raw).as_uri()
        return None
    if raw.startswith("//"):
        return None
    return raw


def _catalog(packet: dict, state: dict | None = None) -> dict:
    state = state or {}
    catalog = {}
    for key in ("facts", "calculated_values"):
        items = packet.get(key, {})
        if isinstance(items, dict):
            catalog.update(items)
        else:
            catalog.update({str(item["id"]): item for item in items})
    catalog.update(packet.get("external_evidence", {}))
    catalog.update(state.get("calculations", {}))
    for key, estimate in state.get("estimates", {}).items():
        catalog[key] = estimate
        for part in ("low", "base", "high"):
            catalog[f"{key}.{part}"] = {
                "kind": "estimate",
                "value": estimate.get(part),
                "refs": [key],
            }
    for key, text in state.get("supplemental_pages", {}).items():
        catalog[key] = {"text": text, "kind": "supplemental_original"}
    for chapter in packet.get("chapters", []):
        catalog[str(chapter["id"])] = chapter
    for doc in packet.get("documents", []):
        doc_id = str(doc.get("document_id", doc.get("document", "")))
        catalog[doc_id] = {k: v for k, v in doc.items() if k != "pages"}
        for page, text in doc.get("pages", {}).items():
            catalog[f"{doc_id}:p{page}"] = {
                "document_id": doc_id,
                "page": page,
                "text": text,
                "source_path": doc.get("source_path"),
            }
    return catalog


def _refs(values: object, catalog: dict) -> str:
    if not values:
        return '<span class="muted">未提供引用</span>'
    if not isinstance(values, list):
        values = [values]
    links = []
    for value in values:
        key = (
            str(value.get("id", value.get("ref", "")))
            if isinstance(value, dict)
            else str(value)
        )
        if key in catalog:
            links.append(f'<a href="#{_anchor(key)}">{esc(key)}</a>')
        else:
            links.append(f'<span class="warning">未解析引用：{esc(key)}</span>')
    return " · ".join(links)


def _fields(value: dict, labels: dict) -> str:
    return "".join(
        f"<p><strong>{esc(label)}：</strong>{esc(_text(value[key]))}</p>"
        for key, label in labels.items()
        if value.get(key) not in (None, "", [])
    )


def chart(
    rows: list[dict],
    metric: str,
    title: str,
    *,
    currency: str | None = "CNY",
    calculated: bool = False,
    percent: bool = False,
    monthly: bool = False,
    multiple: bool = False,
) -> str:
    """Plot annual values; gaps split paths and incompatible currencies stay apart."""
    annual = [
        row
        for row in rows
        if (monthly or str(row.get("period", "")).endswith("-12-31"))
        and str(row.get("kind", "annual")).lower() not in {"h1", "half_year", "quarter"}
        and (percent or multiple or row.get("currency") == currency)
    ]
    observed = sorted({str(row["period"]) for row in annual})
    if monthly and observed:
        first = int(observed[0][:4]) * 12 + int(observed[0][5:7]) - 1
        last = int(observed[-1][:4]) * 12 + int(observed[-1][5:7]) - 1
        periods = [
            f"{number // 12:04d}-{number % 12 + 1:02d}"
            for number in range(first, last + 1)
        ]
    else:
        periods = (
            [
                f"{year}-12-31"
                for year in range(int(observed[0][:4]), int(observed[-1][:4]) + 1)
            ]
            if observed
            else []
        )
    issuers = sorted(
        {str(row.get("issuer_id", row.get("issuer", ""))) for row in annual}
    )
    series = []
    for index, issuer in enumerate(issuers):
        own = [
            row
            for row in annual
            if str(row.get("issuer_id", row.get("issuer", ""))) == issuer
        ]
        by_period = {
            (str(row["period"])[:7] if monthly else str(row["period"])): row
            for row in own
        }
        values = []
        for period in periods:
            value = _number(
                by_period.get(period, {})
                .get("calculated" if calculated else "values", {})
                .get(metric)
            )
            values.append(None if value is None else value * (100 if percent else 1))
        series.append(
            (own[0].get("company_name", issuer), COLORS[index % len(COLORS)], values)
        )
    present = [
        value for _, _, values in series for value in values if value is not None
    ]
    if not present:
        return f'<figure><h3>{esc(title)}</h3><p class="muted">无可比数据；没有按零值绘制。</p></figure>'
    scale = 1 if percent or multiple or metric == "bvps" else 100000000
    low, high = min(0, min(present) / scale), max(0, max(present) / scale)
    high = high + (high - low) * 0.12 if high > low else low + 1

    def x(index):
        return 64 + index * 450 / max(1, len(periods) - 1)

    def y(value):
        return 244 - (value / scale - low) / (high - low) * 210

    unit = (
        "%"
        if percent
        else (
            (currency or "原币未明确") + "/股"
            if metric == "bvps"
            else "亿 " + (currency or "原币未明确")
        )
    )
    if multiple:
        unit = "倍"
    svg = [
        f'<svg viewBox="0 0 560 290" role="img" aria-label="{esc(title)}"><title>{esc(title)}（{esc(unit)}）</title>'
    ]
    for tick in range(5):
        value = low + (high - low) * tick / 4
        yy = y(value * scale)
        svg.append(
            f'<line x1="64" y1="{yy:.2f}" x2="530" y2="{yy:.2f}" stroke="#e2e9e9"/><text x="58" y="{yy + 4:.2f}" text-anchor="end" font-size="11">{value:.1f}</text>'
        )
    for index, period in enumerate(periods):
        if (
            monthly
            and index % max(1, len(periods) // 7) != 0
            and index != len(periods) - 1
        ):
            continue
        svg.append(
            f'<text x="{x(index):.2f}" y="270" text-anchor="middle" font-size="11">{esc(period[:7] if monthly else period[:4])}</text>'
        )
    for name, color, values in series:
        path, previous = [], False
        for index, value in enumerate(values):
            if value is None:
                previous = False
                continue
            path.append(f"{'L' if previous else 'M'}{x(index):.2f},{y(value):.2f}")
            previous = True
            svg.append(
                f'<circle cx="{x(index):.2f}" cy="{y(value):.2f}" r="3" fill="{color}"><title>{esc(name)} {esc(periods[index])}: {value / scale:,.2f} {esc(unit)}</title></circle>'
            )
        svg.append(
            f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        )
    svg.append(
        '</svg><div class="legend">'
        + "".join(
            f'<span style="color:{color}">● {esc(name)}</span>'
            for name, color, _ in series
        )
        + "</div>"
    )
    span = "月末当时可知信息" if monthly else "仅完整年度"
    return f'<figure><h3>{esc(title)}</h3>{"".join(svg)}<figcaption class="muted">{esc(unit)}；{span}，缺值断线。不同币种金额分图，不据此强制排名。</figcaption></figure>'


def _result(result: dict, catalog: dict, heading: str, draft: bool = False) -> str:
    body = [
        f'<section class="{"draft" if draft else "reviewed"}"><h2>{esc(heading)}</h2>'
    ]
    if draft:
        body.append("<p>未经独立审查；不是有效投资结论，不代表验收通过。</p>")
    body += [
        f"<h3>{esc(result.get('title', ''))}</h3>",
        f"<p>{esc(_text(result.get('summary', '')))}</p>",
        '<div class="axes">',
    ]
    axes = result.get("axes", {})
    for key, label in AXES.items():
        item = axes.get(key, {})
        if not isinstance(item, dict):
            item = {"basis": item}
        body.append(
            f'<article><h3>{label}</h3><span class="tag">{esc(item.get("status", "尚未形成"))}</span>'
        )
        body.append(
            _fields(
                item,
                {"basis": "依据", "counterevidence": "反证", "limitations": "限制"},
            )
        )
        body.append(_refs(item.get("refs"), catalog) + "</article>")
    body.append("</div><h3>六项研究结论</h3>")
    claims = result.get("claims", [])
    for key, label in CLAIMS.items():
        matched = [item for item in claims if item.get("key") == key]
        body.append(f'<article class="claim"><h3>{label}</h3>')
        if not matched:
            body.append('<p class="muted">尚未形成此项结论。</p>')
        for item in matched:
            body.append(f'<span class="tag">{esc(item.get("status", "未标注"))}</span>')
            body.append(
                _fields(
                    item,
                    {
                        "basis": "判断依据",
                        "counterevidence": "反证",
                        "limitations": "限制",
                        "sensitivity": "假设与敏感性",
                        "breaker": "失效条件",
                    },
                )
            )
            body.append(
                "<p>事实引用："
                + _refs(item.get("refs"), catalog)
                + "</p><p>计算引用："
                + _refs(item.get("calc_refs"), catalog)
                + "</p>"
            )
        body.append("</article>")
    for item in result.get("sections", []):
        body.append(f"<h3>{esc(item.get('title', ''))}</h3>")
        paragraphs = item.get("paragraphs", [])
        if isinstance(paragraphs, str):
            paragraphs = [paragraphs]
        body.extend(f"<p>{esc(_text(paragraph))}</p>" for paragraph in paragraphs)
        body.append(_refs(item.get("refs"), catalog))
    body.append("<h3>利润、现金与价值桥接</h3>")
    if not result.get("bridges"):
        body.append('<p class="muted">尚无可展示桥接，不能视为已完成归因。</p>')
    for item in result.get("bridges", []):
        body.append(
            f'<article class="claim"><h3>{esc(item.get("title", item.get("id", "")))}</h3>'
        )
        body.append(_fields(item, {"explanation": "解释", "residual": "未解释残差"}))
        body.append(
            _refs(item.get("refs"), catalog)
            + "<br>"
            + _refs(item.get("calc_refs"), catalog)
            + "</article>"
        )
    body.append(_fields(result, {"limitations": "总体限制"}) + "</section>")
    return "".join(body)


def _table(items: list[dict], title: str) -> str:
    if not items:
        return f'<h3>{esc(title)}</h3><p class="muted">无可用记录。</p>'
    keys = list(dict.fromkeys(key for item in items for key in item))
    header = "".join(f"<th>{esc(key)}</th>" for key in keys)
    rows = "".join(
        "<tr>"
        + "".join(f"<td>{esc(_text(item.get(key)))}</td>" for key in keys)
        + "</tr>"
        for item in items
    )
    return f'<h3>{esc(title)}</h3><div class="scroll"><table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div>'


def _cost_anchors(anchors: list[dict], catalog: dict) -> str:
    if not anchors:
        return '<h3>已结构化的行业成本锚</h3><p class="muted">尚无已结构化锚点；不能据此认定缺少成本优势。</p>'
    body = [
        '<h3>已结构化的行业成本锚</h3><div class="scroll"><table><thead><tr><th>公司／期间／产品范围</th><th>单位收入</th><th>单位成本</th><th>币种与可比限制</th><th>原始输入引用</th></tr></thead><tbody>'
    ]
    for item in anchors:
        values = item.get("values", {})
        revenue, cost = (
            _number(values.get("revenue_per_tonne")),
            _number(values.get("cost_per_tonne")),
        )
        revenue_text = "—" if revenue is None else f"{revenue:,.2f} 元/吨"
        cost_text = "—" if cost is None else f"{cost:,.2f} 元/吨"
        body.append(
            "<tr><td>"
            + esc(item.get("company_name", item.get("issuer_id")))
            + "<br>"
            + esc(item.get("period"))
            + "<br>"
            + esc(_text(item.get("scope")))
            + "</td><td>"
            + revenue_text
            + "</td><td>"
            + cost_text
            + "</td><td>"
            + esc(item.get("currency") or "原文币种未明确")
            + "<br>"
            + esc(_text(item.get("comparability_limit", "尚未提供同质可比依据")))
            + "</td><td>"
            + _refs(item.get("refs"), catalog)
            + "</td></tr>"
        )
    return "".join(body) + "</tbody></table></div>"


def _panel_tables(packet: dict, catalog: dict) -> str:
    """Show code-derived values with exact spans and navigable input references."""
    metrics = {
        "revenue": "营业收入",
        "net_profit_parent": "归母净利润",
        "cfo": "CFO",
        "capex": "现金 Capex",
        "fcf_proxy": "基础 FCF",
        "bvps": "BVPS",
        "cfo_conversion": "CFO／合并净利润",
        "gross_margin": "毛利率",
        "simple_roe": "简单平均净资产 ROE",
    }
    body = [
        '<details><summary>逐公司、逐期数值及原文引用</summary><p class="muted">金额保留原币单位；比率显示为小数。基础 FCF 不等于正常化股东现金流。</p><div class="scroll"><table><thead><tr><th>公司／期间／币种</th>'
    ]
    body.extend(f"<th>{esc(label)}</th>" for label in metrics.values())
    body.append("</tr></thead><tbody>")
    for row in packet.get("rows", []):
        body.append(
            "<tr><td>"
            + esc(row.get("company_name", row.get("issuer_id")))
            + "<br>"
            + esc(row.get("period"))
            + "<br>"
            + esc(row.get("currency"))
            + "</td>"
        )
        for metric in metrics:
            value = row.get("values", {}).get(
                metric, row.get("calculated", {}).get(metric)
            )
            numeric = _number(value)
            display = "—" if numeric is None else f"{numeric:,.4f}"
            references = row.get("refs", {}).get(metric, [])
            body.append(
                "<td>" + display + "<br>" + _refs(references, catalog) + "</td>"
            )
        body.append("</tr>")
    body.append("</tbody></table></div></details>")
    growth_rows, conversion_rows = [], []
    names = {
        r.get("issuer_id"): r.get("company_name", r.get("issuer_id"))
        for r in packet.get("rows", [])
    }
    for issuer, item in packet.get("metrics", {}).items():
        for metric, growth in item.get("growth", {}).items():
            growth_rows.append(
                {
                    "公司": names.get(issuer, issuer),
                    "指标": metrics.get(metric, metric),
                    "CAGR": "—"
                    if growth.get("value") is None
                    else f"{growth['value']:.2%}",
                    "起始年": growth.get("start_year"),
                    "结束年": growth.get("end_year"),
                    "增长区间数": growth.get("intervals"),
                    "缺年": growth.get("missing_years", []),
                }
            )
        cash = item.get("cumulative_cash_conversion", {})
        segments = item.get("cumulative_cash_conversion_segments", [])
        selections = segments or [cash]
        for segment in selections:
            conversion_rows.append(
                {
                    "公司": names.get(issuer, issuer),
                    "累计现金转化": "—"
                    if segment.get("value") is None
                    else f"{segment['value']:.2%}",
                    "CFO合计（亿原币）": "—"
                    if segment.get("cfo_sum") is None
                    else f"{segment['cfo_sum'] / 1e8:.2f}",
                    "合并净利润合计（亿原币）": "—"
                    if segment.get("net_profit_total_sum") is None
                    else f"{segment['net_profit_total_sum'] / 1e8:.2f}",
                    "实际期间": segment.get("periods", []),
                    "币种": segment.get("currency")
                    or "列报币种待核，勿与其他公司金额混加",
                }
            )
    body.append(_table(growth_rows, "增长率与实际区间"))
    body.append(
        _table(
            conversion_rows, "累计现金兑现（连续且币种一致的区间，勿跨不同区间排名）"
        )
    )
    return "".join(body)


def _readable_comparison(packet: dict, catalog: dict) -> str:
    """Expose aligned facts before charts without manufacturing an investment thesis."""
    companies = packet.get("spec", {}).get("companies", [])
    rows = packet.get("rows", [])
    annual = [r for r in rows if str(r.get("period", "")).endswith("12-31")]
    years = sorted({r["period"] for r in annual})
    body = [
        (
            '<section id="snapshot"><h2>先看同一期间的经营与现金</h2>'
            "<p>以下由财报事实直接计算；先比较利润是否变成现金，再阅读管理层对变化的解释。"
            "不据此自动认定低估、护城河或正常化股东现金流。</p>"
        )
    ]
    metrics = [
        ("revenue", "收入", False),
        ("net_profit_parent", "归母利润", False),
        ("cfo", "CFO", False),
        ("fcf_proxy", "基础 FCF", True),
        ("gross_margin", "毛利率", True),
        ("bvps", "归母权益/股 BVPS", True),
        ("common_equity_bvps", "扣其他权益后/股", True),
    ]
    if years:
        latest = years[-1]
        body.append(
            f"<h3>{esc(latest[:4])} 年度横向比较</h3>"
            '<p class="muted">金额为亿元；每股值为元/股。BVPS以期末总股数计算；第二种口径仅在其他权益工具金额有披露时扣除，不代表已扣全部库存股或加权平均股数。每个值保留来源；空格不是零。</p>'
            '<div class="scroll"><table><tr><th>公司 / 币种</th>'
        )
        body.extend(f"<th>{label}</th>" for _, label, _ in metrics)
        body.append("</tr>")
        for company in companies:
            row = next(
                (
                    r
                    for r in annual
                    if r["issuer_id"] == company["issuer_id"] and r["period"] == latest
                ),
                {},
            )
            body.append(
                f"<tr><th>{esc(company.get('name', company['issuer_id']))}<br>"
                f"{esc(row.get('currency') or '币种待核')}</th>"
            )
            for key, _, calculated in metrics:
                value = _number(
                    row.get("calculated" if calculated else "values", {}).get(key)
                )
                refs = row.get("refs", {}).get(key, [])
                if calculated:
                    refs = [
                        k
                        for k, v in packet.get("calculated_values", {}).items()
                        if v.get("issuer_id") == company["issuer_id"]
                        and v.get("period") == latest
                        and v.get("metric") == key
                    ]
                display = (
                    "—"
                    if value is None
                    else f"{value:.2%}"
                    if key == "gross_margin"
                    else f"{value:.2f}"
                    if key in {"bvps", "common_equity_bvps"}
                    else f"{value / 1e8:,.2f}"
                )
                link = (
                    f'<a href="#{_anchor(refs[0])}">{display}</a>' if refs else display
                )
                body.append(f"<td>{link}</td>")
            body.append("</tr>")
        body.append("</table></div>")
    body.append(
        '<h3>实际可用年度：不能把部分字段当全年齐备</h3><div class="scroll"><table>'
        "<tr><th>公司</th>" + "".join(f"<th>{y[:4]}</th>" for y in years) + "</tr>"
    )
    core = (
        "revenue",
        "cost",
        "net_profit_parent",
        "net_profit_total",
        "cfo",
        "capex",
        "parent_equity",
    )
    for company in companies:
        body.append(f"<tr><th>{esc(company.get('name', company['issuer_id']))}</th>")
        for year in years:
            row = next(
                (
                    r
                    for r in annual
                    if r["issuer_id"] == company["issuer_id"] and r["period"] == year
                ),
                {},
            )
            count = sum(_number(row.get("values", {}).get(k)) is not None for k in core)
            body.append(f"<td>{count}/7</td>")
        body.append("</tr>")
    body.append(
        '</table></div><p class="muted">七项为收入、成本、归母利润、合并利润、CFO、现金Capex、归母净资产。'
        "覆盖数只说明可计算范围，不是投资筛选门槛；重述和原始披露须分别追溯。</p></section>"
    )
    body.append(
        '<section id="management"><h2>管理层怎样解释变化：按公司、年度读原文</h2>'
        "<p>这是公司自己的陈述，尚未由研究者验证因果关系。完整章节保留表头、续表和附注入口。</p>"
    )
    chapters = packet.get("chapters", [])
    for company in companies:
        body.append(f"<h3>{esc(company.get('name', company['issuer_id']))}</h3>")
        docs = sorted(
            (
                d
                for d in packet.get("documents", [])
                if d.get("issuer_id") == company["issuer_id"]
            ),
            key=lambda d: d.get("period_end", ""),
            reverse=True,
        )
        for doc in docs:
            mda = [
                c
                for c in chapters
                if c.get("document_id") == doc["document_id"] and c.get("kind") == "mda"
            ]
            body.append(
                f"<details><summary>{esc(doc.get('period_end'))} · 管理层讨论 · {len(mda)} 个章节</summary>"
            )
            if not mda:
                body.append("<p>尚未定位完整章节；不能认定未披露。</p>")
            for chapter in mda:
                body.append(
                    f"<h4>{esc(chapter.get('title'))}</h4><p>原PDF页码："
                    f"{esc(chapter.get('pages', []))} · {_refs([chapter['id']], catalog)}</p>"
                    f"<blockquote>{esc(chapter.get('text', ''))}</blockquote>"
                )
            body.append("</details>")
    body.append("</section>")
    return "".join(body)


def _original_coverage_section(packet):
    from .longitudinal_discovery import original_report_coverage

    spec = packet.get("spec", {})
    if not all(key in spec for key in ("companies", "as_of", "group_id")):
        return "<p>原年报覆盖：研究范围元信息不足，尚未统计。</p>"
    coverage = original_report_coverage(spec, packet.get("documents", []))
    rows = []
    for company in spec["companies"]:
        periods = [
            r for r in coverage["rows"] if r["issuer_id"] == company["issuer_id"]
        ]
        missing = [
            r["period_end"] for r in periods if r["status"] == "missing_original"
        ]
        rows.append(
            {
                "公司": company.get("name") or company["issuer_id"],
                "原件取得": f"{len(periods) - len(missing)}/{len(periods)}",
                "缺失原报告期间": "、".join(missing) or "无",
            }
        )
    return (
        "<h3>原年报和中报覆盖（与财务数字覆盖分开）</h3>"
        f"<p>取得 {coverage['available']}/{coverage['expected']} 个期间的原报告；"
        "比较期数字齐备不代表当年管理层讨论或附注已取得。"
        "缺失仅指本次资料中没有原件，不表示公司未披露。</p>"
        + _table(rows, "原报告缺口")
    )


def render_longitudinal_report(
    packet: dict, state: dict | None = None, output: Path | str | None = None
) -> str:
    """Render prepare/stage/final artifacts; only workflow-approved content is reviewed."""
    state = state or {}
    packet = state.get("effective_packet", packet)
    spec = packet.get("spec", {})
    title = str(spec.get("title", spec.get("group_id", "长期同行研究")))
    status = state.get("status", "prepared")
    catalog = _catalog(packet, state)
    display_status = STATUS.get(status, status)
    if status in {"accepted", "reviewed_stage"} and not state.get("approved_result"):
        display_status = "结果状态不一致 · 无已审查结果"
    body = [
        f'<header><p class="muted">FUNDAMENTAL QUANT · 长期同行研究</p><h1>{esc(title)}</h1><p class="banner">{esc(display_status)}</p><p class="muted">截止日：{esc(spec.get("as_of", spec.get("cutoff", "未提供")))} · 报告版本：{esc(state.get("report_version", "尚未发布"))}</p></header>',
        '<nav><a href="#research">研究结果</a><a href="#snapshot">同期对比</a><a href="#management">管理层原文</a><a href="#data">长期数据</a><a href="#coverage">覆盖与缺口</a><a href="#cost">调用与审查</a><a href="#evidence">原文与计算</a></nav>',
        '<div id="research"></div>',
    ]
    approved = state.get("approved_result")
    draft = state.get("research_result")
    if status in {"revoked", "superseded"}:
        body.append(
            '<section class="warning"><h2>旧结论已失效</h2><p>本页不再展示旧结果为有效结论。</p></section>'
        )
    else:
        if approved:
            heading = "已审查最终报告" if status == "accepted" else "已审查阶段结果"
            body.append(_result(approved, catalog, heading))
        if draft and (not approved or draft != approved):
            body.append(_result(draft, catalog, "最新研究草稿", draft=True))
        if not approved and not draft:
            body.append(
                "<section><h2>尚未形成研究结论</h2><p>下方是冻结资料和代码计算，不是研究验收报告。尚未运行或通过 DeepSeek 研究与独立审查。</p></section>"
            )
        if status in {"accepted", "reviewed_stage"} and not approved:
            body.append(
                '<p class="warning">状态与结果不一致：没有 approved_result，不能展示为已审查报告。</p>'
            )
    body.append(_readable_comparison(packet, catalog))
    body.append(
        '<section id="data"><h2>长期同业比较</h2><p>事实、计算与模型解释分别展示；缺失资料不表示公司不合格。</p><div class="grid">'
    )
    rows = packet.get("rows", [])
    currencies = sorted({row.get("currency") for row in rows if row.get("currency")})
    monetary_charts = [
        ("revenue", "营业收入", False),
        ("net_profit_parent", "归母净利润", False),
        ("cfo", "经营现金流 CFO", False),
        ("fcf_proxy", "基础 FCF（CFO − 现金 Capex）", True),
        ("bvps", "每股归母净资产 BVPS", True),
    ]
    for currency in currencies:
        for metric, label, calculated in monetary_charts:
            body.append(
                chart(rows, metric, label, currency=currency, calculated=calculated)
            )
    body.append(
        chart(rows, "cfo_conversion", "CFO／合并净利润", calculated=True, percent=True)
    )
    body.append(chart(rows, "gross_margin", "毛利率", calculated=True, percent=True))
    body.append(
        chart(
            rows,
            "simple_roe",
            "归母利润／平均归母净资产",
            calculated=True,
            percent=True,
        )
    )
    unknown = [row for row in rows if not row.get("currency")]
    if unknown:
        body.append(
            '<p class="warning">部分原文只注明金额单位，币种尚未明确。下方按公司独立展示原始数值趋势，不进行跨公司金额高低排名；比率沿用已核实同一报表口径。</p>'
        )
        for issuer in sorted({str(row.get("issuer_id")) for row in unknown}):
            own = [row for row in unknown if str(row.get("issuer_id")) == issuer]
            name = own[0].get("company_name", issuer)
            for metric, label, calculated in monetary_charts:
                body.append(
                    chart(
                        own,
                        metric,
                        f"{name} · {label}",
                        currency=None,
                        calculated=calculated,
                    )
                )
    body.append(
        "</div><details><summary>增长率、累计现金转化与计算覆盖</summary><pre>"
        + esc(_text(packet.get("metrics", {})))
        + "</pre></details>"
    )
    body.append(_panel_tables(packet, catalog))
    body.append(_cost_anchors(packet.get("cost_anchors", []), catalog))
    reading = packet.get("reading_catalog", {})
    if reading:
        body.append(
            "<details><summary>行业成本锚待查问题与资料入口</summary><p>以下是原文入口，尚不是经研究和审查确认的单位成本结论。</p><pre>"
            + esc(_text(reading.get("anchor_questions", [])))
            + "</pre>"
        )
        for item in reading.get("documents", []):
            body.append(
                "<p><strong>"
                + esc(item.get("document_id"))
                + "</strong> · 管理层讨论："
                + _refs(item.get("full_mda_chapters"), catalog)
                + "</p><p>成本及营运资本附注："
                + _refs(item.get("cost_anchor_candidates"), catalog)
                + "</p>"
            )
        body.append("</details>")
    for metric, label in [
        ("inventory_to_annual_cost", "存货／年度营业成本"),
        ("contract_assets_to_annual_revenue", "合同资产／年度收入"),
        ("receivables_to_annual_revenue", "应收／年度收入"),
        ("interest_to_operating_profit", "利息费用／营业利润"),
    ]:
        if any(
            _number(row.get("calculated", {}).get(metric)) is not None for row in rows
        ):
            body.append(chart(rows, metric, label, calculated=True, percent=True))
    body.append(
        '<p class="muted">上述比率是财务敞口指标，不是存货实际周转天数、项目期限或产品实物单位成本。</p>'
    )
    valuations = packet.get("historical_valuations", {})
    valuation_rows = [
        {
            "issuer_id": item.get("issuer_id"),
            "company_name": next(
                (
                    row.get("company_name")
                    for row in rows
                    if row.get("issuer_id") == item.get("issuer_id")
                ),
                item.get("issuer_id"),
            ),
            "period": item.get("month_end", ""),
            "kind": "month",
            "currency": item.get("currency"),
            "calculated": {key: item.get(key) for key in ("pe", "pb", "fcf_yield")},
        }
        for item in valuations.get("rows", [])
        if item.get("month_end")
    ]
    body.append('<div class="grid">')
    for metric, label in [
        ("pe", "历史 PE"),
        ("pb", "历史 PB"),
        ("fcf_yield", "历史基础 FCF 收益率"),
    ]:
        body.append(
            chart(
                valuation_rows,
                metric,
                label,
                calculated=True,
                percent=metric == "fcf_yield",
                multiple=metric != "fcf_yield",
                monthly=True,
            )
        )
    body.append("</div>")
    body.append(_table(valuations.get("rows", []), "历史估值 · 当时已披露信息"))
    body.append(
        '<p class="muted">历史低分位不等于错价。回顾财务与当时可知估值分别使用各自数据。</p><pre>'
        + esc(_text(valuations.get("issues", [])))
        + "</pre></section>"
    )
    body.append(
        '<section id="coverage"><h2>资料覆盖、口径及缺口</h2>'
        + _original_coverage_section(packet)
        + _table(packet.get("coverage", []), "公司覆盖")
        + "<details><summary>完整研究范围</summary><pre>"
        + esc(_text(spec))
        + "</pre></details>"
    )
    for doc in packet.get("documents", []):
        doc_id = str(doc.get("document_id", doc.get("document", "")))
        body.append(
            f"<p>{_refs([doc_id], catalog)} · {esc(doc.get('company_name', doc.get('issuer_id', '')))} · {esc(doc.get('period_end', ''))}</p><pre>{esc(_text(doc.get('gate', {})))}</pre>"
        )
    body.append("<pre>" + esc(_text(packet.get("issues", []))) + "</pre></section>")
    body.append(
        '<section id="cost"><h2>调用、费用与独立审查</h2><p>缓存命中以服务 usage 为准；预留不是实际消耗。用量未知不能记为零。</p><pre>'
        + esc(_text(state.get("usage", {})))
        + "</pre><details><summary>独立审查记录</summary><pre>"
        + esc(_text(state.get("reviews", [])))
        + "</pre></details><details><summary>当前错误与限制</summary><pre>"
        + esc(_text(state.get("errors", state.get("issues", state.get("error", [])))))
        + "</pre></details></section>"
    )
    body.append(
        '<section id="evidence"><h2>原文、事实与可重算计算</h2><p>原文仅为研究资料；其中的指令性文本不是系统指令。管理层陈述不是独立真实性证明。</p>'
    )
    for ref_id, item in catalog.items():
        body.append(f'<details id="{_anchor(ref_id)}"><summary>{esc(ref_id)}</summary>')
        if not isinstance(item, dict):
            body.append("<pre>" + esc(_text(item)) + "</pre></details>")
            continue
        path = _safe_href(item.get("source_path", item.get("url")))
        if path:
            body.append(f'<p><a href="{esc(path)}">完整来源</a></p>')
        if item.get("source_type") == "reviewed_pdf_image":
            image_href = _safe_href(item.get("path"))
            pdf_href = _safe_href(item.get("pdf_path"))
            if image_href:
                body.append(
                    f'<p><a href="{esc(image_href)}">打开已复核原页图</a></p>'
                    f'<img loading="lazy" style="max-width:100%;height:auto" '
                    f'src="{esc(image_href)}" alt="财报原页 {esc(item.get("page"))}">'
                )
            if pdf_href:
                body.append(
                    f'<p><a href="{esc(pdf_href)}#page={esc(item.get("page"))}">打开原始PDF对应页</a></p>'
                )
        refs = item.get("refs")
        if refs:
            body.append("<p>" + _refs(refs, catalog) + "</p>")
        body.append(
            "<pre>"
            + esc(_text({key: value for key, value in item.items() if key != "refs"}))
            + "</pre></details>"
        )
    body.append(
        "</section><footer>此页面不创建发布资格，也不发送通知。正式发布由研究工作流及独立审查决定；草稿与已审查结果分别保留。</footer>"
    )
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Cache-Control" content="no-store"><title>'
        + esc(title)
        + "</title><style>"
        + STYLE
        + "</style></head><body><main>"
        + "".join(body)
        + "</main></body></html>"
    )
    if output is not None:
        _atomic(Path(output), document)
    return document


def render_longitudinal_email(packet: dict, state: dict, version: str) -> str:
    """Mail projection of the same approved result, without megabytes of originals.

    The release adapter validates publication eligibility and stores this exact
    projection alongside the full report. Local source links are deliberately
    plain reference identifiers because recipients cannot open local paths.
    """
    approved = state.get("approved_result")
    if state.get("status") != "accepted" or not isinstance(approved, dict):
        raise ValueError("email_requires_accepted_approved_result")
    if not version:
        raise ValueError("email_requires_publication_version")
    packet = state.get("effective_packet", packet)
    spec = packet.get("spec", {})
    catalog = _catalog(packet, state)
    result_html = _result(approved, catalog, "已审查研究报告")
    result_html = re.sub(
        r'<a href="#ref-[^"]+">(.*?)</a>', r"<span>\1</span>", result_html
    )
    referenced = set()
    for item in (
        list(approved.get("axes", {}).values())
        + approved.get("claims", [])
        + approved.get("bridges", [])
        + approved.get("sections", [])
    ):
        if isinstance(item, dict):
            for ref in item.get("refs", []) + item.get("calc_refs", []):
                if isinstance(ref, str):
                    referenced.add(ref)
    # Expand calculation inputs to show which original records support them;
    # never copy entire pages or provider reasoning into an email.
    pending = list(referenced)
    while pending:
        current = catalog.get(pending.pop(), {})
        if isinstance(current, dict):
            for ref in current.get("refs", []):
                if isinstance(ref, str) and ref not in referenced:
                    referenced.add(ref)
                    pending.append(ref)
    rows = []
    for ref in sorted(referenced):
        entry = catalog.get(ref, {})
        if isinstance(entry, dict):
            record = {
                key: entry[key]
                for key in (
                    "document_id",
                    "issuer_id",
                    "period",
                    "page",
                    "metric",
                    "value",
                    "unit",
                    "currency",
                    "expression",
                    "refs",
                )
                if key in entry
            }
        else:
            record = {}
        rows.append({"引用": ref, "定位与计算": record or "见同版本完整报告原文索引"})
    title = str(spec.get("title", spec.get("group_id", "长期同行研究")))
    body = (
        '<p style="font-size:12px;color:#597078">FUNDAMENTAL QUANT</p><h1>'
        + esc(title)
        + "</h1><p><strong>发布版本：</strong>"
        + esc(version)
        + "</p><p><strong>研究截止日：</strong>"
        + esc(spec.get("as_of", "未提供"))
        + "</p>"
        + result_html
    )
    body += "<section><h2>资料覆盖与原文索引</h2><p>本邮件使用同一已审查版本的完整研究文字；原始财报及图表请在项目完整报告中查看。引用编号保留，不提供无法在邮件中访问的本地链接。</p>"
    body += (
        _table(packet.get("coverage", []), "覆盖范围")
        + _table(rows, "证据与计算索引")
        + "</section>"
    )
    body += (
        "<section><h2>真实调用与费用记录</h2><p>预留不等于实际消耗；未知用量不按零计费。</p><pre>"
        + esc(_text(state.get("usage", {})))
        + "</pre></section>"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + esc(title)
        + "</title><style>"
        + STYLE
        + '</style></head><body style="margin:0;background:#f3f5f5;color:#18333d;font-family:Arial,Microsoft YaHei,sans-serif;line-height:1.7"><main style="max-width:900px;margin:auto;padding:24px">'
        + body
        + "</main></body></html>"
    )
