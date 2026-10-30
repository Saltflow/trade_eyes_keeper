"""Versioned research input contract; no investment judgments or estimates."""

PARSER_VERSION = "fr-pack/14"
STATUSES = {
    "obtained",
    "not_disclosed",
    "not_applicable",
    "not_located",
    "parse_failed",
    "conflict",
}
# key | Chinese name | statement | row pattern. No company-specific rules.
_FIELD_ROWS = r"""
revenue|营业收入|income|(?:其中[:：])?营业收入
cost|营业成本|income|(?:其中[:：])?营业成本
operating_profit|营业利润|income|营业利润
net_profit_parent|归母净利润|income|归属于母公司(?:股东|所有者)的净利润
cfo|经营活动现金流净额|cashflow|经营活动产生的现金流量净额
capex|购建长期资产支付现金|cashflow|购建固定资产、无形资产和其他长期资产支付的现金
asset_disposal_cash|处置长期资产现金净额|cashflow|处置固定资产、无形资产和其他长期资产收回的现金净额
investment_cash_in|收回投资现金|cashflow|收回投资收到的现金
investment_income_cash|投资收益收到现金|cashflow|取得投资收益收到的现金
investment_cash_out|投资支付现金|cashflow|投资支付的现金
acquisition_cash|收购支付现金净额|cashflow|取得子公司及其他营业单位支付的现金净额
cfi|投资活动现金流净额|cashflow|投资活动产生的现金流量净额
cff|筹资活动现金流净额|cashflow|筹资活动产生的现金流量净额
borrowing_cash|借款收到现金|cashflow|取得借款收到的现金
debt_repayment_cash|偿债支付现金|cashflow|偿还债务支付的现金
distribution_interest_cash|分红、利润或偿息支付现金合计|cashflow|分配股利、利润或偿付利息支付的现金
tax_cash|支付各项税费（非仅所得税）|cashflow|支付的各项税费
investment_income|投资收益|income|投资收益
fair_value_change|公允价值变动收益|income|公允价值变动收益
credit_impairment|信用减值损失|income|信用减值损失
asset_impairment|资产减值损失|income|资产减值损失
finance_cost|财务费用|income|财务费用
interest_expense|利息费用（非支付现金）|income|(?:其中[:：])?利息费用
interest_income|利息收入|income|利息收入
income_tax|所得税费用|income|(?:减[:：])?所得税费用
inventory|存货|balance|存货
ppe|固定资产|balance|固定资产
construction|在建工程|balance|在建工程
intangibles|无形资产|balance|无形资产
goodwill|商誉（单列）|balance|商誉
receivables|应收账款|balance|应收账款
payables|应付账款|balance|应付账款
contract_assets|合同资产|balance|合同资产
contract_liabilities|合同负债|balance|合同负债
short_debt|短期借款|balance|短期借款
long_debt|长期借款|balance|长期借款
bonds|应付债券|balance|应付债券
lease_liabilities|租赁负债|balance|租赁负债
current_long_debt|一年内到期的非流动负债|balance|一年内到期的非流动负债
parent_equity|归母净资产|balance|归属于母公司(?:所有者|股东)权益(?:[（(]或股东权益[）)])?合计
other_equity|其他权益工具|balance|其他权益工具$
treasury_stock|库存股账面金额（非股数）|balance|(?:减[:：])?库存股
share_capital|股本金额（非股数）|balance|(?:实收资本|股本)(?:[（(]或股本[）)])?
"""
FIELDS = {
    parts[0]: tuple(parts[1:])
    for row in _FIELD_ROWS.strip().splitlines()
    if (parts := row.split("|", 3))
}
TOPICS = {
    "revenue": (
        "收入确认",
        ["收入", "合同资产", "合同负债", "分部"],
        [
            "控制权|履约",
            "总额|净额",
            "退货|折扣|返利|可变对价",
            "合同资产|合同负债",
            "分部",
        ],
    ),
    "inventory": (
        "存货确认",
        ["存货", "营业收入和营业成本", "营业成本"],
        ["计价|加权平均|先进先出", "成本", "跌价|转回", "结转", "陈酿|生产周期|基酒"],
    ),
    "ppe": (
        "PPE",
        ["固定资产", "在建工程", "借款费用", "资产减值"],
        ["资本化|确认条件", "折旧", "残值", "转固|预定可使用", "减值"],
    ),
    "intangibles": (
        "无形资产",
        ["无形资产", "开发支出", "研发", "商誉", "资产减值"],
        ["内部研究|研究阶段|开发阶段", "资本化", "摊销|使用寿命", "减值", "商誉"],
    ),
    "income_tax": (
        "所得税",
        ["所得税", "税项"],
        ["当期所得税", "递延所得税", "调整过程|调节", "优惠", "亏损|可抵扣"],
    ),
    "investment_income": (
        "投资损益",
        ["投资收益", "金融工具", "长期股权投资", "公允价值", "信用减值", "资产减值"],
        ["权益法", "处置", "股利|股息", "公允价值", "减值"],
    ),
    "finance_cost": (
        "财务成本",
        ["财务费用", "借款费用", "借款", "应付债券", "租赁"],
        ["利息费用|利息支出", "利息收入", "资本化", "汇兑", "租赁"],
    ),
}
SUPPLEMENTS = {
    "adjusted_profit": ("扣非净利润及非经常损益", "扣除非经常性损益|非经常性损益"),
    "share_count": ("期末股数及库存股数量（与股本金额分开）", "股份总数|回购|库存股"),
    "operating_breakdown": (
        "分部、产品、地区、量价及成本构成",
        "产销量|销售量|成本分析|营业成本构成|分行业|分产品|分地区|分部",
    ),
    "cfo_reconciliation": ("净利润到CFO调节表", "现金流量表补充资料"),
    "policy_changes": (
        "政策、估计变更及重述",
        "会计政策变更|会计估计变更|差错更正|追溯调整",
    ),
    "audit": ("审计意见及关键审计事项", "审计报告|关键审计事项"),
    "disclosed_fcf": ("公司披露FCF及其定义", "自由现金流|Free Cash Flow"),
    "capitalized_interest": ("资本化利息", "资本化|借款费用"),
    "cash_interest": (
        "现金利息（不以费用或分红偿息合计替代）",
        "利息.*现金|现金.*利息",
    ),
}
