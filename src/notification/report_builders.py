"""Shared event, validated Ke text and manual optimizer report builders."""

from __future__ import annotations

import html as html_lib
import logging
from datetime import date, datetime
from itertools import groupby

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from ..markets import _detect_fine_group
from ..utils.in_kind_reporting import render_in_kind_ledger

logger = logging.getLogger(__name__)


def _fmt(v, unit="", fmt_spec=".2f"):
    """安全格式化数值：None/pd.NA/NaN → "—"，数值 → 格式化。

    原则：绝不凭空造零。缺失数据和真实零值必须可区分。
    """
    if v is None:
        return "—"
    try:
        if pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    if unit:
        return f"{v:{fmt_spec}}{unit}"
    return f"{v:{fmt_spec}}"


def _signed_text_metric(value, unit="%", precision=2) -> str:
    """Format optional report metrics without turning missing data into zero."""
    if value is None:
        return "—"
    try:
        if pd.isna(value):
            return "—"
        return f"{float(value):+.{precision}f}{unit}"
    except (TypeError, ValueError):
        return "—"


def _html_escape(value, default="—") -> str:
    """Escape notification data without collapsing real zeroes to a dash."""
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    return html_lib.escape(str(value), quote=True)


def _safe_html_url(value) -> str:
    """Return an escaped http(s) URL or an empty string for unsafe input."""
    if value is None:
        return ""
    raw = str(value).strip()
    if raw.lower().startswith(("http://", "https://")):
        return html_lib.escape(raw, quote=True)
    return ""


def build_brief_entries(stock_data, today) -> list[dict]:
    """Extract recent price rows without technical-strategy fields."""
    from datetime import datetime as dt_mod

    import pandas as pd

    today_date = today.date() if hasattr(today, "date") else today
    entries = []

    for _, row in stock_data.iterrows():
        code = str(row.get("stock_code", ""))
        name = str(row.get("stock_name") or code)
        close_price = row.get("close")
        open_price = row.get("open")
        prev_close = row.get("prev_close")
        try:
            prev_close = float(prev_close)
            if not np.isfinite(prev_close) or prev_close <= 0:
                prev_close = None
        except (TypeError, ValueError):
            prev_close = None

        # 3 天日期过滤
        data_date = row.get("date")
        in_trading = False
        if data_date is not None and not pd.isna(data_date):
            try:
                date_str = str(data_date)[:10]
                data_dt = dt_mod.strptime(date_str, "%Y-%m-%d").date()  # noqa: DTZ007 -- calendar only
                days_since = (today_date - data_dt).days
                in_trading = 0 <= days_since <= 3
            except (TypeError, ValueError, OverflowError) as exc:
                logger.debug("Report row omitted: %s", type(exc).__name__)
                continue
        if not in_trading:
            continue

        entries.append(
            {
                "code": code,
                "name": name,
                "close": close_price,
                "open": open_price,
                "prev_close": prev_close,
            }
        )

    return entries


def _benchmark_label(code: str) -> str:
    """Return the human label used consistently in every notification channel."""
    return "无风险" if code == "risk_free" else code


def _format_benchmark_comparison(report) -> tuple[list[str], list[str]]:
    """Render cumulative benchmark returns and forward win rates separately."""
    returns = getattr(report, "benchmark_returns", None) or {}
    win_rates = getattr(report, "benchmark_win_rates", None) or {}
    details = getattr(report, "benchmark_details", None) or {}
    primary = getattr(report, "primary_benchmark", "") or ""
    return_parts = []
    win_parts = []
    for code, benchmark_return in returns.items():
        try:
            excess = float(report.total_return) - float(benchmark_return)
            marker = "★" if code == primary else ""
            return_parts.append(
                f"{marker}{_benchmark_label(code)} {float(benchmark_return):+.1f}% / "
                f"超额{excess:+.1f}%"
            )
        except (TypeError, ValueError):
            continue
    for code, win_rate in win_rates.items():
        try:
            detail = details.get(code, {}) or {}
            wins = detail.get("win_days")
            effective = detail.get("comparison_days", detail.get("effective_days"))
            count_text = (
                f" ({int(wins)}/{int(effective)})"
                if wins is not None and effective is not None
                else ""
            )
            marker = "★" if code == primary else ""
            win_parts.append(
                f"{marker}{_benchmark_label(code)} {float(win_rate):.0f}%{count_text}"
            )
        except (TypeError, ValueError):
            continue
    return return_parts, win_parts


