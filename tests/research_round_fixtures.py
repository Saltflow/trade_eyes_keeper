"""Explicitly simulated drafts using frozen figures; no model or network access."""

import json
from pathlib import Path

import yaml

FIXTURE = Path(__file__).parent / "fixtures/research_rounds/companies.json"


def bodies():
    data = json.loads(FIXTURE.read_bytes())
    labels = {
        "revenue": "营业收入",
        "profit": "归母利润",
        "cfo": "经营现金",
        "capex": "完整现金Capex",
        "cash": "货币资金",
        "restricted": "受限货币资金",
        "equity": "归母权益",
        "eps": "EPS（元/股）",
        "roe": "ROE（%）",
        "dividend": "年度分红口径",
        "receivables": "应收账款",
        "contract_assets": "合同资产",
        "inventory": "存货",
        "contract_liabilities": "合同负债",
    }
    body = "# 离线模拟：用规模、历史和现金解释公司\n\n> 人工构造的接口样例，非新的模型研究结论，未独立核验财报。没有行情快照，市值、股价和股息率均未取得。\n\n"
    body += "共同口径：除每股和百分比外为CNY亿元；合并；FY2023–FY2025。现金、占款与权益是期末存量，其余按各项目含义；2025分红含预案，不代表当年实际支付。隧道2023保留原报口径，不与后报拼接；四川2024旧记录身份未定。\n\n"
    for c in data["companies"]:
        body += f"## {c['name']}：公司尺度与三年变化\n\n| 项目 | FY2023 | FY2024 | FY2025 |\n|---|---:|---:|---:|\n"
        for key, label in labels.items():
            if key in c:
                body += (
                    "| " + label + " | " + " | ".join(str(v) for v in c[key]) + " |\n"
                )
        n, d = c["restricted"][-1], c["cash"][-1]
        body += f"\n受限货币资金{n:.2f}亿元 / 货币资金{d:.2f}亿元 = {n / d * 100:.2f}%〔样例算术〕；金额必须连同资金基数理解，不能据比例单项判断现金安全。\n\n"
        n, p, cash = c["dividend"][-1], c["profit"][-1], c["cfo"][-1]
        body += f"年度分红口径{n:.2f}亿元，占归母利润{p:.2f}亿元的{n / p * 100:.2f}%，相当于CFO {cash:.2f}亿元的{n / cash * 100:.2f}%〔样例算术〕。这是方案与年度能力的比较，不能当已付现金覆盖率。\n\n"
        if "receivables" in c:
            occupied = c["receivables"][-1] + c["contract_assets"][-1]
            body += f"应收账款与合同资产合计{occupied:.2f}亿元 / 年收入{c['revenue'][-1]:.2f}亿元 = {occupied / c['revenue'][-1] * 100:.2f}%〔样例算术〕，仅说明占款量级，不是回款天数；须结合收入变化、账龄和回款继续解释。\n\n"
        else:
            body += "存货116.22→133.93→153.96亿元，合同负债26.73→39.78→33.67亿元；产品结构和陈酿性质未拆分，不能把全部库存当积压。\n\n"
    charts = []
    for c in data["companies"]:
        topics = [
            ("growth", "收入与利润", "亿元", ["revenue", "profit"]),
            ("returns", "ROE历史变化", "%", ["roe"]),
            ("cash", "现金、投入与年度分红口径", "亿元", ["cfo", "capex", "dividend"]),
            (
                "occupation",
                "经营占款",
                "亿元",
                ["receivables", "contract_assets"]
                if "receivables" in c
                else ["inventory", "contract_liabilities"],
            ),
        ]
        for tag, title, unit, keys in topics:
            charts.append(
                {
                    "id": c["code"] + "_" + tag,
                    "type": "line" if tag != "cash" else "bar",
                    "title": c["name"] + "：" + title,
                    "unit": unit,
                    "scope": c["name"] + "；合并；各公司自身历史，不作跨行业优劣排名",
                    "period_basis": "FY2023–FY2025；占款为期末存量，分红为所属年度方案，其余按指标含义",
                    "version": "既有底稿列示版本；未核验PDF；隧道2023原报",
                    "source": data["source"],
                    "x": data["years"],
                    "series": [{"name": labels[k], "values": list(c[k])} for k in keys],
                }
            )
    interpretation = "\n## 图表与判断\n\n增长图回答收入恢复是否伴随利润恢复；ROE图显示资本回报方向；现金图分别呈现CFO、完整Capex与年度分红方案，不能堆成实际现金流恒等式；占款图说明投入与收入回款的关系。图不替代原值表和行业解释。\n\n## 条件结论与反证\n\n工程两家公司仅有条件可比，白酒为独立参照。需要观察现金改善是否依赖支付递延、资本开支变化是否来自并表变化、库存与预收是否说明不同的经营方向。缺少价格和估值参数，保留价值驱动与条件，不计算安全边际。下一期同口径报告、回款及分红实施是重看触发；不是新的投资建议。\n"
    rendered, drafts = [], []
    for stage in ["draft", "review"]:
        text = body
        for i, item in enumerate(charts, 1):
            value = json.loads(json.dumps(item))
            if stage == "draft" and value["id"] == "600039_cash":
                value["series"][0]["values"][-1] = 72.23
            text += (
                f"### 图{i}：{value['title']}\n\n```chart\n"
                + yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
                + "```\n\n"
            )
        text += interpretation
        text += "\n## 离线模拟审查记录\n\n" + (
            "故意在四川CFO图中放入72.23，仅验证后续修订路径；原表仍为77.23。"
            if stage == "draft"
            else "样例审查将四川FY2025图中CFO从故意错误72.23改回77.23；分母、三年历史与三家公司均保留。真实模型是否能发现错误尚未验证。"
        )
        drafts.append(text)
    for i, item in enumerate(charts, 1):
        rendered.append(
            f"### 图{i}：{item['title']}\n\n![{item['title']}](assets/review_chart_{i}.png)\n\n单位：{item['unit']}；{item['period_basis']}。范围及版本沿用上表；图用于解释本公司历史。\n"
        )
    final = (
        body
        + "\n".join(rendered)
        + interpretation
        + "\n> 本页仅验证三轮交接：最终引用审查后图片；不是新模型的正式研究成果。\n"
    )
    return [*drafts, final]
