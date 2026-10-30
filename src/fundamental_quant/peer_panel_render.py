"""Readable offline longitudinal comparison with self-contained charts."""

import html
from pathlib import Path
from string import Template

from .fr_pack import read_json

COMPANIES = [
    ("a_share:000568", "泸州老窖", "#c26732"),
    ("a_share:000858", "五粮液", "#16817a"),
    ("a_share:600519", "贵州茅台", "#536abb"),
]
STYLE = """*{box-sizing:border-box}body{margin:0;background:#f5f3ee;color:#203238;font:16px/1.8 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1180px;margin:auto;padding:40px 28px}h1{font-size:40px;line-height:1.25;letter-spacing:-1px}h2{font-size:26px;line-height:1.4;margin-top:0}h3{font-size:19px}p{max-width:980px}a{color:#146f73}section{background:#fff;border:1px solid #e3e3dc;border-radius:14px;padding:30px;margin:24px 0}.eyebrow{font-size:13px;letter-spacing:2px;color:#58716d}.muted{color:#647371;font-size:14px}.lead{font-size:20px}.tag{display:inline-block;border:1px solid #cbd4ca;border-radius:20px;padding:2px 12px;font-size:13px}.cards,.charts{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.cards article{border-top:3px solid #c26732;padding:12px 0}.cards strong{display:block;font-size:28px;color:#963f16}.charts{grid-template-columns:repeat(2,1fr)}figure{margin:0;border:1px solid #e4e7e4;padding:16px;border-radius:8px}figcaption{font-size:14px;color:#596d6a}svg{width:100%;height:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:right;vertical-align:top;padding:12px 9px;border-bottom:1px solid #e4e7e4}th:first-child,td:first-child{text-align:left}th{background:#f0f4ef}td em{font-style:normal;color:#c26732}.scroll{overflow:auto}nav{display:flex;gap:20px;flex-wrap:wrap;margin:24px 0}.warning{border-left:4px solid #c26732;background:#fff3e5;padding:15px}.claim{border-left:3px solid #16817a;padding:0 0 0 18px}.legend{display:flex;gap:22px;flex-wrap:wrap;font-size:14px}.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:6px}blockquote{margin:14px 0;padding:14px 18px;background:#f3f6f3;font-size:14px;white-space:pre-wrap}details{padding:10px 0}summary{cursor:pointer;color:#146f73}pre{white-space:pre-wrap;word-break:break-word;font-size:13px}.pill{display:inline-block;font-size:12px;background:#f0f4ef;padding:2px 8px}footer{padding:20px;color:#596d6a}@media(max-width:800px){main{padding:20px 12px}section{padding:20px}h1{font-size:30px}.cards,.charts{grid-template-columns:1fr}}"""


def esc(value):
    return html.escape(str(value))


def fmt(value, style="amount"):
    if value is None:
        return "—"
    if style == "pct":
        return f"{value:.2%}"
    if style == "plain":
        return f"{value:,.2f}"
    return f"{value / 100000000:,.2f}"


def page(title, body):
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>'


