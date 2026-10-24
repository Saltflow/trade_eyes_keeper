"""Offline layout arithmetic only: frozen facts plus an isolated fictional unit firm."""

import copy

import yaml

from tests import research_theme_fixtures as themes

YEARS = [f"FY{y}" for y in range(2019, 2026)]
FUTURE = [f"FY{y}E" for y in range(2026, 2031)]
ASSUMPTIONS = {"保守": (0.00, 0.08), "基准": (0.05, 0.10), "乐观": (0.10, 0.12)}


def scenario(growth, margin):
    revenue, equity, rows = 100.0, 100.0, []
    for year in FUTURE:
        previous_revenue, opening_equity = revenue, equity
        revenue *= 1 + growth
        profit, depreciation, capex = revenue * margin, revenue * 0.02, revenue * 0.03
        working_capital = (revenue - previous_revenue) * 0.20
        cfo = profit + depreciation - working_capital
        fcfe = cfo - capex
        equity += profit - fcfe
        rows.append(
            {
                "year": year,
                "revenue": revenue,
                "margin": margin * 100,
                "profit": profit,
                "depreciation": depreciation,
                "capex": capex,
                "working_capital": working_capital,
                "cfo": cfo,
                "fcfe": fcfe,
                "eps": profit / 10,
                "dps": fcfe / 10,
                "opening_equity": opening_equity,
                "equity": equity,
                "roe": profit / ((opening_equity + equity) / 2) * 100,
            }
        )
    return rows


def maximum_price(rows, hurdle, exit_pe=10):
    return sum(r["dps"] / (1 + hurdle) ** (i + 1) for i, r in enumerate(rows)) + (
        rows[-1]["eps"] * exit_pe / (1 + hurdle) ** len(rows)
    )


def solve(function, target, low, high):
    """Bisection confined to positive, monotone fictional acceptance cases."""
    assert (function(low) - target) * (function(high) - target) <= 0
    increasing = function(high) > function(low)
    for _ in range(90):
        midpoint = (low + high) / 2
        if (function(midpoint) < target) == increasing:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2


def seven_year_prose(prose):
    """Expand only the explicit old sample table shape, never model responses."""
    lines = []
    for line in prose.splitlines():
        cells = line.split("|")
        if line.startswith("|") and len(cells) == 7:
            if "FY2023" in line:
                cells[3:3] = [f" {y} " for y in YEARS[:4]]
            elif "---" in line:
                cells[3:3] = ["---:"] * 4
            else:
                cells[3:3] = [" 本轮未取得 "] * 4
            line = "|".join(cells)
        lines.append(line)
    return "\n".join(lines) + "\n\n"


