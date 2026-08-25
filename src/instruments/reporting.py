"""Detailed HTML rendering for typed instrument audits."""

from __future__ import annotations

import html
from datetime import date
from typing import Optional

from .models import (
    CompanyFundamentals,
    FundProfile,
    GrowthMetric,
    InstrumentAuditReport,
    InstrumentProfile,
    InstrumentType,
    MetricStatus,
    MetricValue,
)


def _escape(value: object) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


def _metric_text(
    metric: MetricValue,
    *,
    suffix: str = "",
    decimals: int = 2,
) -> str:
    if metric.value is None:
        labels = {
            MetricStatus.KNOWN_ZERO: "0",
            MetricStatus.NOT_APPLICABLE: "不适用",
            MetricStatus.NOT_MEANINGFUL: "无意义",
            MetricStatus.STALE: "已过期",
            MetricStatus.CONFLICT: "冲突",
        }
        return labels.get(metric.status, "缺失")
    return f"{metric.value:,.{decimals}f}{suffix}"


def _metric_html(
    metric: MetricValue,
    *,
    suffix: str = "",
    decimals: int = 2,
) -> str:
    details = [metric.status.value]
    if metric.as_of:
        details.append(f"as-of {metric.as_of.isoformat()}")
    if metric.published_at:
        details.append(f"披露 {metric.published_at.isoformat()}")
    if metric.source:
        details.append(metric.source)
    if metric.alternatives:
        details.append(f"{len(metric.alternatives)} 个冲突/校验值")
    note = f"<br><small>{_escape(' · '.join(details))}</small>"
    if metric.note:
        note += f"<br><small>{_escape(metric.note)}</small>"
    return (
        f"<strong>{_escape(_metric_text(metric, suffix=suffix, decimals=decimals))}"
        f"</strong>{note}"
    )


def _growth_text(growth: Optional[GrowthMetric]) -> str:
    if growth is None:
        return "缺失"
    if growth.value_pct is not None:
        return f"{growth.value_pct:+.1f}%"
    if growth.interpretation:
        return growth.interpretation
    if growth.status == MetricStatus.NOT_MEANINGFUL:
        return "无意义"
    return "缺失"


def _growth_html(growth: Optional[GrowthMetric]) -> str:
    if growth is None:
        return "缺失"
    periods = []
    if growth.current_period:
        periods.append(growth.current_period.isoformat())
    if growth.prior_period:
        periods.append(growth.prior_period.isoformat())
    detail = " → ".join(reversed(periods))
    if growth.source:
        detail = f"{detail} · {growth.source}" if detail else growth.source
    values = ""
    if growth.current_value is not None or growth.prior_value is not None:
        values = (
            f"<br><small>基期 {_escape(growth.prior_value)}；"
            f"本期 {_escape(growth.current_value)}</small>"
        )
    return (
        f"<strong>{_escape(_growth_text(growth))}</strong>"
        f"<br><small>{_escape(detail)}</small>{values}"
    )


def _statement_age(
    company: CompanyFundamentals,
    evaluation_date: date,
) -> str:
    if not company.statements:
        return "无可用财务快照"
    latest = max(
        company.statements,
        key=lambda item: (item.period_end, item.published_at or date.min),
    )
    availability = latest.published_at or latest.period_end
    age_days = max((evaluation_date - availability).days, 0)
    publication = (
        latest.published_at.isoformat()
        if latest.published_at
        else "未知（仅允许当前审计）"
    )
    return (
        f"报告期 {latest.period_end.isoformat()}；披露 {publication}；"
        f"账龄 {age_days} 天；{latest.source}；"
        f"{latest.currency or '币种未知'}"
    )