def chart(panel, metric, title, base_year=None, calculated=False, percent=False):
    years = list(range(base_year or 2022, 2026))
    series = []
    for issuer, name, color in COMPANIES:
        values = []
        for year in years:
            row = next(
                (
                    r
                    for r in panel["rows"]
                    if r["issuer"] == issuer and r["period"] == f"{year}-12-31"
                ),
                {},
            )
            values.append(
                row.get("calculated" if calculated else "values", {}).get(metric)
            )
        if base_year:
            base = values[0]
            values = [
                v / base * 100 if v is not None and base and base > 0 else None
                for v in values
            ]
        elif percent:
            values = [v * 100 if v is not None else None for v in values]
        series.append((name, color, values))
    points = [v for _, _, values in series for v in values if v is not None]
    bottom = min(0, min(points)) if points else 0
    top = max(points) * 1.15 if points else 1
    if top <= bottom:
        top = bottom + 1
    x = lambda i: 50 + i * 380 / max(1, len(years) - 1)
    y = lambda value: 230 - (value - bottom) / (top - bottom) * 195
    svg = '<svg viewBox="0 0 460 270" role="img" aria-label="' + esc(title) + '">'
    for i in range(5):
        value = bottom + (top - bottom) * i / 4
        svg += f'<line x1="50" y1="{y(value):.1f}" x2="435" y2="{y(value):.1f}" stroke="#e4e8e4"/><text x="44" y="{y(value) + 4:.1f}" text-anchor="end" font-size="11" fill="#657673">{value:.0f}{"%" if percent else ""}</text>'
    for i, year in enumerate(years):
        svg += f'<text x="{x(i):.1f}" y="252" text-anchor="middle" font-size="12" fill="#657673">{year}</text>'
    for name, color, values in series:
        path = ""
        previous = False
        for i, value in enumerate(values):
            if value is None:
                previous = False
                continue
            path += f"{'L' if previous else 'M'}{x(i):.1f},{y(value):.1f} "
            previous = True
        svg += f'<path d="{path}" stroke="{color}" fill="none" stroke-width="3"/>'
        for i, value in enumerate(values):
            if value is not None:
                svg += f'<circle cx="{x(i):.1f}" cy="{y(value):.1f}" r="4" fill="{color}"><title>{name} {years[i]}：{value:.2f}{"%" if percent else ""}</title></circle>'
    return (
        "<figure><h3>"
        + esc(title)
        + "</h3>"
        + svg
        + "</svg><figcaption>"
        + (
            "2021＝100；五个年度点，四个增长区间。"
            if base_year
            else "逐年同口径计算；缺数断线，鼠标停在点上查看数值。"
        )
        + "</figcaption></figure>"
    )