def valuation_section():
    text = """## 七、现价、五年预测与hurdle：条件必须能落到价格

### 真实公司：预测位置保留，缺口明确

以下三家公司只有FY2023–FY2025底稿，FY2019–FY2022本轮未取得；没有同日行情、投资人hurdle和已审查预测假设。不能据此给当前买入回报。形式已经包含七年历史和未来五年，不能将三年历史冒称七年，也不能借下方虚构算例填数。

| 同一输入/输出 | 四川路桥（工程） | 隧道股份（工程） | 泸州老窖（机制参照） |
|---|---|---|---|
| 最近七年历史 | 2019–2025；前四年未取得 | 同左 | 同左 |
| 现价/报价时点/股类/币种/普通股市值 | 本轮未取得 | 本轮未取得 | 本轮未取得 |
| 收入外推依据 | 三年端点变化，不冒称七年趋势；并表边界待定 | 三年收缩趋势，不等于五年必然延续 | 三年波动；不据高ROE直接外推 |
| 利润率/营运资本/必要投入/净融资假设 | 尚未形成研究情景 | 尚未形成研究情景 | 尚未形成研究情景 |
| 普通股利润/权益/股数与ROE路径 | 其他权益权利等边界待定 | 其他权益权利等边界待定 | 需实际普通股口径 |
| FCFE收益率/现价五年IRR | 缺行情和现金预测，条件式待算 | 同左 | 同左 |
| hurdle最高买价/反向增长条件 | 待已说明的经营及终值假设；不默认用户门槛 | 同左 | 同左 |

未来五年表按指标分组、公司相邻。每项列出保守/基准/乐观；同一情景的公司可以横比，不把各公司不同情景混成排名。现有材料尚未支持这些预测，本轮保留位置：

| 指标/单位 | 公司/情景 | FY2026E | FY2027E | FY2028E | FY2029E | FY2030E |
|---|---|---|---|---|---|---|
"""
    for label in [
        "收入/利润率/利润",
        "普通股股数/EPS/权益/ROE",
        "投入/营运资本/净融资",
        "FCFE及每股分配",
    ]:
        for code in themes.ALL:
            text += f"| {label} | {themes.name(code)}／情景待定 | 假设未定 | 假设未定 | 假设未定 | 假设未定 | 假设未定 |\n"
    text += """
### 归一化单位公司：纯虚构形式算例，不属于三家公司

为验证预测→现金→每股→终值→IRR→hurdle展示，单独设置单位公司U。金额为虚构单位，价格为单位/股，股数10、基期收入100、普通股权益100、假定买价10、市值100；均非实际行情。无债务、无少数股东/其他权益，净融资0。保守/基准/乐观增长分别0%/5%/10%，净利率8%/10%/12%；不是历史估计或投资建议。

三情景折旧=收入2%，资本投入=收入3%，非现金营运资本=收入20%，因此增量占款=增量收入20%；期末营运资本及固定资产由这套假设滚动。CFO=利润+折旧−增量营运资本；FCFE=CFO−资本投入+净融资，不再重复扣占款。FCFE全部假定年末分红，股数不变，无回购。普通股权益期末=期初+利润−分红，ROE使用期初期末平均权益。无期初多余现金；期末价值不再叠加已分出的FCFE。

退出采用第五年EPS×10倍PE（主观敏感性假设），不是永续增长估值；不再另外加永续终值。hurdle为8%/10%/12%敏感性，不是用户指定门槛。所有未来值标E；实际公司预测仍未验收。

| U基准预测指标 | FY2026E | FY2027E | FY2028E | FY2029E | FY2030E |
|---|---:|---:|---:|---:|---:|
"""
    cases = {name: scenario(*values) for name, values in ASSUMPTIONS.items()}
    baseline = cases["基准"]
    for label, key in [
        ("收入", "revenue"),
        ("净利率（%）", "margin"),
        ("普通股利润", "profit"),
        ("折旧", "depreciation"),
        ("增量营运资本", "working_capital"),
        ("CFO", "cfo"),
        ("资本投入", "capex"),
        ("FCFE/分红", "fcfe"),
        ("EPS", "eps"),
        ("DPS", "dps"),
        ("期末普通股权益", "equity"),
        ("平均权益ROE（%）", "roe"),
    ]:
        text += f"| {label} | " + " | ".join(f"{r[key]:.4f}" for r in baseline) + " |\n"
    text += "\n[[FORECAST_FIGURE]]\n\nROE是资本回报，不能当买入回报；FCFE收益率=第一年FCFE/假定市值100。五年IRR来自年末DPS及第五年卖价。基准未来第一年现金收益率8.45%，不是ROE，也不是五年IRR。\n\n"
    text += "| U情景 | 首年FCFE收益率 | 五年累计DPS | 第五年卖价（10倍EPS） | 买价10的五年IRR | 8%最高买价 | 10%最高买价 | 12%最高买价 |\n|---|---:|---:|---:|---:|---:|---:|---:|\n"
    for name, rows in cases.items():
        irr = solve(lambda rate, rows=rows: maximum_price(rows, rate), 10, -0.5, 0.8)
        text += f"| {name} | {rows[0]['fcfe']:.2f}% | {sum(r['dps'] for r in rows):.4f} | {rows[-1]['eps'] * 10:.4f} | {irr * 100:.2f}% | "
        text += (
            " | ".join(
                f"{maximum_price(rows, rate):.4f}" for rate in [0.08, 0.10, 0.12]
            )
            + " |\n"
        )
    text += "\n[[HURDLE_FIGURE]]\n\n"
    hurdle, exit_pe = 0.10, 10
    limit = maximum_price(baseline, hurdle)
    required_growth = solve(
        lambda g: maximum_price(scenario(g, 0.10), hurdle), 10, -0.05, 0.15
    )
    terminal_pv = baseline[-1]["eps"] * exit_pe / 1.1**5
    text += f"\n公式：最高买价(h)=Σ[DPS_t/(1+h)^t]+第五年卖价/(1+h)^5；IRR令这个现值等于所示买价。基准10%门槛对应最高价{limit:.4f}，假定买价10的折价=1−10/{limit:.4f}≈{(1 - 10 / limit) * 100:.2f}%。基准价值中终值折现贡献约{terminal_pv / limit * 100:.2f}%，需重点审查退出假设。\n\n"
    text += f"反向条件：在利润率10%、相同投入/占款、全分配、退出PE10倍和买价10固定时，要达到10%五年回报，未来五年收入年增速至少约{required_growth * 100:.2f}%。这只是同一套虚构条件下的求解，不是三家公司的要求增速。\n\n"
    text += (
        "| U基准10%门槛的退出假设敏感性 | 退出PE8倍 | 退出PE10倍 | 退出PE12倍 |\n|---|---:|---:|---:|\n| 最高买价 | "
        + " | ".join(f"{maximum_price(baseline, 0.10, pe):.4f}" for pe in [8, 10, 12])
        + " |\n\n"
    )
    text += "## 八、反证与重看\n\n对工程两家，应检验订单转收入、项目利润率、占款释放、正常投入与融资约束；对白酒参照，应检验库存/预收与分红现金。预测收入增加但营运资本消耗更快、利润率回落或退出估值下降，都可推翻回报结论。真实公司缺价格/权利/假设时继续给条件表达，不给当前便宜或达标排名。形式样例不能证明真实模型会作出这些判断。\n\n"
    text += "## 附录：七年窗口与验收边界\n\nFY2019–FY2022四年本轮未取得，FY2023–FY2025使用冻结旧值；缺口不是零或原文未披露。指数仍用各公司真实可得的共同FY2023=100，而非编造FY2019基数。历史七年有六个增长间隔，当前三年端点仅两个。旧版本/并表/普通股权利限制继续保留。U算例完全虚构并隔离。形式验收覆盖结构、算术、图表和三轮交接，不覆盖七年数据完整性、预测合理性、行情时效或真实模型研究效果。\n"
    return text