def build_strategy_text_summary(session, markdown: bool = False) -> str:
    """Validated Ke curves as the text-channel substitute for the shared chart."""
    from .report_policy import build_return_curves_text

    return build_return_curves_text(getattr(session, "strategy_return_curves", None))


def _optimizer_execution_description(execution: dict) -> str:
    """Describe the selected execution model without inventing cash limits."""
    if execution.get("model") == "target_weight":
        return (
            "目标仓位: "
            f"单标的上限 {float(execution['per_symbol_cap']):.0%} / "
            f"总仓位上限 {float(execution['total_exposure_cap']):.0%}"
        )
    return (
        "单笔现金上限: "
        f"买入 {float(execution.get('buy_cash_limit', 0)):.0f} / "
        f"卖出 {float(execution.get('sell_cash_limit', 0)):.0f} 元"
    )


def optimizer_notification_title(report, group_name: str = "") -> str:
    """Return a title consistent with both aggregate and per-market outcomes."""
    states = {
        str(getattr(item, "status", ""))
        for item in (getattr(report, "groups", {}) or {}).values()
    }
    aggregate_status = str(getattr(report, "status", "completed"))
    failed = aggregate_status in {"failed", "interrupted"} or bool(
        states & {"failed", "interrupted"}
    )
    finished = bool(states & {"completed", "no_candidates"})
    if states and "no_data" in states and states <= {"no_data", "not_run"}:
        title = "策略数据未就绪"
    elif failed:
        title = "策略优化部分失败" if finished else "策略优化失败"
    elif states and states <= {"no_symbols", "no_data", "not_run"}:
        title = "策略优化未执行"
    elif aggregate_status == "partial":
        title = "策略优化部分失败" if finished else "策略优化失败"
    elif finished and states & {"no_symbols", "no_data", "not_run"}:
        title = "策略优化部分完成"
    else:
        title = "策略优化完成"
    return f"{title} · {group_name}" if group_name else title