def render_comparison(root: Path):
    panel = read_json(root / "panel.json")
    import hashlib
    import json

    basis_path = root / "editorial_basis.json"
    digest = hashlib.sha256(
        json.dumps(
            {k: panel[k] for k in ["rows", "cost_anchors", "reviewed_source_facts"]},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    if basis_path.exists() and read_json(basis_path)["sha256"] != digest:
        raise ValueError(
            "editorial_evidence_changed_requires_review_or_new_output_directory"
        )
    if not basis_path.exists():
        basis_path.write_text(
            json.dumps(
                {
                    "sha256": digest,
                    "scope": "2021-2025 three liquor companies and 2026H1; interpretation frozen to these inputs",
                }
            ),
            encoding="utf8",
        )
    lookup = {(r["issuer"], r["period"]): r for r in panel["rows"]}
    docs = {d["document"]: d for d in panel["documents"] if "pack_folder" in d}

    def source_link(doc, page_number, text=None):
        url = (
            Path(docs[doc]["pack_folder"]) / "original.html"
        ).as_uri() + f"#p{page_number}"
        return f'<a href="{esc(url)}">{esc(text or f"{doc} p{page_number}")}</a>'

    def cell(row, key, calculated=False, style="amount"):
        val = row["calculated" if calculated else "values"].get(key)
        anchor = row["issuer"].split(":")[-1] + "-" + row["period"] + "-" + key
        return f'<a href="evidence.html#{anchor}">{fmt(val, style)}</a>'

    legend = (
        '<div class="legend">'
        + "".join(
            f'<span><i class="dot" style="background:{color}"></i>{name}</span>'
            for _, name, color in COMPANIES
        )
        + "</div>"
    )
    body = '<header><div class="eyebrow">FUNDAMENTAL QUANT · 第二阶段 / 历史与同行</div><h1>泸州老窖：利润增长，<br>是否变成了持续的现金回报？</h1><p class="lead">与五粮液、贵州茅台比较。先把长期增长、最近转折、每吨经济性和现金流分开，再判断哪些差异值得继续研究。</p><span class="tag">2021—2025年度</span> <span class="tag">2026H1单列</span> <span class="tag">已有12份财报 · 本次API调用0</span></header>'
    body += '<nav><a href="#answer">先看结论</a><a href="#growth">长期趋势</a><a href="#cash">现金兑现</a><a href="#cost">每吨成本锚</a><a href="#capital">资本效率与BVPS</a><a href="#recent">最新中报</a><a href="#scope">口径与验收</a></nav>'
    body += '<section id="answer"><h2>先给三个结论</h2><div class="cards"><article><strong>利润增长$lz_profit_cagr/年</strong><p>泸州2021—2025归母利润CAGR为$lz_profit_cagr，但CFO同期CAGR为$lz_cfo_cagr。利润趋势与现金趋势并不一致。</p></article><article><strong>2025是明显转折</strong><p>泸州收入同比$lz_revenue_yoy、归母利润$lz_profit_yoy，2026H1又分别下降$lz_h1_revenue_decline、$lz_h1_profit_decline。不能只用五年平均掩盖近期变化。</p></article><article><strong>吨成本要配合产品结构</strong><p>泸州中高档酒2025每吨账面成本约$lz_cost_tonne 万元、平均实现收入$lz_revenue_tonne 万元。高毛利可观察，但不能据此认定提价能力或同质产品成本领先。</p></article></div><p class="warning">本版交付的是有来源、可重算的事实比较和研究解释。部分资料包尚未达到85%完整性线，保留失败状态；没有将这份比较标为正式投资报告，也不恢复付费运行。</p></section>'
    body += (
        '<section id="growth"><h2>1. 长期看增长，短期看是否转向</h2>'
        + legend
        + '<div class="charts">'
        + chart(panel, "revenue", "营业收入趋势", 2021)
        + chart(panel, "net_profit_parent", "归母净利润趋势", 2021)
        + '</div><p>以2021为共同基期，茅台到2025仍保留较大的收入和利润增幅；泸州居中，五粮液在2025明显下行。这只是这段历史的结果，不证明下一阶段的增长顺序。</p><div class="scroll"><table><tr><th>CAGR</th><th>收入CAGR</th><th>归母利润CAGR</th><th>CFO CAGR</th><th>基础FCF CAGR</th></tr>'
    )
    for issuer, name, _ in COMPANIES:
        growth = {g["metric"]: g for g in panel["growth"] if g["issuer"] == issuer}
        body += (
            "<tr><td>"
            + name
            + "</td>"
            + "".join(
                "<td>"
                + fmt(growth.get(k, {}).get("value"), "pct")
                + f"<br><small>{growth.get(k, {}).get('start_year', '—')}—{growth.get(k, {}).get('end_year', '—')} · {growth.get(k, {}).get('intervals', '—')}区间</small></td>"
                for k in ["revenue", "net_profit_parent", "cfo", "fcf_proxy"]
            )
            + "</tr>"
        )
    body += '</table></div><p class="muted">收入、归母利润和CFO为4个增长区间；Capex原始覆盖从2022开始，因此基础FCF为3个区间。茅台、五粮液2021比较数取2023年报重述后栏。没有补造缺失年度。</p><div class="claim"><b>对泸州的含义：</b>历史利润增速高于收入增速，本来支持利润率改善这一观察；但2025和2026H1的收缩已经成为新证据。接下来应检验“短期渠道调整”是否能由销量、毛利和回款共同恢复支持，不能直接外推历史利润增长率。</div></section>'
    body += (
        '<section id="cash"><h2>2. 现金流与利润分开：高转换率也可能来自低利润分母</h2>'
        + legend
        + '<div class="charts">'
        + chart(
            panel,
            "cfo_conversion",
            "合并CFO / 合并净利润",
            calculated=True,
            percent=True,
        )
        + chart(
            panel, "gross_margin", "营业收入口径毛利率", calculated=True, percent=True
        )
        + '</div><div class="scroll"><table><tr><th>2025 / 亿元</th><th>CFO</th><th>现金Capex</th><th>基础FCF</th><th>CFO/合并净利</th></tr>'
    )
    for issuer, name, _ in COMPANIES:
        row = lookup[(issuer, "2025-12-31")]
        body += (
            "<tr><td>"
            + name
            + "</td><td>"
            + cell(row, "cfo")
            + "</td><td>"
            + cell(row, "capex")
            + "</td><td>"
            + cell(row, "fcf_proxy", True)
            + "</td><td>"
            + cell(row, "cfo_conversion", True, "pct")
            + "</td></tr>"
        )
    body += "</table></div><p><b>泸州：</b>2025基础FCF为$lz_fcf 亿元，CFO/合并净利润为$lz_cash_conversion。2024的高现金流不能单独当作常态；2021—2025CFO并未跟随利润增长。需要继续拆营运资本、收款方式和资本开支，而不是把最新或最高一年的FCF当作正常化值。</p><p><b>五粮液：</b>2025现金转换率为$wly_cash_conversion，但当年归母利润下降$wly_profit_decline，高比率同时受分母收缩影响。2026H1利润回升、经营现金流却为负，进一步说明不能把一年CFO/利润高直接解释为质量高。</p><p><b>茅台：</b>2025CFO与2026H1CFO出现大幅反向变化，管理层均解释了财务公司成员存款与同业存款的影响。这些资金流动使原始CFO与酒业务现金创造能力不等价。</p>"
    for item in panel["reviewed_source_facts"].get("management_statements", [])[:4]:
        body += (
            "<details><summary>公司解释："
            + esc(item["topic"])
            + " · "
            + source_link(item["document_id"], item["page"])
            + "</summary><blockquote>"
            + esc(item["quote"])
            + "</blockquote></details>"
        )
    body += '<p class="muted">公式只给基础FCF。没有把财务公司资金、少数股东权益、维持性与增长性Capex自动净额调整成股东自由现金流。转换率统一以合并净利润为分母。</p></section>'
    body += '<section id="cost"><h2>3. 白酒的成本锚：卖出一吨，赚到多少收入，结转多少成本？</h2><p>这里用匹配产品范围的<b>销售收入÷销量</b>与<b>营业成本÷销量</b>。它比单列毛利率更接近业务经济性，但仍是产品组合的平均值，不是出厂标价、终端成交价或新酿基酒的生产成本。</p><div class="scroll"><table><tr><th>2025产品范围</th><th>平均实现收入 万元/吨</th><th>平均账面成本 万元/吨</th><th>分部毛利率</th><th>来源</th></tr>'
    for index, a in enumerate(panel["cost_anchors"]):
        c = a["calculated"]
        f = a["fields"][0]
        body += f'<tr><td>{esc(a["company_name"])} · {esc(a["scope"])}</td><td>{fmt(c["revenue_per_tonne"] / 10000 if c["revenue_per_tonne"] is not None else None, "plain")}</td><td>{fmt(c["cost_per_tonne"] / 10000 if c["cost_per_tonne"] is not None else None, "plain")}</td><td>{fmt(c["segment_gross_margin"], "pct")}</td><td><a href="evidence.html#anchor-{index}">收入 / 成本 / 销量原文</a></td></tr>'
    body += '</table></div><div class="claim"><b>这张表能回答什么：</b>泸州中高档酒和其他酒类的单位收入、成本及毛利差异很大，说明产品组合对公司平均利润率影响显著。<b>不能回答什么：</b>跨品牌每吨成本低不自动等于效率高；陈酿周期、酒体等级、历史入库成本及渠道组合都不同。</div><p>茅台2025直销收入增长、批发代理收入下降，渠道转移也会影响平均收入与毛利。泸州2025中高档销量下降13.23%、对应收入下降16.74%，两者同时下降；收入÷销量的变化只能称为组合平均实现收入变化，不能拆成纯价格因素。</p>'
    for item in panel["reviewed_source_facts"].get("management_statements", [])[4:]:
        body += (
            "<details><summary>公司说明："
            + esc(item["topic"])
            + " · "
            + source_link(item["document_id"], item["page"])
            + "</summary><blockquote>"
            + esc(item["quote"])
            + "</blockquote></details>"
        )
    body += '</section><section id="capital"><h2>4. 资本效率与BVPS：盈利能力和每股净资产分别看</h2><div class="scroll"><table><tr><th>2025</th><th>归母利润 亿元</th><th>归母净资产 亿元</th><th>简单平均权益ROE</th><th>BVPS 元/股</th><th>Capex/收入</th></tr>'
    for issuer, name, _ in COMPANIES:
        row = lookup[(issuer, "2025-12-31")]
        body += (
            "<tr><td>"
            + name
            + "</td>"
            + "".join(
                "<td>" + cell(row, k, calc, style) + "</td>"
                for k, calc, style in [
                    ("net_profit_parent", False, "amount"),
                    ("parent_equity", False, "amount"),
                    ("simple_roe", True, "pct"),
                    ("bvps", True, "plain"),
                    ("capex_intensity", True, "pct"),
                ]
            )
            + "</tr>"
        )
    body += '</table></div><p>本期茅台、泸州、五粮液的简单ROE约为$mt_roe、$lz_roe、$wly_roe；这描述当前利润相对平均归母资本的回报，不能证明高回报能持续。五粮液利润骤降拉低当期ROE；泸州仍高于五粮液，但应与自身下行趋势一起看。</p><p>BVPS使用期末归母净资产除以股份变动表中的真实期末总股数，不用股本金额代替股数，也不用EPS加权股数。它用于后续PB与资本回报比较，单独高低不代表便宜。没有引入未经核对的现价或计算买入信号。</p><p class="muted">简单ROE不是公司披露的加权ROE。ROIC尚缺统一的经营投入资本、税项和金融资产剔除桥接，本版不填一个未经支持的数字。</p></section>'
    body += '<section id="recent"><h2>5. 最新中报：只做同半年度比较</h2><div class="scroll"><table><tr><th>2026H1</th><th>收入同比</th><th>归母利润同比</th><th>CFO 亿元</th><th>基础FCF 亿元</th></tr>'
    for issuer, name, _ in COMPANIES:
        row = lookup[(issuer, "2026-06-30")]
        body += (
            "<tr><td>"
            + name
            + "</td>"
            + "".join(
                "<td>" + cell(row, k, calc, style) + "</td>"
                for k, calc, style in [
                    ("revenue_yoy", True, "pct"),
                    ("net_profit_parent_yoy", True, "pct"),
                    ("cfo", False, "amount"),
                    ("fcf_proxy", True, "amount"),
                ]
            )
            + "</tr>"
        )
    body += "</table></div><p><b>泸州：</b>收入与利润继续同步收缩，暂时不能用“仅为上一年的扰动”解释2025下滑。<b>五粮液：</b>利润回升与现金流转负并存，应把销售恢复与收款政策分别核对。<b>茅台：</b>收入略增、归母利润略降，CFO大幅回升有财务公司资金因素，不能把现金回升全归功于需求。</p><h3>下一轮研究只补会改变判断的问题</h3><ul><li>泸州：中高档销量、组合平均实现收入和毛利是否共同止跌；若利润恢复但回款仍弱，现金兑现假设仍未成立。</li><li>五粮液：承兑票据到期、现金收款比例与动销怎样解释利润和CFO背离；不以单期高现金转换率作结论。</li><li>茅台：剥离财务公司资金影响后的经营现金趋势，以及直销增长能否弥补批代收缩；未取得桥接前保留限制。</li></ul></section>"
    body += '<section id="scope"><h2>6. 数据范围、两个门槛与复核边界</h2><p>目标为泸州老窖，选择同为白酒企业的五粮液和贵州茅台作同行。已有2023、2024、2025年报及2026中报各3份；更早数来自原文比较期，未额外下载、未重新调用DS。</p><div class="scroll"><table><tr><th>资料</th><th>财期</th><th>关键项</th><th>有效填充率</th><th>整包完整性</th></tr>'
    for d in panel["documents"]:
        if "gate" not in d:
            continue
        g = d["gate"]
        key = g["gates"]["critical_fields"]
        fill = g["gates"]["fill_rate"]
        body += f"<tr><td>{source_link(d['document'], 1, d['company'] + ' · ' + d['document'])}</td><td>{d['period']}</td><td>{len(key['required']) - len(key['missing'])}/{len(key['required'])}</td><td>{fill['numerator']}/{fill['denominator']} = {fill['rate']:.2%}</td><td>{'通过' if g['passed'] else '未通过'}</td></tr>"
    body += (
        "</table></div><p>所有资料已补齐关键6项，但部分未达到43项85%填充线。本页允许对已有可靠字段作范围明确的比较，不改变这些整包门槛结果，也不宣称完成投资发布验收。未披露的管理层解释不另设阻塞。</p><p>表内金额保留原表计量，以亿元呈现。多数非茅台表头没有重复列报币种，本版保留该限制、不作换汇；来源保留公司、财期、原值、列号、页码与版本。2023年初权益映射到上年末仅用于时间索引，原始“期初”标签继续保存。不同披露若数值冲突则留空并暴露，不静默覆盖。</p><details><summary>完整计算公式与限制</summary><pre>"
        + esc(str(panel["formulas"]))
        + "</pre><ul>"
        + "".join("<li>" + esc(x) + "</li>" for x in panel["limitations"])
        + '</ul></details><p><a href="panel.json">结构化面板与计算</a> · <a href="evidence.html">逐项原文核对</a> · <a href="review_source_facts.json">2021、成本锚及管理层解释来源</a></p></section><footer>第二阶段事实比较 · 2026-10-10 · 零新增模型/API调用 · 不包含估值、正常化股东现金流或买入推荐</footer>'
    )
    target = lookup[("a_share:000568", "2025-12-31")]["calculated"]
    half = lookup[("a_share:000568", "2026-06-30")]["calculated"]
    wly = lookup[("a_share:000858", "2025-12-31")]["calculated"]
    mt = lookup[("a_share:600519", "2025-12-31")]["calculated"]
    target_growth = {
        g["metric"]: g["value"]
        for g in panel["growth"]
        if g["issuer"] == "a_share:000568"
    }
    anchor = next(
        a["calculated"]
        for a in panel["cost_anchors"]
        if a["issuer_id"] == "a_share:000568" and a["scope"] == "中高档酒类"
    )
    body = Template(body).substitute(
        {
            "lz_profit_cagr": fmt(target_growth.get("net_profit_parent"), "pct"),
            "lz_cfo_cagr": fmt(target_growth.get("cfo"), "pct"),
            "lz_revenue_yoy": fmt(target["revenue_yoy"], "pct"),
            "lz_profit_yoy": fmt(target["net_profit_parent_yoy"], "pct"),
            "lz_h1_revenue_decline": fmt(abs(half["revenue_yoy"]), "pct"),
            "lz_h1_profit_decline": fmt(abs(half["net_profit_parent_yoy"]), "pct"),
            "lz_cost_tonne": fmt(anchor["cost_per_tonne"] / 10000, "plain"),
            "lz_revenue_tonne": fmt(anchor["revenue_per_tonne"] / 10000, "plain"),
            "lz_fcf": fmt(target["fcf_proxy"]),
            "lz_cash_conversion": fmt(target["cfo_conversion"], "pct"),
            "wly_cash_conversion": fmt(wly["cfo_conversion"], "pct"),
            "wly_profit_decline": fmt(abs(wly["net_profit_parent_yoy"]), "pct"),
            "mt_roe": fmt(mt["simple_roe"], "pct"),
            "lz_roe": fmt(target["simple_roe"], "pct"),
            "wly_roe": fmt(wly["simple_roe"], "pct"),
        }
    )
    (root / "index.html").write_text(
        page("泸州老窖与同行：五年趋势与现金兑现", body), encoding="utf8"
    )
    evidence = '<h1>数据与计算原文索引</h1><a href="index.html">返回比较报告</a>'
    for row in panel["rows"]:
        prefix = row["issuer"].split(":")[-1] + "-" + row["period"]
        evidence += (
            "<section><h2>" + esc(row["company"] + " " + row["period"]) + "</h2>"
        )
        for metric, facts in row["refs"].items():
            evidence += f'<h3 id="{prefix}-{metric}">{esc(metric)}：{esc(row["values"].get(metric))}</h3>'
            for f in facts:
                doc = f.get("document") or f["document_id"]
                pages = f.get("pages") or [f["page"]]
                evidence += (
                    "<p>"
                    + " · ".join(source_link(doc, p) for p in pages)
                    + "</p><details><summary>展开原文与期间表头</summary><pre>"
                    + esc(f.get("header", ""))
                    + chr(10)
                    + esc(f["quote"])
                    + "</pre></details>"
                )
        for metric, value in row["calculated"].items():
            evidence += f'<h3 id="{prefix}-{metric}">{esc(metric)}：{esc(value)}</h3><p>以本节列明的同口径输入计算；完整公式见 panel.json。</p>'
        evidence += "</section>"
    for index, a in enumerate(panel["cost_anchors"]):
        evidence += f'<section id="anchor-{index}"><h2>{esc(a["company_name"] + " · " + a["scope"])}</h2>'
        for f in a["fields"]:
            evidence += (
                "<h3>"
                + esc(f["metric"] + ": " + f["raw_value"] + " " + f["unit"])
                + "</h3><p>"
                + source_link(f["document_id"], f["page"])
                + "</p><pre>"
                + esc(f["quote"])
                + "</pre>"
            )
        evidence += "</section>"
    (root / "evidence.html").write_text(page("逐项原文核对", evidence), encoding="utf8")
