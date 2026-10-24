"""Actual provided years; local quote input and isolated fictional valuation sample."""

import copy

import yaml

from tests import research_theme_fixtures as themes
from tests import research_valuation_fixtures as valuation


def valuation_text():
    text = valuation.valuation_section()
    text = text.replace(
        "以下三家公司只有FY2023–FY2025底稿，FY2019–FY2022本轮未取得；没有同日行情、投资人hurdle和已审查预测假设。不能据此给当前买入回报。形式已经包含七年历史和未来五年，不能将三年历史冒称七年，也不能借下方虚构算例填数。",
        "三家公司本轮实际提供FY2023–FY2025，全文就使用这三年，不增加其他年份。最新已知价从本轮快照读取；它不自动提供股数、完整现金预测或投资人hurdle。现有价格可以支持条件式每股估值，不能借下方虚构U算例填真实公司预测。",
    )
    text = text.replace(
        "最近七年历史 | 2019–2025；前四年未取得", "实际历史 | 2023–2025"
    )
    text = text.replace(
        "现价/报价时点/股类/币种/普通股市值 | 本轮未取得 | 本轮未取得 | 本轮未取得",
        "最新已知价/日期 | 见本轮行情快照 | 见本轮行情快照 | 见本轮行情快照",
    )
    text = text.replace(
        "FCFE收益率/现价五年IRR | 缺行情和现金预测，条件式待算 | 同左 | 同左",
        "FCFE收益率/五年IRR | 仍需现金/股数预测；可用已知价作条件式每股定价 | 还缺价格，条件式待算 | 仍需现金/股数预测",
    )
    text = text.replace("## 附录：七年窗口与验收边界", "## 附录：实际窗口与验收边界")
    text = text.replace(
        "FY2019–FY2022四年本轮未取得，FY2023–FY2025使用冻结旧值；缺口不是零或原文未披露。指数仍用各公司真实可得的共同FY2023=100，而非编造FY2019基数。历史七年有六个增长间隔，当前三年端点仅两个。",
        "FY2023–FY2025使用冻结旧值，只画实际三年；七年是目标资料量，当前不造空列。指数取共同可得FY2023=100，三年端点只有两个增长间隔。本轮行情快照优先于旧材料中未提供行情的描述，价格日期必须明示。",
    )
    return text


def chart_specs():
    return [*themes.chart_specs(), *valuation.chart_specs()[12:]]


def bodies(market):
    _, parts = themes.sections()
    forecast, rest = valuation_text().split("[[FORECAST_FIGURE]]")
    hurdle, close = rest.split("[[HURDLE_FIGURE]]")
    parts = [*parts[:-1], (forecast, [12]), (hurdle, [13]), (close, [])]
    specs, replies = chart_specs(), []
    for stage in ["draft", "review", "write"]:
        body = (
            "# 已有历史如何支持最新已知价回报：工程横比与白酒机制参照\n\n> 离线形式模拟，不是真实模型研究。实际历史FY2023–FY2025，未来五年FY2026E–FY2030E。虚构U与实际公司隔离。\n\n"
            + market
            + "\n\n"
        )
        for prose, figures in parts:
            prose = prose.replace(
                "| 同日总市值/价格 | 未提供 | 未提供 | 未提供 |",
                "| 最新已知价/日期 | 见本轮快照 | 见本轮快照 | 见本轮快照 |",
            )
            prose = prose.replace(
                "未取得同日行情就不计算股息率或当前估值倍数。",
                "已取得的价格按其实际日期使用；缺股数和普通股归属的项目保留条件，不拿权益替市值。",
            )
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
                "图6故意使用72.23，验证交接。"
                if stage == "draft"
                else "图6改回77.23；实际三年轴及本轮行情快照保留。"
            )
        replies.append(body)
    return replies
