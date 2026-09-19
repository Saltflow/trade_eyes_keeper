"""Daily-report PDF generation (xelatex LaTeX template -> bytes).

Extracted verbatim from EmailNotifier._generate_daily_pdf (528 lines). It was
a method only because of three helpers it borrowed from the notifier; those are
now injected explicitly, so this module owns one job.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .report_builders import _alert_value, _benchmark_label

logger = logging.getLogger(__name__)


def generate_daily_pdf(
    session,
    alert_stocks,
    signal_scan,
    backtest,
    stock_data,
    *,
    chart_deviation_timeline,
    pick_best_anchor,
    get_server_info,
) -> bytes | None:
    """生成日报 PDF（xelatex 编译 LaTeX 模板），返回 bytes"""
    import tempfile
    import subprocess
    import os
    import re

    try:
        # 1. 图表 PNG
        chart_buf = chart_deviation_timeline(
            signal_scan, backtest, base64=False
        )
        chart_path = None
        if chart_buf and not isinstance(chart_buf, str):
            fd, chart_path = tempfile.mkstemp(suffix=".png", prefix="chart_")
            with open(fd, "wb") as f:
                data = (
                    chart_buf
                    if isinstance(chart_buf, bytes)
                    else chart_buf.getvalue()
                    if hasattr(chart_buf, "getvalue")
                    else chart_buf
                )
                f.write(data)
            chart_section = f"\\includegraphics[width=\\textwidth]{{{chart_path}}}"
        else:
            chart_section = "{\\color{gray}\\small 今日无图表数据}"

        # 2. KPI
        sa = getattr(signal_scan, "alerts", None) or [] if signal_scan else []

        # ── LaTeX 转义函数 (必须在使用前定义) ──
        def _esc(s):
            """LaTeX 转义"""
            return (
                str(s)
                .replace("&", "\\&")
                .replace("%", "\\%")
                .replace("#", "\\#")
                .replace("$", "\\$")
                .replace("_", "\\_")
            )

        buy_count = len(sa)
        evaluation_reports = getattr(session, "evaluation_reports", {}) or {}
        bt_a = backtest.get("a_share", {}) if backtest else {}
        bt_n = backtest.get("non_a_share", {}) if backtest else {}

        def _report_return(group: str):
            report = evaluation_reports.get(group)
            if report is not None:
                return report.total_return
            return None

        ta = _report_return("a_share")
        th = _report_return("hk")
        tu = _report_return("us")
        # 兼容尚未迁移的旧 session.backtest，但日报主链路只读
        # EvaluationReport，因此 PDF 和邮件/IM 使用同一份三市场结果。
        if not evaluation_reports:
            ta = bt_a.get("total_return")
            tu = bt_n.get("total_return")
        has_backtest = any(value is not None for value in (ta, th, tu))
        kpi_buy = str(buy_count)
        kpi_a = f"{ta:+.1f}\\%" if ta is not None else "—"
        kpi_h = f"{th:+.1f}\\%" if th is not None else "—"
        kpi_u = f"{tu:+.1f}\\%" if tu is not None else "—"
        if not has_backtest:
            kpi_s = "⚙ 优化未运行"
        elif any(value > 0 for value in (ta, th, tu) if value is not None):
            kpi_s = "✓ 策略有效"
        else:
            kpi_s = "✗ 策略无效"
        kpi_color_a = "green" if ta is not None and ta > 0 else "red"
        kpi_color_h = "green" if th is not None and th > 0 else "red"
        kpi_color_u = "green" if tu is not None and tu > 0 else "red"
        kpi_color_s = "green" if buy_count > 0 else "red"

        # 3. 触发信号
        trigger_lines = []
        for a in sa:
            code = _alert_value(a, "stock_code", "?")
            label = _esc(_alert_value(a, "rule_label", "?"))
            cv = _esc(str(_alert_value(a, "current_value", "—")))
            trigger_lines.append(f"\\textbf{{{_esc(code)}}} & {label} & {cv} \\\\")
        trigger_section = (
            "\\begin{tabular}{lll}\n"
            + "\\textbf{标的} & \\textbf{信号规则} & \\textbf{当前值}\\\\\n"
            + "\n".join(trigger_lines)
            + "\n\\end{tabular}"
            if trigger_lines
            else "{\\color{gray}\\small 今日无触发信号}"
        )

        # 4. 表: 合并 indicator_snapshot + fundamentals
        snapshot = (
            getattr(signal_scan, "indicator_snapshot", {}) if signal_scan else {}
        )
        consensus = getattr(signal_scan, "consensus", None) if signal_scan else None
        cons_inds = (
            consensus.consensus_indicators if consensus else ["deviation", "rsi"]
        )
        fundamentals = {}
        if stock_data is not None and hasattr(stock_data, "iterrows"):
            for _, row in stock_data.iterrows():
                code = str(row.get("stock_code", ""))
                if not code:
                    continue
                pe = row.get("pe_ratio")
                pb = row.get("pb_ratio")
                dy = row.get("dividend_yield")
                anchors = {}
                for anchor_key in ("ma60", "wma20", "wma30", "wma50"):
                    anchor_value = row.get(anchor_key)
                    if anchor_value is not None and not pd.isna(anchor_value):
                        anchors[anchor_key] = float(anchor_value)
                close = row.get("close")
                best_anchor = (
                    pick_best_anchor(float(close), anchors)
                    if close is not None and not pd.isna(close) and anchors
                    else None
                )
                anchor_name, anchor_value, anchor_deviation = (
                    best_anchor if best_anchor else ("—", None, None)
                )
                fundamentals[code] = {
                    "close": f"{close:.2f}"
                    if close is not None and not pd.isna(close)
                    else "—",
                    "anchor_name": _esc(anchor_name),
                    "anchor_value": f"{anchor_value:.2f}"
                    if anchor_value is not None
                    else "—",
                    "anchor_deviation": f"{anchor_deviation:+.1f}\\%"
                    if anchor_deviation is not None
                    else "—",
                    "pe": f"{pe:.1f}"
                    if pe is not None and not pd.isna(pe)
                    else "—",
                    "pb": f"{pb:.2f}"
                    if pb is not None and not pd.isna(pb)
                    else "—",
                    "dy": f"{dy:.2f}"
                    if dy is not None and not pd.isna(dy)
                    else "—",
                }

        header_cols = ["标的", "收盘", "锚点", "锚值", "偏离\\%"] + cons_inds + ["息\\%", "PE", "PB", "信号"]
        # 列格式: l for 标的, c for signal, r for numbers
        col_fmt = "l" + "r" * (len(cons_inds) + 7) + "c"
        table_rows = ""
        alert_codes = set(getattr(a, "stock_code", "") for a in sa)

        from src.markets import _detect_fine_group

        report_codes = list(fundamentals)
        a_codes = sorted(
            [c for c in report_codes if _detect_fine_group(c) == "a_share"],
            key=lambda c: abs(snapshot.get(c, {}).get("deviation", 0) or 0),
            reverse=True,
        )
        for code in a_codes:
            vals = snapshot.get(code, {})
            sig = "●" if code in alert_codes else ""
            fund = fundamentals.get(code, {})
            cells = [
                _esc(code),
                fund.get("close", "—"),
                fund.get("anchor_name", "—"),
                fund.get("anchor_value", "—"),
                fund.get("anchor_deviation", "—"),
            ]
            for ind in cons_inds:
                v = vals.get(ind)  # None = 缺失, 0 = 真实零
                if v is None:
                    cells.append("—")
                elif ind == "deviation":
                    cells.append(f"{v * 100:+.1f}\\%")
                else:
                    cells.append(f"{v:.2f}")
            cells.append(fund.get("dy", "—"))
            cells.append(fund.get("pe", "—"))
            cells.append(fund.get("pb", "—"))
            cells.append(sig)
            row_color = "\\rowcolor{bg!30}" if sig else ""
            table_rows += f"{row_color}{' & '.join(cells)} \\\\\n"

        nona_codes = sorted(
            [c for c in report_codes if c not in a_codes],
            key=lambda c: abs(snapshot.get(c, {}).get("deviation", 0) or 0),
            reverse=True,
        )
        if nona_codes:
            table_rows += f"\\multicolumn{{{len(header_cols)}}}{{l}}{{\\color{{navy}}\\textbf{{境外 · {len(nona_codes)} 只}}}}\\\\\n"
        for code in nona_codes:
            vals = snapshot.get(code, {})
            sig = "●" if code in alert_codes else ""
            fund = fundamentals.get(code, {})
            cells = [
                _esc(code),
                fund.get("close", "—"),
                fund.get("anchor_name", "—"),
                fund.get("anchor_value", "—"),
                fund.get("anchor_deviation", "—"),
            ]
            for ind in cons_inds:
                v = vals.get(ind)  # None = 缺失, 0 = 真实零
                if v is None:
                    cells.append("—")
                elif ind == "deviation":
                    cells.append(f"{v * 100:+.1f}\\%")
                else:
                    cells.append(f"{v:.2f}")
            cells.append(fund.get("dy", "—"))
            cells.append(fund.get("pe", "—"))
            cells.append(fund.get("pb", "—"))
            cells.append(sig)
            row_color = "\\rowcolor{bg!30}" if sig else ""
            table_rows += f"{row_color}{' & '.join(cells)} \\\\\n"

        table_section = (
            "\\scriptsize\n"
            "\\rowcolors{2}{white}{stripe}\n"
            "\\resizebox{\\textwidth}{!}{%\n"
            f"\\begin{{tabular}}{{{col_fmt}}}\n"
            "\\toprule\n" + " & ".join(header_cols) + " \\\\\n"
            "\\midrule\n" + table_rows + "\\bottomrule\n"
            "\\end{tabular}}"
        )

        # 5. 脚注
        buy_sigs = consensus.buy_signal_counts if consensus else {}
        strat_note = " · ".join(list(buy_sigs.keys())[:4]) if buy_sigs else "—"
        if evaluation_reports:
            metric_parts = []
            labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
            for group, label in labels.items():
                report = evaluation_reports.get(group)
                if report is None:
                    continue
                base_parts = []
                for bn, bv in (report.benchmark_returns or {}).items():
                    rate = (report.benchmark_win_rates or {}).get(bn)
                    rate_text = f"，胜率 {rate:.0f}\\%" if rate is not None else ""
                    base_parts.append(f"{_esc(_benchmark_label(bn))} {bv:+.1f}\\%{rate_text}")
                metric_parts.append(
                    f"{label} 收益 {report.total_return:+.1f}\\%"
                    + (f"（{'；'.join(base_parts)}）" if base_parts else "")
                )
            bt_text = "\\quad ".join(metric_parts) or "策略未运行"
        else:
            bt_text = f"A股策略超额 {ta:+.1f}\\%" if ta is not None else "A股策略未运行"
            if bt_a.get("benchmarks"):
                for bn, bv in bt_a["benchmarks"].items():
                    beat = "✓" if (ta is not None and ta > bv) else "✗"
                    bt_text += f"\\quad vs {bn} {bv:+.1f}\\% {beat}"

        # 统一 EvaluationReport 附录：PDF 不再二次计算周线或持仓。
        evaluation_report_section = ""
        if evaluation_reports:
            report_lines = [
                "\\newpage",
                "\\section*{\\color{navy}统一策略评估}",
                "{\\footnotesize 本节直接渲染与 HTML、飞书和 Telegram "
                "相同的 EvaluationReport。}",
            ]
            group_labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
            for group, label in group_labels.items():
                report = evaluation_reports.get(group)
                if report is None:
                    continue
                report_lines.extend([
                    f"\\subsection*{{{label}}}",
                    "\\begin{tabular}{lr@{\\quad}lr@{\\quad}lr}",
                    "\\toprule",
                    f"收益 & {report.total_return:+.1f}\\% & "
                    f"最大回撤 & {report.max_drawdown:.1f}\\% & "
                    f"夏普 & {report.sharpe_ratio:.2f} \\",
                    f"交易数 & {int(report.trade_count)} & "
                    f"期末资产 & {report.final_asset:,.2f} & "
                    f"仓位 & {report.final_position_pct:.1f}\\% \\",
                    f"分红税前 & {report.gross_dividend_cash:,.2f} & "
                    f"预扣税额 & {report.dividend_tax_cost:,.2f} & "
                    f"分红税后 & {report.net_dividend_cash:,.2f} \\",
                    "\\bottomrule",
                    "\\end{tabular}",
                ])

                weekly = report.weekly_nav_ohlc or {}
                labels = weekly.get("labels", [])
                opens = weekly.get("open", [])
                highs = weekly.get("high", [])
                lows = weekly.get("low", [])
                closes = weekly.get("close", [])
                count = min(
                    len(labels), len(opens), len(highs), len(lows), len(closes)
                )
                if count:
                    report_lines.extend([
                        "\\paragraph{周 NAV K线（自然周 OHLC）}",
                        "\\begin{longtable}{lrrrr}",
                        "\\toprule 自然周 & Open & High & Low & Close \\\\ \\midrule",
                    ])
                    for index in range(count):
                        report_lines.append(
                            f"{_esc(labels[index])} & {opens[index]:.2f} & "
                            f"{highs[index]:.2f} & {lows[index]:.2f} & "
                            f"{closes[index]:.2f} \\"
                        )
                    report_lines.extend(["\\bottomrule", "\\end{longtable}"])

                quarters = report.quarterly_holdings or []
                if quarters:
                    report_lines.extend([
                        "\\paragraph{季末持仓（自然季度最后有效交易日）}",
                        "\\begin{longtable}{llllrrr}",
                        "\\toprule 季度 & 日期 & 代码 & 股数 & 成本 & 价格 & 市值 "
                        "\\\\ \\midrule",
                    ])
                    for snapshot in quarters:
                        positions = snapshot.get("positions", []) or []
                        if not positions:
                            report_lines.append(
                                f"{_esc(snapshot.get('quarter', '?'))} & "
                                f"{_esc(snapshot.get('date', '-'))} & 空仓 & -- & -- & -- & "
                                f"{float(snapshot.get('nav', 0)):,.2f} \\"
                            )
                        for position in positions:
                            report_lines.append(
                                f"{_esc(snapshot.get('quarter', '?'))} & "
                                f"{_esc(snapshot.get('date', '-'))} & "
                                f"{_esc(position.get('code', '?'))} & "
                                f"{float(position.get('shares', 0)):.0f} & "
                                f"{float(position.get('cost', 0)):.2f} & "
                                f"{float(position.get('price', 0)):.2f} & "
                                f"{float(position.get('value', 0)):,.2f} \\"
                            )
                    report_lines.extend(["\\bottomrule", "\\end{longtable}"])

                final_holdings = report.final_holdings or []
                report_lines.extend([
                    "\\paragraph{期末持仓}",
                    "\\begin{tabular}{lrrrrrr}",
                    "\\toprule 代码 & 股数 & 成本 & 期末价 & 市值 & 权重 & 盈亏 \\\\ "
                    "\\midrule",
                ])
                if final_holdings:
                    for position in final_holdings:
                        report_lines.append(
                            f"{_esc(position.get('code', '?'))} & "
                            f"{float(position.get('shares', 0)):.0f} & "
                            f"{float(position.get('cost', 0)):.2f} & "
                            f"{float(position.get('price', 0)):.2f} & "
                            f"{float(position.get('value', 0)):,.2f} & "
                            f"{float(position.get('weight', 0)):.1f}\\% & "
                            f"{float(position.get('pnl', 0)):+,.2f} \\"
                        )
                else:
                    report_lines.append("空仓 & -- & -- & -- & -- & -- & -- \\")
                report_lines.extend(["\\bottomrule", "\\end{tabular}"])
            # Python string literals above emit one trailing backslash; LaTeX
            # table rows require two. Normalize only row-ending backslashes.
            row_break = chr(92)
            report_lines = [
                line + row_break if line.endswith(row_break) else line
                for line in report_lines
            ]
            evaluation_report_section = "\n".join(report_lines)

        # 6. 附录
        md_path = (
            Path(__file__).parent.parent / "templates" / "appendix_methodology.md"
        )
        appendix_section = ""
        if md_path.exists():
            md_text = md_path.read_text(encoding="utf-8")
            # 简单的 MD → LaTeX 转换
            latex_lines = []
            in_list = False
            in_table = False
            in_display_math = False
            for line in md_text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("$$"):
                    # $$ 单独成行 = 显示公式块开关；同行 $$...$$ = 单行公式
                    if stripped.endswith("$$") and len(stripped) > 4:
                        formula = stripped.strip("$").strip()
                        latex_lines.append(f"\\[{formula}\\]")
                    elif in_display_math:
                        latex_lines.append("\\]")
                        in_display_math = False
                    else:
                        latex_lines.append("\\[")
                        in_display_math = True
                elif in_display_math:
                    # 公式块内的行原样保留在数学环境内，禁止按普通段落转义
                    latex_lines.append(stripped)
                elif stripped.startswith("# "):
                    if in_list:
                        latex_lines.append("\\end{itemize}")
                        in_list = False
                    if in_table:
                        latex_lines.append("\\end{tabular}")
                        in_table = False
                    latex_lines.append(f"\\section*{{{_esc(stripped[2:])}}}")
                elif stripped.startswith("## "):
                    if in_list:
                        latex_lines.append("\\end{itemize}")
                        in_list = False
                    latex_lines.append(f"\\subsection*{{{_esc(stripped[3:])}}}")
                elif stripped.startswith("- "):
                    if not in_list:
                        latex_lines.append("\\begin{itemize}")
                        in_list = True
                    item = stripped[2:]
                    item = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", item)
                    latex_lines.append(f"  \\item {item}")
                elif stripped.startswith("---"):
                    latex_lines.append("\\vspace{4pt}\\hrule\\vspace{4pt}")
                elif stripped.startswith("|"):
                    if not in_table:
                        cols = stripped.count("|") - 1
                        latex_lines.append(f"\\begin{{tabular}}{{{'l' * cols}}}")
                        latex_lines.append("\\toprule")
                        in_table = True
                    else:
                        cells = [c.strip() for c in stripped.split("|")[1:-1]]
                        latex_lines.append(" & ".join(cells) + " \\\\")
                elif in_table and not stripped.startswith("|"):
                    latex_lines.append("\\bottomrule")
                    latex_lines.append("\\end{tabular}")
                    in_table = False
                elif stripped:
                    item = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", stripped)
                    item = re.sub(r"\$(.+?)\$", r"$\1$", item)
                    latex_lines.append(f"{item}\n")
            if in_list:
                latex_lines.append("\\end{itemize}")
            if in_table:
                latex_lines.append("\\end{tabular}")
            if in_display_math:
                latex_lines.append("\\]")
            appendix_section = "\n".join(latex_lines)

        # 7. 渲染模板
        tex_path = Path(__file__).parent.parent / "templates" / "report_daily.tex"
        template = tex_path.read_text(encoding="utf-8").replace("\r\n", "\n")

        info = get_server_info()
        html = template.replace(
            "\\VAR{report_date}", datetime.now().strftime("%Y-%m-%d %A")
        )
        html = html.replace("\\VAR{server_hostname}", info.get("hostname", ""))
        html = html.replace("\\VAR{kpi_buy}", kpi_buy)
        html = html.replace("\\VAR{kpi_a}", kpi_a)
        html = html.replace("\\VAR{kpi_h}", kpi_h)
        html = html.replace("\\VAR{kpi_u}", kpi_u)
        html = html.replace("\\VAR{kpi_s}", kpi_s)
        html = html.replace("\\VAR{kpi_color_a}", kpi_color_a)
        html = html.replace("\\VAR{kpi_color_h}", kpi_color_h)
        html = html.replace("\\VAR{kpi_color_u}", kpi_color_u)
        html = html.replace("\\VAR{kpi_color_s}", kpi_color_s)
        html = html.replace("\\VAR{chart_section}", chart_section)
        html = html.replace("\\VAR{trigger_section}", trigger_section)
        html = html.replace("\\VAR{table_section}", table_section)
        html = html.replace("\\VAR{strategy_note}", strat_note)
        html = html.replace("\\VAR{backtest_note}", bt_text)
        html = html.replace(
            "\\VAR{evaluation_report_section}", evaluation_report_section
        )
        html = html.replace("\\VAR{appendix_section}", appendix_section)

        # 8. xelatex 编译
        with tempfile.TemporaryDirectory() as tmpdir:
            tex_file = Path(tmpdir) / "report.tex"
            tex_file.write_text(html, encoding="utf-8")

            for _ in range(2):  # 两次编译（交叉引用）
                result = subprocess.run(
                    [
                        "xelatex",
                        "-interaction=nonstopmode",
                        "-output-directory",
                        tmpdir,
                        str(tex_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    log_file = Path(tmpdir) / "report.log"
                    log_tail = ""
                    if log_file.exists():
                        lines = log_file.read_text(errors="replace").split("\n")
                        # 找第一个 "!" 错误行
                        for i, line_text in enumerate(lines):
                            if line_text.startswith("!"):
                                log_tail = "\\n".join(lines[max(0, i - 1) : i + 5])
                                break
                    logger.warning(
                        "xelatex 编译问题: %s", log_tail or result.stderr[-200:]
                    )

            pdf_file = Path(tmpdir) / "report.pdf"
            if pdf_file.exists():
                pdf_bytes = pdf_file.read_bytes()
                logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
                return pdf_bytes
            else:
                log_file = Path(tmpdir) / "report.log"
                if log_file.exists():
                    logger.error(
                        "xelatex 日志: %s",
                        log_file.read_text(errors="replace")[-500:],
                    )
                logger.error("xelatex 未产出 PDF")
                return None

    except Exception as e:
        logger.error("生成日报 PDF 失败: %s", e)
        return None
    finally:
        if chart_path and os.path.exists(chart_path):
            os.unlink(chart_path)