def chart_specs():
    specs = copy.deepcopy(themes.chart_specs())
    for spec in specs:
        spec["x"] = YEARS
        spec["period_basis"] = (
            "FY2019–FY2025七年窗口；FY2019–FY2022未取得；" + spec["period_basis"]
        )
        for series in spec["series"]:
            series["values"] = [None] * 4 + series["values"]
    cases = {name: scenario(*values) for name, values in ASSUMPTIONS.items()}
    for identity, title, unit, x, getter in [
        (
            "unit_forecast",
            "虚构U：未来五年收入情景，非实际公司预测",
            "虚构单位",
            FUTURE,
            lambda rows: [r["revenue"] for r in rows],
        ),
        (
            "unit_hurdle",
            "虚构U：同一退出假设下hurdle与最高买价",
            "虚构单位/股",
            ["8%", "10%", "12%"],
            lambda rows: [maximum_price(rows, h) for h in [0.08, 0.10, 0.12]],
        ),
    ]:
        specs.append(
            {
                "id": identity,
                "title": title,
                "unit": unit,
                "scope": "归一化虚构U，非三家公司",
                "period_basis": "FY2026E–FY2030E；年末现金流，退出PE10倍",
                "version": "纯形式模拟",
                "source": "本地明确假设，不是行情或财报",
                "type": "line",
                "x": x,
                "series": [
                    {"name": name, "values": getter(rows)}
                    for name, rows in cases.items()
                ],
                "reading": "全部是形式验收假设，不能作为三家公司预测或现价依据。",
            }
        )
    return specs


def bodies():
    specs = chart_specs()
    _, parts = themes.sections()
    valuation = valuation_section()
    forecast, rest = valuation.split("[[FORECAST_FIGURE]]")
    hurdle, closing = rest.split("[[HURDLE_FIGURE]]")
    parts = [(seven_year_prose(prose), figures) for prose, figures in parts[:-1]] + [
        (forecast, [12]),
        (hurdle, [13]),
        (closing, []),
    ]
    responses = []
    for stage in ["draft", "review", "write"]:
        body = "# 七年历史如何支持现价回报：工程横比与白酒机制参照\n\n> 形式验收模拟全文，不是模型研究。历史窗口FY2019–FY2025，当前仅后三年有值；未来五年FY2026E–FY2030E。真实行情未取得；虚构U仅验证版式与算术。\n\n"
        for prose, figures in parts:
            body += prose
            for i in figures:
                spec = copy.deepcopy(specs[i])
                reading = spec.pop("reading")
                if stage == "draft" and i == 5:
                    spec["series"][0]["values"][-1] = (
                        72.23 / themes.COMPANIES["600039"]["revenue"][-1] * 100
                    )
                body += f"### 图{i + 1}：{spec['title']}\n\n"
                body += (
                    f"![{spec['title']}](assets/review_chart_{i + 1}.png)\n\n"
                    if stage == "write"
                    else "```chart\n"
                    + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
                    + "```\n\n"
                )
                body += (
                    f"口径：{spec['period_basis']}；单位：{spec['unit']}。{reading}\n\n"
                )
        if stage != "write":
            body += "\n模拟审查记录：" + (
                "图6故意误用72.23，用于测试交接。"
                if stage == "draft"
                else "图6改回77.23；七年空列、U现金桥接/ROE/IRR和14图交接作为离线检查。"
            )
        responses.append(body)
    return responses
