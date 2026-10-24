"""Theme-first simulated responses using frozen facts; no research/API execution."""

import json

import yaml

from tests.research_round_fixtures import FIXTURE

DATA = json.loads(FIXTURE.read_bytes())
COMPANIES = {c["code"]: c for c in DATA["companies"]}
ENGINEERING = ["600039", "600820"]
ALL = [*ENGINEERING, "000568"]


def name(code):
    return COMPANIES[code]["name"] + ("（机制参照）" if code == "000568" else "")


def ratios(values, denominators, positive=False):
    return [
        None if d == 0 or (positive and d < 0) else n / d * 100
        for n, d in zip(values, denominators)
    ]


def numbers(values):
    return " | ".join(
        "无覆盖意义：CFO为负" if v is None else f"{v:,.2f}" for v in values
    )


def history(items, codes=ALL):
    text = "| 同一指标/单位 | 公司及角色 | FY2023 | FY2024 | FY2025 |\n|---|---|---:|---:|---:|\n"
    for label, getter in items:
        for code in codes:
            text += f"| {label} | {name(code)} | {numbers(getter(COMPANIES[code]))} |\n"
    return text + "\n"


def chart_specs():
    specs = []

    def add(identity, title, unit, definition, codes, getter, reading):
        specs.append(
            {
                "id": identity,
                "title": title,
                "unit": unit,
                "scope": "工程两家深入横比；"
                + (
                    "泸州老窖仅作机制参照"
                    if "000568" in codes
                    else "四川路桥与隧道股份"
                ),
                "period_basis": "FY2023–FY2025，同一时间轴；" + definition,
                "version": "既有底稿各年列示版本；隧道2023原报，未统一重述；四川2024旧身份未定",
                "source": DATA["source"],
                "type": "line",
                "x": DATA["years"],
                "series": [
                    {"name": name(code), "values": getter(COMPANIES[code])}
                    for code in codes
                ],
                "reading": reading,
            }
        )

    add(
        "engineering_revenue",
        "工程收入原值：同轴看规模与方向",
        "亿元",
        "全年合并营业收入；无标准化",
        ENGINEERING,
        lambda c: c["revenue"],
        "四川规模约为隧道两倍，但规模不代表回报；两者收入方向不同。",
    )
    add(
        "engineering_profit",
        "工程归母利润原值：恢复与收缩",
        "亿元",
        "全年归母净利润；无标准化",
        ENGINEERING,
        lambda c: c["profit"],
        "四川利润未随收入恢复至早期水平，隧道利润持续下降；需解释利润构成。",
    )
    add(
        "revenue_index",
        "收入变化：各公司自身FY2023=100",
        "指数",
        "同一基准年；当年收入/本公司FY2023收入×100",
        ALL,
        lambda c: ratios(c["revenue"], [c["revenue"][0]] * 3),
        "指数比较各自增长方向，不把白酒收入规模当工程同行。",
    )
    add(
        "profit_index",
        "利润变化：各公司自身FY2023=100",
        "指数",
        "当年归母利润/本公司FY2023归母利润×100",
        ALL,
        lambda c: ratios(c["profit"], [c["profit"][0]] * 3),
        "三个样本利润都低于各自基期；共同现象尚不能证明同一行业原因。",
    )
    add(
        "roe",
        "ROE同轴观察：资本回报的不同机制",
        "%",
        "全年加权ROE披露值；不混期末净资产粗算值",
        ALL,
        lambda c: c["roe"],
        "白酒高ROE作为资本机制参照；杠杆、业务组合、永续权利影响工程比较。",
    )
    add(
        "cash_intensity",
        "CFO强度：统一用同期合并营业收入作分母",
        "%",
        "全年合并CFO/同年度合并营业收入×100；不是CFO/利润",
        ALL,
        lambda c: ratios(c["cfo"], c["revenue"]),
        "四川由负转正，隧道2024高后回落，白酒波动更大；须解释收付、票据与预收。",
    )
    add(
        "capex_intensity",
        "工程投入强度：完整现金Capex/收入",
        "%",
        "全年购建长期资产支付现金/同年度营业收入×100",
        ENGINEERING,
        lambda c: ratios(c["capex"], c["revenue"]),
        "四川2025骤降有出表背景，不能直接外推正常维护投入；隧道另有投资支付。",
    )
    add(
        "engineering_occupation",
        "工程占款量级：同分母看应收与合同资产",
        "%",
        "(年末应收账款+合同资产)/当年全年营业收入×100；不是回款天数",
        ENGINEERING,
        lambda c: ratios(
            [a + b for a, b in zip(c["receivables"], c["contract_assets"])],
            c["revenue"],
        ),
        "四川与隧道占款相对收入均上升；分子增长与收入下降都能使比例升高，需分别解释。",
    )
    add(
        "restricted_cash",
        "受限货币资金占比：相同资金分母",
        "%",
        "年末受限货币资金/同期货币资金×100；不是受限资产占比",
        ALL,
        lambda c: ratios(c["restricted"], c["cash"]),
        "2025对应四川3.97%、隧道5.16%、泸州1.52%；应结合资金基数、债务和受限条件，不能据单比率排名安全性。",
    )
    add(
        "payout_profit",
        "年度分红口径/同年度归母利润",
        "%",
        "所属年度分红方案/同年度归母净利润×100；2025含预案，不等于已付现金",
        ALL,
        lambda c: ratios(c["dividend"], c["profit"]),
        "盈利分配比例与实际现金能力要分别看，四川2025回购1.06亿元另列。",
    )
    add(
        "payout_cfo",
        "年度分红口径/CFO：现金量级参照",
        "%",
        "所属年度分红方案/全年CFO×100；负CFO年份留空并解释，非实际支付覆盖率",
        ALL,
        lambda c: ratios(c["dividend"], c["cfo"], positive=True),
        "四川2023无正经营现金覆盖，白酒2025方案超过CFO；不能从年度预案推出已付现金。",
    )
    wine = COMPANIES["000568"]
    specs.append(
        {
            "id": "wine_mechanism",
            "title": "白酒机制参照：库存与预收相对收入",
            "unit": "%",
            "scope": "泸州老窖；白酒机制参照，不套工程合同资产指标",
            "period_basis": "FY2023–FY2025；年末存货或合同负债/同年全年合并收入×100；仅量级参照",
            "version": "既有底稿列示版本；2025资料层级限制保留",
            "source": DATA["source"],
            "type": "line",
            "x": DATA["years"],
            "series": [
                {
                    "name": "存货/收入",
                    "values": ratios(wine["inventory"], wine["revenue"]),
                },
                {
                    "name": "合同负债/收入",
                    "values": ratios(wine["contract_liabilities"], wine["revenue"]),
                },
            ],
            "reading": "存货上升叠加收入下降推高库存量级；合同负债绝对额2025下降，但其收入占比不一定下降，不能混淆。",
        }
    )
    return [*specs[:8], specs[11], *specs[8:11]]