def build_optimizer_summary(
    report,
    group_name: str = "",
    full_report: dict | None = None,
    include_charts: bool = True,
) -> str:
    """将 OptimizationReport + 完整回测报告格式化为 HTML 摘要。

    full_report 非空时追加：日回报测指标 / 季末持仓 / 敏感性 / 波动率。
    include_charts=False 时跳过图片（飞书 markdown 不支持 <img>）。
    """
    if hasattr(report, "strategy_name") and isinstance(
        getattr(report, "groups", None), dict
    ):
        return _build_optimizer_run_summary(report)

    lines = ["<b>策略优化完成</b>"]
    if group_name:
        label = {
            "a_share": "A股",
            "hk": "港股",
            "us": "美股",
            "non_a_share": "非A股",
        }.get(group_name, group_name)
        lines.append(f"分组: {label}")
    lines.append(
        f"耗时: {report.elapsed_seconds:.0f}s  |  评估: {report.iterations} 策略"
    )

    if not report.top_strategies and not full_report:
        return "\n".join(lines) + "\n(无有效策略)"

    if report.top_strategies:
        lines.append("")
        for i, t in enumerate(report.top_strategies[:3]):
            lines.append(f"<b>策略 #{i + 1}</b>")
            lines.append(
                f"  收益 {t.test_return:+.1f}%  回撤 {t.test_drawdown:.1f}%  "
                f"夏普 {t.sharpe:.2f}  交易 {t.trade_count}笔"
            )
            lines.append(f"  <pre>{t.strategy_description}</pre>")
        lines.append(f"完整结果: data/optimizer/{report.report_id}.yaml")

    if full_report:
        lines.append("")
        lines.append("<b>━━━ 日回报测 (近9月验证期) ━━━</b>")
        rc = "#27ae60" if full_report["total_return"] >= 0 else "#c0392b"
        lines.append(
            f'收益 <span style="color:{rc}">{full_report["total_return"]:+.1f}%</span>'
            f" (超额 {full_report.get('excess_return') or full_report['total_return']:+.1f}%)"
            f"  回撤 {full_report['dd']:.1f}%"
            f"  夏普 {full_report['sharpe']:.2f}"
            f"  交易 {full_report['trades']}笔"
            f"  仓位 {full_report['position']:.0f}%"
        )

        # 参数摘要
        params = full_report.get("params", {})
        if params:
            pi = []
            for lbl in (
                "adx_pct",
                "rsi_pct",
                "deviation_pct",
                "vol_ratio_pct",
                "ma200_dev_pct",
            ):
                tau = params.get(f"{lbl}_tau")
                w = params.get(f"{lbl}_w")
                if tau is not None and w is not None:
                    pi.append(f"τ={tau:.2f} w={w:.2f}")
            lines.append(f"  信号: {' | '.join(pi)}")
            lines.append(
                f"  买阈 τ_buy={params.get('tau_buy', '?'):.2f}"
                f"  卖阈 τ_sell={params.get('tau_sell', '?'):.2f}"
            )
            execution = full_report.get("execution", {}) or {}
            if execution:
                lines.append("  " + _optimizer_execution_description(execution))

        # 季末持仓
        qh = full_report.get("quarterly", [])
        if qh:
            last_q = qh[-1]
            qpos = last_q.get("positions", [])
            if qpos:
                pos_str = ", ".join(
                    f"{p['code']} {p['shares']:.0f}股@{p['price']:.2f}"
                    f"({p.get('pnl_pct', 0):+.0f}%)"
                    for p in qpos[:8]
                )
                lines.append(
                    f"  期末持仓(Q{last_q.get('quarter', '?')} "
                    f"仓位{last_q.get('pos_pct', 0):.0f}%): {pos_str}"
                )

        # 参数敏感性
        sens = full_report.get("sensitivity")
        if sens:
            lines.append("")
            lines.append("<b>━━━ 参数敏感性 (10版随机扰动) ━━━</b>")
            lines.append(
                f"  最优版 {sens['base_ret']:+.1f}%"
                f" → 最差版 {sens['worst_ret']:+.1f}%"
                f" (降幅 {sens['drop_pct']:.1f}pp)"
            )
            lines.append(
                f"  10版范围 [{sens['ret_range'][0]:+.1f}%, "
                f"{sens['ret_range'][1]:+.1f}%]"
            )

        # 跨天波动率
        vol = full_report.get("volatility")
        if vol:
            lines.append("")
            lines.append("<b>━━━ 跨天波动率 (近5交易日) ━━━</b>")
            lines.append(
                f"  最低 {vol['min']:+.1f}% / 最高 {vol['max']:+.1f}%"
                f"  (波幅 {vol['range']:.1f}pp)"
            )

        # 周K 蜡烛图（邮件用 CID，飞书跳过）
        ohlc = full_report.get("weekly_ohlc")
        if include_charts and full_report.get("candlestick_png"):
            lines.append("<br><b>━━━ 周K NAV 蜡烛图 ━━━</b>")
            lines.append(
                '<img src="cid:candlestick" style="max-width:100%;'
                'border:1px solid #444;border-radius:4px;margin:8px 0">'
            )
        elif ohlc:
            # 飞书/无图时：贴周K收益摘要
            labels = ohlc.get("labels", [])
            closes = ohlc.get("close", [])
            if labels and closes:
                n = len(labels)
                lines.append("<br><b>━━━ 周K收益 ━━━</b>")
                summary_parts = []
                for k in range(max(0, n - 8), n):
                    summary_parts.append(f"{labels[k]}: {closes[k]:.0f}")
                lines.append("  " + " | ".join(summary_parts))

    return "<br>".join(lines)