def _company_detail(
    profile: InstrumentProfile,
    evaluation_date: date,
) -> str:
    company = profile.company
    if company is None:
        return ""
    latest = (
        max(
            company.statements,
            key=lambda item: (item.period_end, item.published_at or date.min),
        )
        if company.statements
        else None
    )
    raw_rows = []
    for label, value in (
        ("总股本", company.total_shares),
        ("每股净资产", company.book_value_per_share),
        ("TTM 营业收入", company.ttm_revenue),
        ("TTM 归母净利润", company.ttm_net_income_parent),
        ("TTM 扣非归母净利润", company.ttm_adjusted_net_income_parent),
    ):
        raw_rows.append(
            f"<tr><th>{_escape(label)}</th><td>{_metric_html(value)}</td></tr>"
        )
    for label, value in (
        ("Latest quarter free cash flow", company.latest_quarter_free_cash_flow),
        ("TTM free cash flow", company.ttm_free_cash_flow),
    ):
        raw_rows.append(
            f"<tr><th>{_escape(label)}</th><td>{_metric_html(value)}</td></tr>"
        )
    if latest is not None:
        for label, value in (
            ("最新期归母净资产", latest.parent_equity),
            ("最新期营业收入", latest.revenue),
            ("最新期归母净利润", latest.net_income_parent),
            ("最新期扣非归母净利润", latest.adjusted_net_income_parent),
        ):
            raw_rows.append(
                f"<tr><th>{_escape(label)}</th><td>{_escape(value)}</td></tr>"
            )

    valuation_rows = "".join(
        (
            f"<tr><th>PE(TTM)</th><td>{_metric_html(company.pe_ttm)}</td></tr>",
            f"<tr><th>PB</th><td>{_metric_html(company.pb)}</td></tr>",
            (
                "<tr><th>ROE(TTM)</th>"
                f"<td>{_metric_html(company.roe_ttm, suffix='%')}</td></tr>"
            ),
            (
                "<tr><th>股息率(TTM)</th>"
                f"<td>{_metric_html(company.dividend_yield, suffix='%')}</td></tr>"
            ),
            (
                "<tr><th>行情源 PE</th>"
                f"<td>{_metric_html(company.quoted_pe)}</td></tr>"
            ),
            (
                "<tr><th>行情源 PB</th>"
                f"<td>{_metric_html(company.quoted_pb)}</td></tr>"
            ),
        )
    )
    growth_labels = (
        ("营收同比", "revenue_yoy"),
        ("营收环比", "revenue_qoq"),
        ("利润同比", "net_income_yoy"),
        ("利润环比", "net_income_qoq"),
        ("扣非利润同比", "adjusted_net_income_yoy"),
        ("扣非利润环比", "adjusted_net_income_qoq"),
        ("TTM 营收同比", "revenue_ttm_yoy"),
        ("TTM 利润同比", "net_income_ttm_yoy"),
    )
    growth_rows = "".join(
        f"<tr><th>{_escape(label)}</th>"
        f"<td>{_growth_html(company.growth.get(key))}</td></tr>"
        for label, key in growth_labels
    )
    return f"""
<p class="freshness">{_escape(_statement_age(company, evaluation_date))}</p>
<div class="grid">
  <div><h4>财务原始值与 TTM</h4><table>{''.join(raw_rows)}</table></div>
  <div><h4>现价推导与行情校验</h4><table>{valuation_rows}</table></div>
  <div><h4>利润与营收增长</h4><table>{growth_rows}</table></div>
</div>
"""


def _look_through_rows(fund: FundProfile) -> str:
    labels = {
        "pe_ttm": "穿透 PE",
        "pb": "穿透 PB",
        "roe_ttm": "穿透 ROE",
        "dividend_yield": "穿透股息率",
        "revenue_yoy": "成分营收同比加权中位数",
        "revenue_qoq": "成分营收环比加权中位数",
        "net_income_yoy": "成分利润同比加权中位数",
        "net_income_qoq": "成分利润环比加权中位数",
    }
    rows = []
    for key, label in labels.items():
        item = fund.look_through.get(key)
        if item is None:
            continue
        suffix = "%" if key in {
            "roe_ttm",
            "dividend_yield",
            "revenue_yoy",
            "revenue_qoq",
            "net_income_yoy",
            "net_income_qoq",
        } else ""
        rows.append(
            f"<tr><th>{_escape(label)}</th>"
            f"<td>{_metric_html(item.value, suffix=suffix)}</td>"
            f"<td>{item.covered_weight * 100:.1f}%</td></tr>"
        )
    return "".join(rows)