def sections():
    def ratio(getter, denominator, **kwargs):
        return lambda c: ratios(getter(c), c[denominator], **kwargs)

    intro = "# 回款能否支持普通股价值：工程横比与白酒机制参照\n\n> 完整离线排版样例：三轮响应由人工构造，不是新的模型研究结论。数据取自冻结底稿，未复核原PDF；市场价格、市值和要求回报未提供。\n\n"
    conclusion = "## 一、横向结论：相近的现金问题，来源并不相同\n\n四川路桥2025收入已恢复到2023附近，归母利润和ROE没有同步恢复；隧道股份收入与利润持续收缩，占款相对收入变重。工程两家共同要回答的是：账面业务能否通过结算和回款、在必要再投入后形成普通股可获得现金。\n\n泸州老窖提供另一种机制参照：高毛利和先款后货并不消除现金波动，库存、票据、预收与分红之间仍有约束。三家公司可以横向展示规模、增长与现金事实，但不据此给投资总分或便宜/昂贵排名。\n\n"
    overview = "## 二、共同尺度：先知道这些金额有多大\n\nFY2025合并口径；金额亿元，ROE为披露加权值。货币资金和权益为年末存量。公司顺序统一为四川路桥、隧道股份、白酒机制参照。\n\n| 同一指标 | 四川路桥（工程） | 隧道股份（工程） | 泸州老窖（机制参照） |\n|---|---:|---:|---:|\n"
    for label, key in [
        ("营业收入", "revenue"),
        ("归母利润", "profit"),
        ("CFO", "cfo"),
        ("完整现金Capex", "capex"),
        ("货币资金", "cash"),
        ("受限货币资金", "restricted"),
        ("归母权益", "equity"),
        ("EPS（元/股）", "eps"),
        ("ROE（%）", "roe"),
    ]:
        overview += (
            "| "
            + label
            + " | "
            + numbers([COMPANIES[c][key][-1] for c in ALL])
            + " |\n"
        )
    overview += "| 同日总市值/价格 | 未提供 | 未提供 | 未提供 |\n\n四川收入约为隧道两倍、白酒收入约为四川的22%；这些尺度帮助理解投入与占款，但不能当效率排名。归母权益不是市值，货币资金也不是可立即分给普通股的现金。未取得同日行情就不计算股息率或当前估值倍数。\n\n"
    growth = "## 三、增长与回报：收入修复是否带来每股价值修复\n\n四川营业收入1,150.42→1,072.38→1,151.11亿元，归母利润90.04→72.10→72.97亿元；隧道收入741.93→688.16→579.27亿元，归母利润29.39→28.41→22.14亿元。两家的区别是收入恢复与持续收缩，而不是都用“增长承压”概括。四川还要解释收入恢复为何没有修复利润；隧道需要同时解释订单转化、施工毛利和投资收益。\n\n"
    growth += history(
        [
            (label, lambda c, k=key: c[k])
            for label, key in [
                ("营业收入（亿元）", "revenue"),
                ("归母利润（亿元）", "profit"),
                ("EPS（元/股）", "eps"),
                ("ROE（%）", "roe"),
            ]
        ]
    )
    growth += "收入与利润指数都以各公司自身FY2023为100，分开成图；绝对额用于规模，指数用于变化。隧道2023为原报序列，后报比较值差异未在这里强行改写；四川2024旧记录身份和并表边界也保留。图可帮助观察方向，不证明已完成同口径归因。\n\n白酒利润和EPS在2025回落，ROE也下降；它帮助说明资本回报与经营现金并非同一概念，不能用其高ROE替工程两家确定估值。\n\n"
    cash = "## 四、利润与现金：先统一分母，再解释现金来源\n\n本段统一展示CFO/同年度合并营业收入，称为现金强度，不称利润现金转换率。归母利润与合并CFO不是同一归属范围，不混换分母画成看似可比的CFO/利润图。原额和分母保留在表里。\n\n"
    cash += history(
        [
            ("CFO（亿元）", lambda c: c["cfo"]),
            ("营业收入/分母（亿元）", lambda c: c["revenue"]),
            ("CFO/收入（%）", ratio(lambda c: c["cfo"], "revenue")),
            ("完整现金Capex（亿元）", lambda c: c["capex"]),
        ]
    )
    cash += "四川从负现金转正，隧道2024改善后于2025回落。前者仍需解释应付增长和对外支付节奏，后者需解释收现与投资回收；同为工程业务也不是同一种现金改善。四川2025低Capex有矿业/清洁能源出表背景，不应当作正常维护投入直接外推；隧道还有投资支付，不能只看购建长期资产一项。\n\n白酒2024高现金流在2025回落，票据、预收和库存变化是机制线索。它的较高CFO/收入不能直接解释为工程回款更差；商业模式和业务周期不同。若未来不依赖支付递延仍能改善收现，才会强化工程现金改善的判断。\n\n"
    occupation = "## 五、占款机制：工程直接比较，白酒另作机制参照\n\n工程两家统一用(年末应收账款+合同资产)/同年全年营业收入。分子是余额，分母是流量，比例仅反映占款量级，不是回款天数。\n\n"
    occupation += history(
        [
            ("应收账款（亿元）", lambda c: c["receivables"]),
            ("合同资产（亿元）", lambda c: c["contract_assets"]),
            ("营业收入/分母（亿元）", lambda c: c["revenue"]),
            (
                "应收+合同资产/收入（%）",
                ratio(
                    lambda c: [
                        a + b for a, b in zip(c["receivables"], c["contract_assets"])
                    ],
                    "revenue",
                ),
            ),
        ],
        ENGINEERING,
    )
    occupation += "2025四川占款合计1,275.91亿元，对照收入1,151.11亿元为110.84%；隧道合计745.35亿元，对照收入579.27亿元为128.67%。四川占款增长显著，隧道还叠加收入缩小，两种因素都能推高比值。不能仅据128.67%更高就断言损失更大，还需账龄、结算条件和项目回款证据。\n\n白酒的对应问题是陈酿/成品库存与预收：存货116.22→133.93→153.96亿元，合同负债26.73→39.78→33.67亿元，收入302.33→311.96→257.31亿元。2025合同负债绝对额下降，但因收入降得更多，其收入占比从约12.75%升至13.08%；这正说明绝对额与分母必须同时看。不能把陈酿库存等同工程合同资产，也不能把全部存货当积压。\n\n"
    allocation = "## 六、资本配置与普通股：金额要放回资金和利润基数\n\n先看受限货币资金占货币资金，再看年度分红相对利润与CFO，两种分母各自单独成图。受限资产总额不混入受限现金占比；年度分红含预案，不是实际现金支付。\n\n"
    allocation += history(
        [
            ("受限货币资金（亿元）", lambda c: c["restricted"]),
            ("货币资金/分母（亿元）", lambda c: c["cash"]),
            ("受限资金/货币资金（%）", ratio(lambda c: c["restricted"], "cash")),
            ("年度分红口径（亿元）", lambda c: c["dividend"]),
            ("分红/归母利润（%）", ratio(lambda c: c["dividend"], "profit")),
            ("分红/CFO（%）", ratio(lambda c: c["dividend"], "cfo", positive=True)),
        ]
    )
    allocation += "2025白酒受限约4.16亿元，占货币资金273.42亿元约1.52%；四川10.41/262.33约3.97%，隧道13.97/270.67约5.16%。这说明受限部分的量级，不代表其余资金均可分配，还要看债务、项目资本和释放条件。\n\n白酒85亿元年度方案约占归母利润108.31亿元的78.48%，却相当于CFO 71.23亿元的119.33%；盈利分配与经营现金覆盖给出不同信息。四川2025现金分红42.73亿元、回购1.06亿元另列，合计约43.79亿元占归母约60.01%；隧道年度分红10.38→10.06→6.29亿元，不能只从绝对额下降推断投资回报更低。四川2023 CFO为负，分红/CFO不报正常覆盖率，原负数仍在前表。\n\n普通股权利还受其他权益工具影响：四川归母权益528.17亿元包含已知永续12亿元，隧道409.53亿元包含90亿元，后者约占21.98%。两家不能把归母权益全当普通股权益；白酒未取得相应余额不填0。长期借款分项或未齐的债务口径不冒充总债务，不在本例拼造企业价值。偿债是现金用途，不能写成支持分红的现金来源。\n\n"
    close = "## 七、估值条件、反证和重看\n\n工程两家的价值驱动应围绕利润质量、项目占款、持续投入与普通股权利展开：四川要验证收入恢复能否改善利润和回款，并且现金改善不依赖支付递延；隧道要验证收入与投资收益变化、占款和融资约束是否缓解。白酒作为参照，说明利润率与先款后货仍不能替代分红现金覆盖分析。\n\n| 当前比较判断 | 反证或竞争解释 | 重看所需信息 |\n|---|---|---|\n| 工程现金改善可能有结算/支付时点因素 | 不增加应付款、持续正常投入时，收现仍改善 | 同期CFO补充资料、应收合同资产和应付变化 |\n| 工程占款/收入上升意味着资本占用更重 | 项目结算释放、收入恢复或可核实回购改善回收 | 账龄、项目结算、本金利息拆分；不能只看比率 |\n| 白酒分红方案超过当年CFO，现金承压 | 收现恢复且必要投入后仍支持分配 | 票据、预收、库存结构、实际分红实施 |\n| ROE和EPS变化代表股东回报方向 | 杠杆、永续债、重述或股本变化解释表观差异 | 普通股权益、加权股数、版本和分派条款 |\n\n本例未提供市场价格、市值或约定的估值参数，不计算当前收益率、价值区间或安全边际。缺价格不影响上面的规模与经营机制比较；下一步若提供同一时点市场快照和明确假设，再将这些条件与市场支付价格对应。\n\n## 附录：口径与缺口\n\n原数来自冻结同行底稿历史面板，未回查PDF。隧道2023原报/后报差异、四川2024身份与并表边界、白酒2025材料层级等继续保留，不擅自更改旧记录。所有比值和指数均为这些已投递数的演示算术，不代表完成统一重述。受限现金使用原元数换算，其他亿元表值有四舍五入。未投递、未取得、不适用和版本冲突各自保留，不统称未披露。\n"
    return intro, [
        (conclusion, []),
        (overview, []),
        (growth, list(range(5))),
        (cash, [5, 6]),
        (occupation, [7, 8]),
        (allocation, [9, 10, 11]),
        (close, []),
    ]