def _build_optimizer_run_summary(report) -> str:
    """Render a channel-neutral optimizer summary covering all three markets."""
    labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
    status_labels = {
        "completed": "完成",
        "no_symbols": "无可搜参标的",
        "no_data": "无可用数据",
        "no_candidates": "未找到有效候选",
        "failed": "\u6267\u884c\u5931\u8d25",
        "interrupted": "\u5f02\u5e38\u4e2d\u6b62",
        "not_run": "未执行",
    }
    lines = [f"<b>{optimizer_notification_title(report)}</b>"]
    lines.append(
        "每个市场独立搜索、独立验收、独立激活；未激活市场不会读取其他市场策略。"
    )
    failure_reason = str(getattr(report, "failure_reason", "") or "")
    if failure_reason:
        lines.append(f"<b>\u5931\u8d25\u539f\u56e0:</b> {_html_escape(failure_reason)}")
    if report.elapsed_seconds > 0:
        lines.append(
            f"开始时间: {report.timestamp} | 耗时: {report.elapsed_seconds:.0f}s"
        )
    else:
        lines.append(f"活动策略版本: {report.timestamp}（补发重建）")
    if report.activated:
        lines.append("<b>已发布:</b> 此运行已成为日报、简报和回测的当前告警策略。")
    elif getattr(report, "candidate", False):
        run_id = getattr(report, "run_id", "")
        completed_groups = [
            group
            for group, item in report.groups.items()
            if getattr(item, "status", "") == "completed"
        ]
        activation_scope = (
            f" --group {completed_groups[0]}" if len(completed_groups) == 1 else ""
        )
        lines.append(
            "<b>候选已保存:</b> 当前活动策略未切换。"
            + (
                f"人工确认后运行 python main.py --activate-run {run_id}"
                f"{activation_scope}。"
                if run_id
                else "通过验收后可人工激活。"
            )
        )
    else:
        lines.append("<b>未切换:</b> 本次候选未激活；当前生产指针保持不变。")

    for group in ("a_share", "hk", "us"):
        item = report.groups.get(group)
        if item is None:
            lines.append(f"<br><b>{labels[group]}</b>: 未执行")
            continue
        status = status_labels.get(item.status, item.status)
        lines.append(f"<br><b>{labels[group]}</b>: {status}")
        item_strategy = getattr(item, "strategy_name", "")
        if item_strategy:
            lines.append(f"候选策略: <code>{item_strategy}</code>")
        solver_id = str(getattr(item, "solver_id", "") or "")
        gate_profile = str(getattr(item, "gate_profile", "") or "")
        config_hash = str(getattr(item, "market_config_hash", "") or "")
        item_run_id = str(
            getattr(item, "run_id", "")
            or (getattr(report, "run_ids_by_group", {}) or {}).get(group, "")
        )
        if solver_id:
            lines.append(f"Solver: <code>{solver_id}</code>")
        if gate_profile:
            lines.append(f"Gate: <code>{gate_profile}</code>")
        for field_name, label in (
            ("walk_forward_profile", "Walk-Forward"),
            ("execution_profile", "Execution"),
            ("benchmark_profile", "Benchmark"),
        ):
            profile = str(getattr(item, field_name, "") or "")
            if profile:
                lines.append(f"{label}: <code>{profile}</code>")
        if config_hash:
            lines.append(f"配置指纹: <code>{config_hash}</code>")
        if item_run_id:
            lines.append(f"独立 run_id: <code>{item_run_id}</code>")
        diagnostics = getattr(item, "ranking_diagnostics", {}) or {}
        readiness_issues = diagnostics.get("data_readiness_issues", []) or []
        data_reasons: dict[str, list[str]] = {}
        for issue in readiness_issues:
            if not isinstance(issue, dict) or not issue.get("reason"):
                continue
            code = str(issue.get("code", ""))
            reason = str(issue["reason"])
            category = issue.get("category")
            if category and category != "missing_data":
                reason = f"[{category}] {reason}"
            reasons = data_reasons.setdefault(code, [])
            if reason not in reasons:
                reasons.append(reason)
        for exclusion in diagnostics.get("data_exclusions", []) or []:
            code, separator, reason = str(exclusion).partition(": ")
            reasons = data_reasons.setdefault(code if separator else "", [])
            reason = reason or code
            if reason not in reasons:
                reasons.append(reason)
        if data_reasons:
            lines.append("数据准备问题（含基准）:")
            lines.extend(
                f"{_html_escape(code)}: {_html_escape('; '.join(reasons))}"
                for code, reasons in data_reasons.items()
            )
        if item.status != "completed":
            evaluated = int(getattr(item, "evaluated_count", 0) or 0)
            if item.status in {"no_symbols", "no_data"} and not evaluated:
                lines.append("候选评估 0 次；请先恢复数据就绪。")
            if evaluated:
                prefix = (
                    "搜索已完成" if item.status == "no_candidates" else "中止前已完成"
                )
                lines.append(f"{prefix} {evaluated:,} 次候选评估。")
            diagnostics = getattr(item, "ranking_diagnostics", {}) or {}
            error = diagnostics.get("error") or diagnostics.get("configuration_error")
            if error:
                lines.append(f"原因: {_html_escape(error)}")
            failures = diagnostics.get("hard_gate_failure_counts", {})
            if failures:
                lines.append("硬 Gate 淘汰次数（同一候选可触发多项）:")
                lines.extend(
                    f"{_html_escape(rule)}: {int(count):,}"
                    for rule, count in failures.items()
                )
            continue
        evaluated = getattr(item, "evaluated_count", 0)
        survivors = getattr(item, "survivor_count", 0) or item.candidate_count
        ranking_windows = getattr(item, "ranking_window_count", 0)
        validation_windows = getattr(item, "validation_window_count", 0)
        purged_windows = getattr(item, "purged_window_count", 0)
        if evaluated:
            lines.append(
                f"配置搜索评估 {evaluated:,} 次 | 最终入围 {survivors:,} 个 | "
                f"最优 WF 分数 {item.wf_score:+.3f}"
            )
        else:
            lines.append(
                f"最终入围 {survivors:,} 个 | 最优 WF 分数 {item.wf_score:+.3f}"
            )
        if ranking_windows:
            lines.append(
                f"WF 排名仅使用 {ranking_windows} 个历史窗口；"
                f"{validation_windows} 个日报验证窗口未参与排序、约束或敏感性。"
            )
        if purged_windows:
            lines.append(
                f"严格验证隔离额外剔除了 {purged_windows} 个与验证期重叠的 WF 窗口。"
            )
        ranking_diagnostics = getattr(item, "ranking_diagnostics", None) or {}
        if ranking_diagnostics:
            lines.append(
                "Ranking absolute return: "
                f"weighted {float(ranking_diagnostics.get('weighted_strategy_return', 0.0)):+.2f}% | "
                f"positive windows {int(ranking_diagnostics.get('positive_return_windows', 0))}/"
                f"{int(ranking_diagnostics.get('ranking_window_count', 0))}"
            )
        sensitivity = getattr(item, "sensitivity", None) or {}
        if sensitivity:
            lines.append(
                "历史窗口参数敏感性 "
                f"({sensitivity.get('sample_count', 0)} 版扰动): "
                f"基准 {sensitivity.get('base_score', 0.0):+.3f} | "
                f"最差 {sensitivity.get('worst_score', 0.0):+.3f} | "
                f"降幅 {sensitivity.get('drop', 0.0):+.3f}"
            )
        if sensitivity and "selection_score" in sensitivity:
            lines.append(
                f"Robust selection score: {float(sensitivity['selection_score']):+.3f}"
            )
        if item.params:
            params = ", ".join(f"{key}={value}" for key, value in item.params.items())
            lines.append(f"<code>{params}</code>")
        execution = getattr(item, "execution", None) or {}
        if execution:
            lines.append(_optimizer_execution_description(execution))
        validation = getattr(item, "validation", None) or {}
        if validation:
            lines.append("<b>验证期回测</b>")
            lines.append(
                f"收益 {validation.get('total_return', 0.0):+.1f}% | "
                f"超额 {validation.get('excess_return', 0.0):+.1f}% | "
                f"回撤 {validation.get('max_drawdown', 0.0):.1f}% | "
                f"夏普 {validation.get('sharpe_ratio', 0.0):.2f} | "
                f"交易 {validation.get('trade_count', 0)} 笔 | "
                f"平均现金 {validation.get('avg_cash_pct', 0.0):.0f}%"
            )
            lines.append(
                "现金分红（税前 / 税额 / 税后）: "
                f"{validation.get('gross_dividend_cash', 0.0):,.2f} / "
                f"{validation.get('dividend_tax_cost', 0.0):,.2f} / "
                f"{validation.get('net_dividend_cash', 0.0):,.2f}"
            )
            ledger = render_in_kind_ledger(
                validation.get("in_kind_positions", []) or [],
                validation.get("in_kind_cashflows", []) or [],
            )
            if ledger:
                lines.append(ledger)
            benchmark_returns = validation.get("benchmark_returns", {}) or {}
            if benchmark_returns:
                baselines = " | ".join(
                    f"{_benchmark_label(code)} {value:+.1f}%"
                    for code, value in benchmark_returns.items()
                )
                lines.append(f"三基线收益 / 策略超额: {baselines}")
            win_rates = validation.get("benchmark_win_rates", {}) or {}
            if win_rates:
                wins = " | ".join(
                    f"{_benchmark_label(code)} {value:.1f}%"
                    for code, value in win_rates.items()
                )
                lines.append(f"验证期胜率: {wins}")
            positions = validation.get("final_holdings", []) or []
            if positions:
                holding_text = ", ".join(
                    f"{pos.get('code', '?')} {pos.get('shares', 0):.0f}股"
                    for pos in positions
                )
                lines.append(
                    f"期末持仓 (资产 {validation.get('final_asset', 0):,.0f}): "
                    f"{holding_text}"
                )
            else:
                lines.append("期末持仓: 空仓")
            weekly = validation.get("weekly_ohlc", {}) or {}
            weekly_labels = weekly.get("labels", [])
            closes = weekly.get("close", [])
            if weekly_labels and closes:
                start = max(0, len(weekly_labels) - 8)
                changes = " | ".join(
                    f"{weekly_labels[index]} {closes[index]:.0f}"
                    for index in range(start, min(len(weekly_labels), len(closes)))
                )
                lines.append(f"验证期 NAV 周线: {changes}")
    return "<br>".join(lines)