def _holdings_table(fund: FundProfile) -> str:
    if not fund.top_holdings:
        return "<p>未取得带日期的前十大成分；不会用过期或硬编码持仓代替。</p>"
    rows = []
    for holding in fund.top_holdings:
        facts = holding.fundamentals
        rows.append(
            "<tr>"
            f"<td>{_escape(holding.code)}</td>"
            f"<td>{_escape(holding.name)}</td>"
            f"<td>{holding.weight * 100:.2f}%</td>"
            f"<td>{_escape(_metric_text(facts.pe_ttm))}</td>"
            f"<td>{_escape(_metric_text(facts.pb))}</td>"
            f"<td>{_escape(_metric_text(facts.roe_ttm, suffix='%'))}</td>"
            f"<td>{_escape(_growth_text(facts.revenue_yoy))}</td>"
            f"<td>{_escape(_growth_text(facts.revenue_qoq))}</td>"
            f"<td>{_escape(_growth_text(facts.net_income_yoy))}</td>"
            f"<td>{_escape(_growth_text(facts.net_income_qoq))}</td>"
            f"<td>{_escape(_metric_text(facts.dividend_yield, suffix='%'))}</td>"
            f"<td>{_escape(holding.as_of or fund.holdings_as_of)}</td>"
            f"<td>{_escape(holding.source)}</td>"
            "</tr>"
        )
    return f"""
<div class="wide"><h4>前十大成分股逐只画像</h4>
<table><thead><tr>
<th>代码</th><th>名称</th><th>权重</th><th>PE</th><th>PB</th><th>ROE</th>
<th>营收同比</th><th>营收环比</th><th>利润同比</th><th>利润环比</th>
<th>股息率</th><th>持仓日期</th><th>来源</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
"""


def _fund_detail(profile: InstrumentProfile) -> str:
    fund = profile.fund
    if fund is None:
        return ""
    fund_rows = "".join(
        (
            f"<tr><th>发行人</th><td>{_escape(fund.issuer)}</td></tr>",
            f"<tr><th>跟踪指数</th><td>{_escape(fund.tracking_index)}</td></tr>",
            f"<tr><th>资产类别</th><td>{_escape(fund.asset_class)}</td></tr>",
            f"<tr><th>AUM</th><td>{_metric_html(fund.aum)}</td></tr>",
            (
                "<tr><th>管理费率</th>"
                f"<td>{_metric_html(fund.expense_ratio, suffix='%')}</td></tr>"
            ),
            f"<tr><th>NAV</th><td>{_metric_html(fund.nav_per_unit)}</td></tr>",
            (
                "<tr><th>溢价/折价率</th>"
                f"<td>{_metric_html(fund.premium_discount_rate, suffix='%')}</td>"
                "</tr>"
            ),
            (
                "<tr><th>分红/分派率</th>"
                f"<td>{_metric_html(fund.dividend_yield, suffix='%')}</td></tr>"
            ),
            (
                "<tr><th>跟踪偏差</th>"
                f"<td>{_metric_html(fund.tracking_difference, suffix='%')}</td>"
                "</tr>"
            ),
            f"<tr><th>久期</th><td>{_metric_html(fund.duration)}</td></tr>",
            (
                "<tr><th>到期收益率</th>"
                f"<td>{_metric_html(fund.yield_to_maturity, suffix='%')}</td>"
                "</tr>"
            ),
        )
    )
    reit_rows = ""
    if profile.instrument_type == InstrumentType.REIT:
        reit_rows = "".join(
            (
                f"<tr><th>P/NAV</th><td>{_metric_html(fund.p_nav)}</td></tr>",
                f"<tr><th>TTM FFO</th><td>{_metric_html(fund.ttm_ffo)}</td></tr>",
                f"<tr><th>每份 FFO</th><td>{_metric_html(fund.ffo_per_unit)}</td></tr>",
                f"<tr><th>P/FFO</th><td>{_metric_html(fund.p_ffo)}</td></tr>",
                (
                    "<tr><th>出租率</th>"
                    f"<td>{_metric_html(fund.occupancy_rate, suffix='%')}</td></tr>"
                ),
                f"<tr><th>资产类型</th><td>{_escape(fund.property_type)}</td></tr>",
            )
        )
    look_through = _look_through_rows(fund)
    holdings_date = _escape(fund.holdings_as_of)
    return f"""
<p class="freshness">前十大持仓日期 {holdings_date}；合计权重
{fund.top_holdings_weight * 100:.1f}%。穿透覆盖率按每项有效权重单独计算。</p>
<div class="grid">
  <div><h4>基金主体</h4><table>{fund_rows}{reit_rows}</table></div>
  <div><h4>穿透聚合（非基金会计利润）</h4>
    <table><thead><tr><th>指标</th><th>值</th><th>有效权重</th></tr></thead>
    <tbody>{look_through or '<tr><td colspan="3">缺失</td></tr>'}</tbody></table>
  </div>
</div>
{_holdings_table(fund)}
"""


