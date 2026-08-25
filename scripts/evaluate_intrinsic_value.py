#!/usr/bin/env python3
"""Run the point-in-time intrinsic-value expert prototype."""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_history import PriceHistoryBundle  # noqa: E402
from src.fundamental_embedding.intrinsic_evaluation import (  # noqa: E402
    IntrinsicValueIntervalEvaluator,
    IntrinsicValueWalkForwardEvaluator,
)
from src.fundamental_embedding.intrinsic_value import (  # noqa: E402
    IntrinsicValueConfig,
    IntrinsicValueEngine,
    PointInTimeValuationBuilder,
    SubjectiveRiskAdjustment,
)

plt.switch_backend("Agg")


def _load_benchmark_prices(path: str | None) -> PriceHistoryBundle | None:
    """Load a real local benchmark cache as a beta-only price bundle."""

    if not path:
        return None
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"benchmark price file not found: {source}")
    raw = pd.read_csv(source)
    required = {"date", "open", "high", "low", "close"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(
            f"benchmark price file is missing columns: {missing}"
        )
    volume = (
        raw["volume"]
        if "volume" in raw.columns
        else pd.Series(0.0, index=raw.index)
    )
    frame = pd.DataFrame({
        "date": pd.to_datetime(raw["date"], errors="coerce"),
        "raw_open": pd.to_numeric(raw["open"], errors="coerce"),
        "raw_high": pd.to_numeric(raw["high"], errors="coerce"),
        "raw_low": pd.to_numeric(raw["low"], errors="coerce"),
        "raw_close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(volume, errors="coerce").fillna(0.0),
    }).dropna(subset=["date", "raw_close"])
    for name in ("open", "high", "low", "close"):
        frame[f"qfq_{name}"] = frame[f"raw_{name}"]
    frame["qfq_factor"] = 1.0
    frame["tradable"] = frame["volume"] > 0
    frame = frame.sort_values("date").drop_duplicates(
        subset=["date"], keep="last"
    )
    return PriceHistoryBundle(
        code=source.stem,
        prices=frame.reset_index(drop=True),
        source=f"local_real_price_cache:{source.resolve()}",
        diagnostics=[
            "beta benchmark uses unadjusted ETF close; cash dividends omitted"
        ],
    )


def _risk_from_dict(payload: dict[str, Any]) -> SubjectiveRiskAdjustment:
    if float(payload.get("extra_discount_rate", 0.0)) != 0.0:
        raise ValueError(
            "extra_discount_rate is forbidden; subjective event risk must "
            "not contaminate the cost of equity"
        )
    effective = payload.get("effective_from")
    expires = payload.get("expires_at")
    return SubjectiveRiskAdjustment(
        price_haircut=float(payload.get("price_haircut", 0.0)),
        adverse_event_probability=float(
            payload.get("adverse_event_probability", 0.0)
        ),
        adverse_event_loss=float(payload.get("adverse_event_loss", 0.0)),
        uncertainty_multiplier=float(
            payload.get("uncertainty_multiplier", 1.0)
        ),
        reason=str(payload.get("reason", "")),
        effective_from=(
            date.fromisoformat(str(effective)) if effective else None
        ),
        expires_at=date.fromisoformat(str(expires)) if expires else None,
    )


def _load_risks(path: str | None) -> dict[str, SubjectiveRiskAdjustment]:
    if not path:
        return {}
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    instruments = payload.get("instruments", payload)
    if not isinstance(instruments, dict):
        raise ValueError("subjective risk file must contain an instrument map")
    return {
        str(symbol): _risk_from_dict(item or {})
        for symbol, item in instruments.items()
    }


def _latest_estimates(
    builder: PointInTimeValuationBuilder,
    engine: IntrinsicValueEngine,
    config: IntrinsicValueConfig,
    risks: dict[str, SubjectiveRiskAdjustment],
) -> list[dict[str, Any]]:
    anchor = builder.latest_date()
    rows = []
    for symbol in builder.available_symbols():
        snapshot = builder.snapshot(symbol, anchor, config)
        if snapshot is None:
            continue
        estimate = engine.estimate(snapshot, risks.get(symbol))
        row = estimate.to_dict()
        row["dominant_expert"] = max(estimate.gate, key=estimate.gate.get)
        row["snapshot"] = {
            "earnings_per_share": snapshot.earnings_per_share,
            "free_cash_flow_per_share": snapshot.free_cash_flow_per_share,
            "book_value_per_share": snapshot.book_value_per_share,
            "dividend_per_share": snapshot.dividend_per_share,
            "roe": snapshot.roe,
            "growth": snapshot.growth,
            "payout_ratio": snapshot.payout_ratio,
            "cash_conversion": snapshot.cash_conversion,
            "financial_age_days": snapshot.financial_age_days,
            "capital_cost": (
                snapshot.capital_cost.to_dict()
                if snapshot.capital_cost is not None
                else None
            ),
        }
        rows.append(row)
    return rows


def _historical_cases(
    report: dict[str, Any], symbols: list[str]
) -> list[dict[str, Any]]:
    rows = report["rows"]
    cases = []
    for symbol in symbols:
        two_year = [
            item
            for item in rows
            if item["symbol"] == symbol and item["horizon"] == 504
        ]
        if not two_year:
            continue
        selected = max(two_year, key=lambda item: item["evaluation_date"])
        one_year = next((
            item
            for item in rows
            if item["symbol"] == symbol
            and item["horizon"] == 252
            and item["evaluation_date"] == selected["evaluation_date"]
        ), None)
        case = dict(selected)
        case["one_year_return"] = (
            one_year["forward_return"] if one_year else None
        )
        case["two_year_return"] = selected["forward_return"]
        cases.append(case)
    return cases


def _write_chart(report: dict[str, Any], output: Path) -> None:
    rows = [item for item in report["rows"] if item["horizon"] == 504]
    x = np.asarray([item["margin_of_safety"] for item in rows], dtype=float)
    y = np.asarray([item["forward_return"] for item in rows], dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    figure, axis = plt.subplots(figsize=(9.5, 5.5))
    axis.scatter(
        np.clip(x[valid], -2.0, 2.0),
        np.clip(y[valid], -1.0, 3.0),
        alpha=0.25,
        s=18,
        edgecolors="none",
    )
    axis.axvline(0.0, color="#b42318", linestyle="--", linewidth=1)
    axis.axhline(0.0, color="#667085", linestyle="--", linewidth=1)
    axis.set_xlabel("buy-price margin of safety (clipped)")
    axis.set_ylabel("two-year total return (clipped)")
    axis.set_title("Intrinsic-value margin vs two-year realized return")
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(output, dpi=160)
    plt.close(figure)


def _fmt(value: Any, percent: bool = False) -> str:
    if value is None:
        return "—"
    result = float(value)
    return f"{result:.1%}" if percent else f"{result:.3f}"


def _write_html(report: dict[str, Any], path: Path, chart_name: str) -> None:
    metrics_rows = []
    for horizon, item in report["metrics"].items():
        for score, metric in item["scores"].items():
            metrics_rows.append(
                "<tr>"
                f"<td>{html.escape(horizon)}</td>"
                f"<td>{html.escape(score)}</td>"
                f"<td>{_fmt(metric['mean_rank_ic'])}</td>"
                f"<td>{_fmt(metric['positive_quarter_rate'], True)}</td>"
                f"<td>{_fmt(metric['mean_top_bottom_spread'], True)}</td>"
                "</tr>"
            )
    case_rows = []
    for item in report["historical_cases"]:
        case_rows.append(
            "<tr>"
            f"<td>{html.escape(item['symbol'])}</td>"
            f"<td>{html.escape(item['evaluation_date'])}</td>"
            f"<td>{_fmt(item['current_price'])}</td>"
            f"<td>{_fmt(item['fair_value'])}</td>"
            f"<td>{_fmt(item['buy_price'])}</td>"
            f"<td>{html.escape(item['dominant_expert'])}</td>"
            f"<td>{_fmt(item['one_year_return'], True)}</td>"
            f"<td>{_fmt(item['two_year_return'], True)}</td>"
            "</tr>"
        )
    current_rows = []
    for item in report["selected_latest_cases"]:
        current_rows.append(
            "<tr>"
            f"<td>{html.escape(item['symbol'])}</td>"
            f"<td>{_fmt(item['current_price'])}</td>"
            f"<td>{_fmt(item['fair_value_low'])}</td>"
            f"<td>{_fmt(item['fair_value'])}</td>"
            f"<td>{_fmt(item['fair_value_high'])}</td>"
            f"<td>{_fmt(item['buy_price'])}</td>"
            f"<td>{_fmt(item['margin_of_safety'], True)}</td>"
            f"<td>{html.escape(item['dominant_expert'])}</td>"
            "</tr>"
        )
    capital_rows = []
    for item in report["selected_latest_cases"]:
        capital = item.get("snapshot", {}).get("capital_cost") or {}
        beta_components = capital.get("beta_components") or []
        beta_detail = "<br>".join(
            (
                f"{component.get('horizon_years')}Y "
                f"{html.escape(str(component.get('frequency', '')))}: "
                f"{_fmt(component.get('beta'))} "
                f"[CI {_fmt(component.get('confidence_low'))}, "
                f"{_fmt(component.get('confidence_high'))}], "
                f"n={component.get('observations', '—')}, "
                f"R²={_fmt(component.get('r_squared'))}"
            )
            for component in beta_components
        ) or "—"
        capital_rows.append(
            "<tr>"
            f"<td>{html.escape(item['symbol'])}</td>"
            f"<td>{html.escape(str(capital.get('assumptions_as_of', '—')))}</td>"
            f"<td>{_fmt(capital.get('risk_free_rate'), True)}</td>"
            f"<td>{_fmt(capital.get('market_risk_premium'), True)}</td>"
            f"<td>{_fmt(capital.get('market_risk_premium_low'), True)}–"
            f"{_fmt(capital.get('market_risk_premium_high'), True)}</td>"
            f"<td>{_fmt(capital.get('raw_beta'))}</td>"
            f"<td>{_fmt(capital.get('adjusted_beta'))}</td>"
            f"<td>{_fmt(capital.get('beta_low'))}–"
            f"{_fmt(capital.get('beta_high'))}</td>"
            f"<td class=\"detail\">{beta_detail}</td>"
            f"<td>{_fmt(capital.get('cost_of_equity'), True)}</td>"
            f"<td>{_fmt(capital.get('cost_of_equity_low'), True)}–"
            f"{_fmt(capital.get('cost_of_equity_high'), True)}</td>"
            f"<td>{_fmt(capital.get('pre_tax_cost_of_debt'), True)}</td>"
            f"<td>{_fmt(capital.get('effective_tax_rate'), True)}</td>"
            f"<td>{_fmt(capital.get('equity_weight'), True)}</td>"
            f"<td>{_fmt(capital.get('debt_weight'), True)}</td>"
            f"<td>{_fmt(capital.get('wacc'), True)}</td>"
            f"<td>{_fmt(capital.get('wacc_low'), True)}–"
            f"{_fmt(capital.get('wacc_high'), True)}</td>"
            f"<td>{_fmt(capital.get('net_debt'))}</td>"
            "</tr>"
        )
    bridge_rows = []
    for period_label, items in (
        ("历史", report["historical_cases"]),
        ("当前", report["selected_latest_cases"]),
    ):
        for item in items:
            if period_label == "历史":
                assumptions = item.get("expert_assumptions", {})
            else:
                assumptions = {
                    expert["expert_id"]: {
                        "low": expert["low"],
                        "base": expert["base"],
                        "high": expert["high"],
                        **expert["assumptions"],
                    }
                    for expert in item["experts"]
                    if expert["available"]
                }
            for expert_id, values in assumptions.items():
                weight = item["gate"].get(expert_id, 0.0)
                if weight <= 0:
                    continue
                rates = values.get("discount_rates", [])
                base_rate = rates[1] if len(rates) > 1 else None
                diagnostic = item.get("reverse_dcf", {}).get(
                    expert_id, {}
                )
                policy = values.get("required_return_policy", {})
                market_cost = policy.get("market_cost_of_equity", {})
                investor_return = policy.get(
                    "investor_required_return", {}
                )
                implied = diagnostic.get(
                    "market_implied_explicit_growth",
                    item.get("market_implied_growth", {}).get(expert_id),
                )
                bridge_rows.append(
                    "<tr>"
                    f"<td>{period_label}</td>"
                    f"<td>{html.escape(item['symbol'])}</td>"
                    f"<td>{html.escape(item['evaluation_date'])}</td>"
                    f"<td>{html.escape(expert_id)}</td>"
                    f"<td>{_fmt(weight, True)}</td>"
                    f"<td>{html.escape(str(values.get('cash_flow_kind', '—')))}</td>"
                    f"<td>{_fmt(values.get('cash_per_share'))}</td>"
                    f"<td>{_fmt(market_cost.get('base'), True)}</td>"
                    f"<td>{_fmt(investor_return.get('base'), True)}</td>"
                    f"<td>{_fmt(base_rate, True)}</td>"
                    f"<td>{_fmt(values.get('explicit_growth'), True)}</td>"
                    f"<td>{_fmt(values.get('terminal_growth'), True)}</td>"
                    f"<td>{_fmt(implied, True)}</td>"
                    f"<td>{_fmt(diagnostic.get('growth_gap'), True)}</td>"
                    "<td>"
                    f"{html.escape(str(diagnostic.get('interpretation', '—')))}"
                    "</td>"
                    f"<td>{_fmt(values.get('low'))}</td>"
                    f"<td>{_fmt(values.get('base'))}</td>"
                    f"<td>{_fmt(values.get('high'))}</td>"
                    "</tr>"
                )
    interval = report.get("next_report_interval", {})
    interval_html = ""
    if "error" in interval:
        interval_html = (
            "<h2>下一季报前价格区间</h2>"
            f"<p class=\"warn\">无法校准：{html.escape(interval['error'])}</p>"
        )
    elif interval:
        validation = interval["validation"]
        dcf_metrics = validation["dcf_metrics"]
        baseline_metrics = validation["baseline_metrics"]
        metric_rows = []
        for label, parameters, metrics in (
            (
                "候选区间：可使用DCF、Beta和主观风险",
                validation["dcf_parameters"],
                dcf_metrics,
            ),
            (
                "强制基线：现价 + Beta",
                validation["baseline_parameters"],
                baseline_metrics,
            ),
        ):
            metric_rows.append(
                "<tr>"
                f"<td>{html.escape(label)}</td>"
                f"<td>{_fmt(metrics['mean_daily_coverage'], True)}</td>"
                f"<td>{_fmt(metrics['pooled_daily_coverage'], True)}</td>"
                f"<td>{_fmt(metrics['full_path_coverage'], True)}</td>"
                f"<td>{_fmt(metrics['mean_relative_width'], True)}</td>"
                f"<td>{_fmt(metrics['mean_beta_adjusted_width'], True)}</td>"
                f"<td>{_fmt(metrics['mean_objective'])}</td>"
                '<td class="detail">'
                f"{html.escape(json.dumps(parameters, ensure_ascii=False))}"
                "</td>"
                "</tr>"
            )
        forecast_rows = []
        selected_symbols = {
            case["symbol"] for case in report.get("selected_latest_cases", [])
        }
        for item in interval["deployment"]["latest_forecasts"]:
            if selected_symbols and item["symbol"] not in selected_symbols:
                continue
            risk = item.get("risk") or {}
            risk_text = (
                f"固定折价={_fmt(risk.get('price_haircut'), True)}；"
                f"事件概率={_fmt(risk.get('adverse_event_probability'), True)}；"
                f"事件损失={_fmt(risk.get('adverse_event_loss'), True)}"
            )
            forecast_rows.append(
                "<tr>"
                f"<td>{html.escape(item['symbol'])}</td>"
                f"<td>{_fmt(item['current_price'])}</td>"
                f"<td>{_fmt(item['price_lower'])}</td>"
                f"<td>{_fmt(item['price_center'])}</td>"
                f"<td>{_fmt(item['price_upper'])}</td>"
                f"<td>{_fmt(item['relative_width'], True)}</td>"
                f"<td>{_fmt(item['beta'])}</td>"
                f"<td>{_fmt(item['expected_coverage_probability'], True)}</td>"
                f"<td>{_fmt(item['fair_value'])}</td>"
                f"<td>{_fmt(item['cost_of_equity'], True)}</td>"
                f"<td>{_fmt(item['required_return'], True)}</td>"
                f"<td class=\"detail\">{risk_text}</td>"
                "</tr>"
            )
        interval_html = f"""
<h2>下一季报前价格区间：时间留出验证</h2>
<p>区间从财报披露后的首个可用价格开始，到下一份新报告披露前结束。
目标函数为覆盖率减去宽度惩罚；书面公式中的宽度项改为负号，否则优化会
无上限地扩大区间。主观风险用“固定折价 + 事件概率 × 事件损失”移动中枢，
事件损失的伯努利不确定性只扩张下界。没有 effective_from 的人工判断不会
回填历史。</p>
<table><tr><th>模型</th><th>平均日覆盖率</th><th>合并日覆盖率</th>
<th>整段完全覆盖率</th><th>平均相对宽度</th><th>Beta调整宽度</th>
<th>目标分</th><th>参数</th></tr>{''.join(metric_rows)}</table>
<h3>当前案例：有效至下一份新财报</h3>
<table><tr><th>代码</th><th>现价</th><th>预估最低</th><th>中枢</th>
<th>预估最高</th><th>区间宽度/现价</th><th>Beta</th><th>留出期预期覆盖</th>
<th>DCF公允价值</th><th>市场CAPM股权成本</th>
<th>实际必要回报率</th><th>主观风险</th></tr>
{''.join(forecast_rows)}</table>
"""

    summary = {
        "dataset": report["dataset"],
        "value_config": report["value_config"],
        "acceptance": report["acceptance"],
        "latest_coverage": report["latest_coverage"],
        "next_report_interval": {
            "contract": interval.get("contract"),
            "dataset": interval.get("dataset"),
            "acceptance": interval.get("acceptance"),
        },
    }
    summary_json = html.escape(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    content = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>画像驱动内在价值专家实验</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;max-width:1180px;
margin:24px auto;padding:0 16px;color:#172b4d}}
table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}
th,td{{border:1px solid #d0d5dd;padding:7px;text-align:right}}
th:first-child,td:first-child{{text-align:left}} th{{background:#f2f4f7}}
.detail{{min-width:270px;text-align:left;font-size:12px;line-height:1.45}}
.warn{{background:#fff4e5;border-left:5px solid #f79009;padding:12px}}
img{{max-width:100%}} pre{{background:#f5f7fa;padding:14px;white-space:pre-wrap}}
</style></head><body>
<h1>画像驱动内在价值与下一季报价格区间实验</h1>
<p class="warn">现金流 DCF 使用个人必要股权回报率，不再混用 WACC 或减净债务。
当前 OCF-CAPEX 是“未取得净借款时的 FCFE 代理”，报告会显式标记这一限制。
默认估值档位为 8%/7.5%/7%，永续增长为2%；主观风险不污染这些贴现率，
而是单独降低保守买入价并扩张下一季报前价格区间的下行侧。</p>
<p>市场 CAPM 采用同期十年国债收益率、沪深300前瞻隐含风险溢价和多窗口 Beta；
Beta 同时估计2/3/5年日频与周频，用有效估计的中位数作为中心、四分位区间
作为敏感性。CAPM 是市场定价诊断，并作为高风险公司的必要回报率下限；
它不会把美的这类低 Beta 公司的贴现率压低到约4.4%。资本结构和 WACC
仍可作为诊断展示，但本版 FCFE 代理不使用 WACC。</p>
<p>反向 DCF 只回答“现价隐含了多少显式增长”，并与财务历史推导的增长率并排
展示。它不会把现价隐含增长反灌进公允价值，因此公允价值不随输入现价变化。</p>
<p class="warn">历史评价只有在估值日前已有资本市场假设和财务披露时才使用真实
资本成本；当前尚未回填的历史日期仍是显式 fallback，不能与当前 WACC 结果混为
一谈。前瞻 ERP 是显式模型估计，不冒充历史已实现 ERP；历史已实现口径必须等
获得含分红的沪深300全收益指数后再独立验证，不能用价格指数偷换。</p>
{interval_html}
<h2>一至两年横截面诊断（非本版区间主目标）</h2>
<table><tr><th>交易日</th><th>评分</th><th>平均Rank IC</th>
<th>正IC季度</th><th>首尾组收益差</th></tr>{''.join(metrics_rows)}</table>
<img src="{html.escape(chart_name)}" alt="two year valuation evaluation">
<h2>可回看历史案例</h2>
<table><tr><th>代码</th><th>估值日</th><th>当时价格</th><th>公允价值</th>
<th>买入价</th><th>主专家</th><th>后1年</th><th>后2年</th></tr>
{''.join(case_rows)}</table>
<h2>当前案例（尚无未来标签）</h2>
<table><tr><th>代码</th><th>当前价格</th><th>价值低位</th><th>公允价值</th>
<th>价值高位</th><th>保守买入价</th><th>买入安全边际</th><th>主专家</th></tr>
{''.join(current_rows)}</table>
<h2>当前资本成本桥</h2>
<table><tr><th>代码</th><th>假设日期</th><th>无风险</th><th>前瞻ERP</th>
<th>ERP敏感区间</th><th>5年周频Beta</th><th>稳健Beta</th><th>Beta四分位</th>
<th>Beta明细</th><th>股权成本</th><th>股权成本区间</th><th>税前债务成本</th>
<th>有效税率</th><th>股权权重</th><th>债务权重</th><th>WACC</th>
<th>WACC区间</th><th>净债务</th></tr>
{''.join(capital_rows)}</table>
<h2>逐专家估值桥</h2>
<table><tr><th>时点</th><th>代码</th><th>估值日</th><th>专家</th>
<th>Gate</th><th>现金流口径</th><th>每股现金流</th>
<th>市场CAPM</th><th>个人必要回报率</th><th>实际贴现率</th>
<th>基本面显式增长</th><th>终值增长</th><th>市场隐含增长</th>
<th>隐含-基本面</th><th>反向DCF解释</th>
<th>低估值</th><th>基准估值</th><th>高估值</th></tr>
{''.join(bridge_rows)}</table>
<h2>合同与覆盖</h2><pre>{summary_json}</pre>
</body></html>"""
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--market", default="a_share")
    parser.add_argument(
        "--examples", default="000001,000333,000625,000983"
    )
    parser.add_argument("--risk-config")
    parser.add_argument("--required-return-low", type=float, default=0.07)
    parser.add_argument("--required-return", type=float, default=0.075)
    parser.add_argument("--required-return-high", type=float, default=0.08)
    parser.add_argument("--terminal-growth", type=float, default=0.02)
    parser.add_argument(
        "--disable-market-cost-floor",
        action="store_true",
        help="use only the investor hurdle, ignoring a higher market CAPM",
    )
    parser.add_argument(
        "--skip-long-horizon",
        action="store_true",
        help="skip the legacy 252/504-day cross-sectional diagnostic",
    )
    parser.add_argument(
        "--benchmark-prices",
        default=str(PROJECT_ROOT / "cache" / "data" / "510300.csv"),
        help="real historical benchmark CSV used only for point-in-time beta",
    )
    args = parser.parse_args()

    examples = [item.strip() for item in args.examples.split(",") if item.strip()]
    risks = _load_risks(args.risk_config)
    value_config = IntrinsicValueConfig(
        investor_required_return_low=args.required_return_low,
        investor_required_return=args.required_return,
        investor_required_return_high=args.required_return_high,
        terminal_growth=args.terminal_growth,
        market_cost_of_equity_floor=not args.disable_market_cost_floor,
    )
    benchmark_bundle = _load_benchmark_prices(args.benchmark_prices)
    builder = PointInTimeValuationBuilder(
        args.data_root,
        market=args.market,
        benchmark_bundle=benchmark_bundle,
    )
    engine = IntrinsicValueEngine(value_config)
    if args.skip_long_horizon:
        report = {
            "dataset": {
                "root": str(Path(args.data_root).resolve()),
                "market": args.market,
                "symbol_count": len(builder.available_symbols()),
            },
            "value_config": asdict(value_config),
            "metrics": {},
            "rows": [],
            "acceptance": {
                "no_lookahead": True,
                "market_price_not_used_in_cash_flows_or_growth": True,
                "reverse_dcf_is_diagnostic_only": True,
                "subjective_risk_is_explicit": True,
                "long_horizon_diagnostic_skipped": True,
                "production_ready": False,
            },
        }
    else:
        evaluator = IntrinsicValueWalkForwardEvaluator(
            builder, value_config
        )
        report = evaluator.run(risks=risks)
    interval_evaluator = IntrinsicValueIntervalEvaluator(
        builder, value_config
    )
    try:
        report["next_report_interval"] = interval_evaluator.run(risks=risks)
    except ValueError as exc:
        report["next_report_interval"] = {
            "contract": "next-report-price-interval-1",
            "error": str(exc),
            "acceptance": {"production_ready": False},
        }
    latest = _latest_estimates(builder, engine, value_config, risks)
    report["latest_estimates"] = latest
    report["beta_benchmark"] = {
        "path": str(Path(args.benchmark_prices).resolve()),
        "source": benchmark_bundle.source if benchmark_bundle else None,
        "diagnostics": (
            benchmark_bundle.diagnostics if benchmark_bundle else []
        ),
    }
    report["latest_coverage"] = {
        "estimated": sum(item["buy_price"] is not None for item in latest),
        "total": len(latest),
        "by_dominant_expert": {
            name: sum(item["dominant_expert"] == name for item in latest)
            for name in sorted({item["dominant_expert"] for item in latest})
        },
    }
    report["historical_cases"] = _historical_cases(report, examples)
    report["selected_latest_cases"] = [
        item for item in latest if item["symbol"] in examples
    ]

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if report["rows"]:
        pd.DataFrame(report["rows"]).drop(columns=["gate"]).to_csv(
            output / "walk_forward_rows.csv",
            index=False,
            encoding="utf-8",
        )
    pd.DataFrame([
        {
            "symbol": item["symbol"],
            "current_price": item["current_price"],
            "fair_value_low": item["fair_value_low"],
            "fair_value": item["fair_value"],
            "fair_value_high": item["fair_value_high"],
            "buy_price": item["buy_price"],
            "margin_of_safety": item["margin_of_safety"],
            "confidence": item["confidence"],
            "dominant_expert": item["dominant_expert"],
            **{f"gate_{name}": value for name, value in item["gate"].items()},
        }
        for item in latest
    ]).to_csv(output / "latest_estimates.csv", index=False, encoding="utf-8")
    interval = report["next_report_interval"]
    if "error" not in interval:
        pd.DataFrame(interval["validation"]["dcf_metrics"]["rows"]).to_csv(
            output / "next_report_validation_rows.csv",
            index=False,
            encoding="utf-8",
        )
        pd.DataFrame(interval["deployment"]["latest_forecasts"]).to_csv(
            output / "next_report_latest_forecasts.csv",
            index=False,
            encoding="utf-8",
        )
    chart = output / "two_year_margin_vs_return.png"
    _write_chart(report, chart)
    _write_html(report, output / "report.html", chart.name)
    print(json.dumps({
        "report": str((output / "report.html").resolve()),
        "metrics": report["metrics"],
        "historical_cases": report["historical_cases"],
        "latest_cases": report["selected_latest_cases"],
        "latest_coverage": report["latest_coverage"],
        "acceptance": report["acceptance"],
        "next_report_interval": (
            {
                "dataset": interval.get("dataset"),
                "validation": {
                    "dcf_parameters": interval["validation"]["dcf_parameters"],
                    "dcf_metrics": {
                        key: value
                        for key, value in interval["validation"][
                            "dcf_metrics"
                        ].items()
                        if key != "rows"
                    },
                    "baseline_parameters": interval["validation"][
                        "baseline_parameters"
                    ],
                    "baseline_metrics": {
                        key: value
                        for key, value in interval["validation"][
                            "baseline_metrics"
                        ].items()
                        if key != "rows"
                    },
                    "dcf_incremental_objective": interval["validation"][
                        "dcf_incremental_objective"
                    ],
                },
                "deployment_parameters": interval["deployment"]["parameters"],
                "acceptance": interval["acceptance"],
            }
            if "error" not in interval
            else interval
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