def get_brief_dataframe(session):
    """Use the same eligible quote rows on every scheduled delivery channel."""
    frame = session.get_all_dataframe()
    codes = getattr(session, "brief_active_codes", None)
    if isinstance(codes, (tuple, list, set, frozenset)):
        return frame.loc[frame["stock_code"].astype(str).isin(codes)].copy()
    return frame


def _event_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


EVENT_CATEGORIES = (
    "重组并购与重大事项",
    "财报业绩",
    "分红派息",
    "股权融资与解禁",
    "例行披露",
    "其他",
)
EVENT_MARKETS = {"a_share": "A股", "hk": "港股", "us": "美股", "sg": "新加坡"}


def _event_category(item):
    """Specific filing forms win over incidental words such as annual/dividend."""
    title = str(item.get("title") or "").lower()
    source = " ".join(str(item.get(key) or "") for key in ("category", "type")).lower()
    routine_forms = (
        ("翌日披露", ("翌日披露", "next day disclosure", "next-day disclosure")),
        ("证券变动月报表", ("月報表", "月报表", "monthly return")),
        ("业绩说明会", ("业绩说明会", "業績說明會", "earnings call")),
        (
            "投票结果",
            ("投票表决结果", "投票表決結果", "投票结果", "投票結果", "voting results"),
        ),
    )
    for label, words in routine_forms:
        if any(word in title or word in source for word in words):
            return 4, label
    rules = (
        (
            0,
            (
                "重组",
                "重組",
                "收购",
                "收購",
                "并购",
                "併購",
                "合并",
                "合併",
                "控制权",
                "控制權",
                "重大资产",
                "重大資產",
                "关联交易",
                "關連交易",
                "merger",
                "acquisition",
                "restructuring",
                "material agreement",
            ),
        ),
        (
            2,
            (
                "分红",
                "分紅",
                "派息",
                "股息",
                "权益分派",
                "權益分派",
                "利润分配",
                "利潤分配",
                "dividend",
                "distribution",
            ),
        ),
        (
            3,
            (
                "定增",
                "增发",
                "增發",
                "配售",
                "解禁",
                "回购",
                "回購",
                "增持",
                "减持",
                "減持",
                "repurchase",
                "buyback",
                "placing",
                "lock-up",
            ),
        ),
        (
            1,
            (
                "季度报告",
                "季度報告",
                "季度财务",
                "季度財務",
                "年度报告",
                "年度報告",
                "年报",
                "年報",
                "季报",
                "季報",
                "中期报告",
                "中期報告",
                "半年度报告",
                "半年度報告",
                "季度业绩",
                "季度業績",
                "年度业绩",
                "年度業績",
                "中期业绩",
                "中期業績",
                "業績公佈",
                "业绩预告",
                "業績預告",
                "业绩快报",
                "業績快報",
                "盈利预警",
                "盈利預警",
                "10-k",
                "10-q",
                "20-f",
                "40-f",
                "n-csr",
                "n-csrs",
                "quarterly results",
                "annual results",
                "annual report",
                "full yearly results",
                "interim results",
                "earnings",
                "financial statements",
                "产销快报",
                "產銷快報",
                "经营数据",
                "經營數據",
                "operating results",
            ),
        ),
        (
            4,
            (
                "通函",
                "投票表决",
                "投票表決",
                "投票结果",
                "投票結果",
                "会议通知",
                "會議通告",
                "业绩说明会",
                "業績說明會",
                "circular",
                "notice of meeting",
                "voting results",
            ),
        ),
    )
    # Reliable provider categories take precedence; generic categories fall back to titles.
    for text in (source, title):
        for category, words in rules:
            if any(word in text for word in words):
                return category, EVENT_CATEGORIES[category]
    return 5, "其他"