def _overview_rows(report: InstrumentAuditReport) -> str:
    rows = []
    for profile in report.profiles:
        missing = [
            key
            for key, status in profile.completeness.get("statuses", {}).items()
            if status in {MetricStatus.MISSING.value, MetricStatus.STALE.value}
        ]
        rows.append(
            "<tr>"
            f"<td>{_escape(profile.code)}</td>"
            f"<td>{_escape(profile.name)}</td>"
            f"<td>{_escape(profile.instrument_type.value)}</td>"
            f"<td>{_escape(profile.market)} / {_escape(profile.currency)}</td>"
            f"<td>{profile.completeness.get('fill_rate', 0) * 100:.1f}%</td>"
            f"<td>{_escape(', '.join(missing) or '无')}</td>"
            "</tr>"
        )
    return "".join(rows)


def _profile_block(
    profile: InstrumentProfile,
    evaluation_date: date,
) -> str:
    diagnostic = "; ".join(profile.diagnostics) or "无"
    detail = (
        _company_detail(profile, evaluation_date)
        if profile.company is not None
        else _fund_detail(profile)
    )
    return f"""
<section>
<h2>{_escape(profile.code)} {_escape(profile.name)}
<span>{_escape(profile.instrument_type.value)}</span></h2>
<p>市场/币种：{_escape(profile.market)} / {_escape(profile.currency)}；
现价：{_metric_html(profile.latest_price)}；
适用字段填充率：{profile.completeness.get('fill_rate', 0) * 100:.1f}%</p>
{detail}
<details><summary>来源失败、过期与冲突诊断</summary>
<p>{_escape(diagnostic)}</p>
<pre>{_escape(profile.source_attempts)}</pre></details>
</section>
"""


def render_detailed_audit_html(report: InstrumentAuditReport) -> str:
    """Render the standalone audit; daily email uses the compact renderer."""
    summary = report.summary
    blocks = "".join(
        _profile_block(profile, report.evaluation_date)
        for profile in report.profiles
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>标的画像审计：财务与基金穿透</title>
<style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;color:#243447;background:#f4f6f8}}
main{{max-width:1500px;margin:20px auto;padding:0 16px}}
section,.overview{{background:#fff;border-radius:8px;padding:18px;margin:16px 0;
box-shadow:0 1px 4px #ccd3da}}
h1,h2,h3,h4{{color:#20364d}} h2 span{{font-size:13px;color:#687b8f}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{border:1px solid #d9e0e7;padding:6px;text-align:left;vertical-align:top}}
th{{background:#edf2f6}} small,.freshness{{color:#617386}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}}
.wide{{overflow-x:auto}} pre{{white-space:pre-wrap;word-break:break-word;font-size:11px}}
</style></head><body><main>
<h1>标的画像审计：财务与基金穿透</h1>
<p>生成时间：{_escape(report.generated_at.isoformat(sep=' ', timespec='seconds'))}；
评价日：{_escape(report.evaluation_date)}；标的：
{summary.get('instrument_count', 0)}；适用字段填充率：
{summary.get('fill_rate', 0) * 100:.1f}%</p>
<p>当前画像只进入审计和日报展示，不进入 60 个月策略搜参。历史查询只使用
披露日不晚于评价日的数据；缺失、已知为零、不适用和无意义分别记录。</p>
<div class="overview"><h2>全量覆盖概览</h2>
<table><thead><tr><th>代码</th><th>名称</th><th>细分类型</th>
<th>市场/币种</th><th>填充率</th><th>缺失适用字段</th></tr></thead>
<tbody>{_overview_rows(report)}</tbody></table></div>
{blocks}
</main></body></html>"""