def bodies():
    specs = chart_specs()
    intro, parts = sections()
    replies = []
    for stage in ["draft", "review", "write"]:
        body = intro
        for prose, figures in parts:
            body += prose
            for i in figures:
                spec = json.loads(json.dumps(specs[i]))
                reading = spec.pop("reading")
                if stage == "draft" and spec["id"] == "cash_intensity":
                    spec["series"][0]["values"][-1] = (
                        72.23 / COMPANIES["600039"]["revenue"][-1] * 100
                    )
                body += f"### 图{i + 1}：{spec['title']}\n\n"
                if stage == "write":
                    body += f"![{spec['title']}](assets/review_chart_{i + 1}.png)\n\n"
                else:
                    body += (
                        "```chart\n"
                        + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
                        + "```\n\n"
                    )
                body += (
                    f"口径：{spec['period_basis']}；单位：{spec['unit']}。{reading}\n\n"
                )
        if stage != "write":
            body += "\n### 离线模拟审查记录\n\n" + (
                "故意在图6把四川FY2025 CFO分子77.23误放为72.23，仅用于验证修订PNG交接。"
                if stage == "draft"
                else "模拟审查将图6分子改回77.23，同期收入分母1151.11不变；其他图与主题保留。此处没有真实模型审稿。"
            )
        replies.append(body)
    return replies