def _event_date(value):
    if value is None or not isinstance(value, (str, date, pd.Timestamp)):
        return None
    try:
        day = pd.Timestamp(value)
        if pd.isna(day):
            return None
        if day.tzinfo is not None:
            day = day.tz_convert("Asia/Shanghai")
        return day.date()
    except (ValueError, TypeError, OverflowError):
        return None


def group_daily_event_rows(events):
    """Stable category/market/instrument groups for every presentation channel."""
    for _, rows in groupby(
        events,
        key=lambda row: (row["priority"], row["category"], row["market"], row["code"]),
    ):
        rows = list(rows)
        first = rows[0]
        yield {
            "category": first["category_label"],
            "market": first["market_label"],
            "code": first["code"],
            "name": first["name"],
            "rows": rows,
        }


def build_daily_event_rows(
    announcements,
    placements,
    stock_data,
    dividend_events=None,
    *,
    as_of=None,
    config=None,
):
    """Share the complete, ordered event feed across report channels."""
    events = []
    row_map = (
        {str(row.get("stock_code", "")): row for _, row in stock_data.iterrows()}
        if stock_data is not None and hasattr(stock_data, "iterrows")
        else {}
    )
    for item in dividend_events or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "")
        if status not in {"未除权", "已除权未派息"}:
            continue
        code = str(item.get("code", "") or "").strip()
        ex_date = str(item.get("ex_date", "") or "")
        payment_date = str(item.get("payment_date", "") or "")
        cash_per_share = _event_number(item.get("cash_per_share"))
        currency = str(item.get("currency", "") or "").strip().upper()
        name = row_map.get(code, {}).get("stock_name", "")
        amount = (
            f"每股/份现金 {currency + ' ' if currency else ''}{cash_per_share:.4f}"
            if cash_per_share is not None
            else "金额待公告"
        )
        schedule = f"除权 {ex_date or '待确认'}"
        if payment_date:
            schedule += f" · 派息 {payment_date}"
        events.append(
            {
                "date": ex_date,
                "code": code,
                "kind": status,
                "title": f"{name} · {amount} · {schedule}",
                "url": "",
                "category": 2,
                "priority": 0 if status == "已除权未派息" else 1,
            }
        )
    months = (
        ((config or {}).get("report") or {})
        .get("events", {})
        .get("announcement_lookback_months", 2)
    )
    if type(months) is not int or not 1 <= months <= 24:
        raise ValueError("公告展示期限要求1–24个自然月")
    report_date = (
        _event_date(as_of)
        if as_of is not None
        else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )
    if report_date is None:
        raise ValueError("报表日期无效")
    cutoff = (pd.Timestamp(report_date) - pd.DateOffset(months=months)).date()
    seen = set()
    for code, items in (announcements or {}).items():
        for item in items or []:
            if isinstance(item, dict):
                published = _event_date(item.get("published_at")) or _event_date(
                    item.get("date")
                )
                date = published.isoformat() if published else ""
                if published is not None and published < cutoff:
                    continue
                title = str(item.get("title", "未命名公告") or "未命名公告")
                url = html_lib.unescape(_safe_html_url(item.get("url")))
                category, subtype = _event_category(item)
            else:
                date, title, url = "", str(item), ""
                category, subtype = 5, "其他"
            identity = (str(code), date, url or title)
            if identity in seen:
                continue
            seen.add(identity)
            events.append(
                {
                    "date": date,
                    "code": str(code),
                    "kind": subtype,
                    "title": title,
                    "url": url,
                    "category": category,
                    "priority": 3,
                }
            )
    for code, item in (placements or {}).items():
        if not isinstance(item, dict):
            continue
        unlock_date = str(item.get("unlock_date", "") or "")
        name = row_map.get(str(code), {}).get("stock_name", "")
        issue_num = _event_number(item.get("issue_num"))
        issue_price = _event_number(item.get("issue_price"))
        pct_of_total = _event_number(item.get("pct_of_total"))
        issue_num_text = _fmt(
            issue_num / 1e8 if issue_num is not None else None,
            "亿股",
            ".2f",
        )
        events.append(
            {
                "date": unlock_date,
                "code": str(code),
                "kind": "未解禁定增",
                "title": (
                    f"{name} · {issue_num_text} · "
                    f"占总股本 {_fmt(pct_of_total, '%', '.2f')} · "
                    f"定增价 {_fmt(issue_price, '元', '.2f')} · "
                    f"解禁 {unlock_date or '待确认'}"
                ),
                "url": "",
                "category": 3,
                "priority": 2,
            }
        )
    for event in events:
        event["category_label"] = (
            event["kind"]
            if event["priority"] < 3
            else EVENT_CATEGORIES[event["category"]]
        )
        event["market"] = _detect_fine_group(event["code"])
        event["market_label"] = EVENT_MARKETS[event["market"]]
        event["name"] = str(
            row_map.get(event["code"], {}).get("stock_name") or event["code"]
        )
    events.sort(
        key=lambda row: (
            row["priority"],
            row["category"],
            tuple(EVENT_MARKETS).index(row["market"]),
            row["code"],
            not bool(_event_date(row["date"])),
            -(_event_date(row["date"]).toordinal() if _event_date(row["date"]) else 0),
            row["title"],
            row["url"],
        )
    )
    combined, routine = [], {}
    for row in events:
        if row["category"] == 4 and row["kind"] in {"翌日披露", "证券变动月报表"}:
            key = (row["market"], row["code"], row["kind"])
            if key in routine:
                routine[key]["members"].append(row)
                continue
            row = {**row, "members": [row]}
            routine[key] = row
        combined.append(row)
    return combined


def build_daily_events_text(session, stock_data=None) -> str:
    """Use the same event rows as email, without silently truncating the feed."""
    rows = build_daily_event_rows(
        getattr(session, "announcements", None),
        getattr(session, "placements", None),
        stock_data,
        getattr(session, "dividend_events", None),
        as_of=getattr(session, "created_at", None),
        config=getattr(session, "config", None),
    )
    lines = []
    last = None
    for group in group_daily_event_rows(rows):
        heading = (group["category"], group["market"])
        if heading != last:
            lines.append(_html_escape(" · ".join(heading)))
            last = heading
        lines.append(f"{_html_escape(group['code'])} {_html_escape(group['name'])}")
        for row in group["rows"]:
            members = row.get("members") or [row]
            if len(members) > 1:
                lines.append(f"{_html_escape(row['kind'])} {len(members)}份")
            for member in members:
                lines.append(
                    f"{_html_escape(member['date'] or '日期待确认')} · {_html_escape(member['title'])}"
                    + (
                        f"\n原文：{_html_escape(member['url'])}"
                        if member.get("url")
                        else ""
                    )
                )
    return "\n".join(lines)


def report_data_readiness(session) -> dict:
    """One readiness projection for execution receipts and report renderers."""
    config = getattr(session, "config", {})
    if not isinstance(config, dict) or "stocks" not in config:
        return {"status": "not_applicable"}
    expected = {
        str(stock.get("code", "") if isinstance(stock, dict) else stock).strip()
        for stock in config.get("stocks", [])
    }
    data = getattr(session, "stocks_data", {}) or {}
    available = set(data) if isinstance(data, dict) else set()
    missing = sorted(expected - available)
    errors = getattr(session, "errors", []) or []
    analysis_issues = {}
    return {
        "status": "not_ready" if missing or errors or analysis_issues else "ready",
        "expected_codes": sorted(expected),
        "available_codes": sorted(available),
        "missing_codes": missing,
        "error_count": len(errors),
        "analysis_issues": analysis_issues,
    }
