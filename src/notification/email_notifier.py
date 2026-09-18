"""
邮件通知模块
发送股票提醒邮件
"""

import html as html_lib
import logging
import smtplib
import ssl
import numpy as np
import pandas as pd
import socket
import platform
import subprocess
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email import policy
from datetime import datetime
from pathlib import Path

from .chart_generator import (
    generate_combined_chart,
    generate_portfolio_overview_chart,
)
from .base import BaseNotifier
try:
    from ..markets import _detect_fine_group
except ImportError:  # pragma: no cover - legacy top-level ``notification`` imports
    from markets import _detect_fine_group

logger = logging.getLogger(__name__)


# Report builders moved to their own module. Re-exported here so the existing
# import sites (feishu_notifier, telegram_notifier, health_server, 13 test
# files) keep working unchanged.
from .daily_pdf import generate_daily_pdf
from .report_builders import (  # noqa: F401
    SIGNAL_NAMES,
    _alert_value,
    _benchmark_label,
    _build_optimizer_run_summary,
    _build_signal_label_map,
    _fmt,
    _format_benchmark_comparison,
    _html_escape,
    _readable_signal,
    _safe_html_url,
    _signed_text_metric,
    build_brief_entries,
    build_optimizer_summary,
    build_strategy_suggestions,
    build_strategy_text_summary,
    optimizer_notification_title,
    pick_best_anchor,
)


class EmailNotifier(BaseNotifier):
    """邮件通知器"""

    def __init__(self, config):
        """
        初始化邮件通知器

        Args:
            config: 配置字典
        """
        self.config = config
        self.email_config = config.get("email", {})

        # SMTP服务器配置 (从 config.yaml 读取)
        self.smtp_server = self.email_config.get("smtp_server", "")
        self.smtp_port = self.email_config.get("smtp_port", 465)
        self.sender_email = self.email_config.get("sender_email", "")
        self.sender_password = self.email_config.get("sender_password", "")
        self.receiver_email = self.email_config.get("receiver_email", "")
        self.enable_tls = self.email_config.get("enable_tls", False)
        self.enable_ssl = self.email_config.get("enable_ssl", True)

        # 邮件副本配置：使用项目根目录的绝对路径避免工作目录漂移
        archive_dir_config = self.email_config.get("archive_dir", "data/email_archive")
        archive_dir_path = Path(archive_dir_config)
        if not archive_dir_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent.parent
            archive_dir_path = (project_root / archive_dir_config).resolve()

        self.email_archive_dir = archive_dir_path
        self.email_archive_dir.mkdir(parents=True, exist_ok=True)
        logger.info("邮件副本目录已初始化为: %s", self.email_archive_dir)

        if not self.sender_email or not self.sender_password or not self.receiver_email:
            logger.warning("邮件配置不完整，邮件通知功能可能无法正常工作")

        # 报告 token 超时配置
        try:
            timeout = config.get("health_server", {}).get(
                "report_token_timeout_minutes", 30
            )
            from ..health_server.core.global_instances import set_report_token_timeout

            set_report_token_timeout(timeout)
        except Exception as e:
            logger.debug(f"设置 token 超时失败: {e}")

    def send_from_session(self, session):
        """
        从Session读取数据并发送邮件（新数据流）

        Args:
            session: SessionContext对象
        """
        try:
            # 从Session读取所有数据
            alert_stocks = session.get_alerts_as_dicts()
            stock_data = session.get_all_dataframe()
            announcements = session.announcements
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 生成走势图表（PNG bytes + CID 内嵌，兼容 Gmail）
            _, chart_png_bytes = generate_combined_chart(
                historical_data=historical_data,
                alerts=alert_stocks,
                stock_data=stock_data,
                trading_days=60,
            )

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果（唯一收口）
            evaluation_reports = getattr(session, "evaluation_reports", None)

            # 日报只嵌入一张归一化三市场总览图，避免三张纵向大图淹没正文。
            portfolio_chart_dict = None
            if evaluation_reports:
                try:
                    overview_png = generate_portfolio_overview_chart(
                        evaluation_reports
                    )
                    portfolio_chart_dict = (
                        {"overview": overview_png} if overview_png else None
                    )
                    n_charts = len(portfolio_chart_dict) if portfolio_chart_dict else 0
                    logger.info(
                        f"投资组合图表生成: {n_charts}张"
                        if n_charts
                        else "投资组合图表跳过"
                    )
                except Exception as e:
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session,
                    alert_stocks,
                    signal_scan,
                    backtest,
                    stock_data,
                )
                if pdf_bytes:
                    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
            except Exception as e:
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（精简正文）
            body = self._build_email_body(
                alert_stocks,
                stock_data,
                announcements,
                historical_data=historical_data,
                chart_png_bytes=chart_png_bytes,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                evaluation_reports=evaluation_reports,
                daily_mode=True,
                placements=getattr(session, "placements", None),
                instrument_audit=getattr(session, "instrument_audit", None),
            )


            # 发送邮件（PDF 作为附件）
            self._send_email(
                subject,
                body,
                chart_png_bytes=chart_png_bytes,
                portfolio_chart_dict=portfolio_chart_dict,
                pdf_bytes=pdf_bytes,
            )

            logger.info(
                f"邮件任务完成 ({self.receiver_email}) "
                f"(来自Session: {len(alert_stocks)}个警报)"
            )

        except Exception as e:
            logger.error(f"从Session发送邮件失败: {e}")

    def send_daily_report_from_session(self, session):
        """
        从Session读取数据并发送每日报告（新数据流）

        Args:
            session: SessionContext对象
        """
        try:
            # 从Session读取所有数据
            stock_data = session.get_all_dataframe()
            announcements = session.announcements
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果（唯一收口）
            evaluation_reports = getattr(session, "evaluation_reports", None)

            # 无告警日报同样使用单张三市场组合总览图。
            portfolio_chart_dict = None
            if evaluation_reports:
                try:
                    overview_png = generate_portfolio_overview_chart(
                        evaluation_reports
                    )
                    portfolio_chart_dict = (
                        {"overview": overview_png} if overview_png else None
                    )
                except Exception as e:
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session,
                    [],
                    signal_scan,
                    backtest,
                    stock_data,
                )
            except Exception as e:
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（使用空警报列表）
            body = self._build_email_body(
                [],
                stock_data,
                announcements,
                historical_data=historical_data,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                evaluation_reports=evaluation_reports,
                daily_mode=True,
                placements=getattr(session, "placements", None),
                instrument_audit=getattr(session, "instrument_audit", None),
            )


            # 发送邮件
            self._send_email(
                subject,
                body,
                portfolio_chart_dict=portfolio_chart_dict,
                pdf_bytes=pdf_bytes,
            )

            logger.info(f"每日报告邮件任务完成 ({self.receiver_email}) (来自Session)")

        except Exception as e:
            logger.error(f"从Session发送每日报告邮件失败: {e}")

    def _build_strategy_alert_section(
        self, signal_scan, alert_stocks, stock_data
    ) -> str:
        """构建策略信号报警 + 共识指标快照"""
        if not signal_scan:
            return ""

        consensus = getattr(signal_scan, "consensus", None)
        alerts = getattr(signal_scan, "alerts", None) or []
        snapshot = getattr(signal_scan, "indicator_snapshot", None) or {}
        warnings = getattr(signal_scan, "divergence_warnings", None) or []

        # 区分边界报警和策略报警
        boundary_codes = set()
        if alert_stocks:
            for a in alert_stocks:
                if isinstance(a, dict) and a.get("type") != "strategy":
                    boundary_codes.add(a.get("stock_code", ""))
                elif not isinstance(a, dict):
                    boundary_codes.add(getattr(a, "stock_code", ""))

        strategy_codes = set()
        for a in alerts:
            code = a.stock_code if hasattr(a, "stock_code") else a.get("stock_code", "")
            strategy_codes.add(code)

        html = "<h3>策略信号扫描</h3>\n"

        # 报警部分
        if alerts:
            html += f"<p><strong>策略报警 ({len(alerts)} 条 / {len(strategy_codes)} 只标的)</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:100%;margin:10px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            html += '<tr style="background:#34495e;color:#fff"><th>标的</th><th>规则</th><th>条件</th><th>当前值</th><th>来源</th></tr>\n'
            for a in alerts[:12]:
                code = (
                    a.stock_code
                    if hasattr(a, "stock_code")
                    else a.get("stock_code", "?")
                )
                label = (
                    a.rule_label
                    if hasattr(a, "rule_label")
                    else a.get("rule_label", "?")
                )
                cond = (
                    a.condition_str
                    if hasattr(a, "condition_str")
                    else a.get("condition", "?")
                )
                cv = (
                    a.current_value
                    if hasattr(a, "current_value")
                    else a.get("current_value", "-")
                )
                rank = (
                    a.strategy_rank
                    if hasattr(a, "strategy_rank")
                    else a.get("strategy_rank", "?")
                )
                html += (
                    f'<tr style="background:#e8f6f3">'
                    f"<td>[策略] {code}</td>"
                    f"<td>{label}</td>"
                    f'<td style="font-size:11px">{cond[:60]}</td>'
                    f"<td>{cv}</td>"
                    f"<td>Rank {rank}</td>"
                    f"</tr>\n"
                )
            html += "</table>\n"
        else:
            html += "<p>策略信号: 无触发</p>\n"

        # 共识指标快照
        if consensus and consensus.consensus_indicators and snapshot:
            ind_cols = consensus.consensus_indicators
            html += f"<p><strong>共识指标快照 ({len(snapshot)} 只)</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:auto;margin:10px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            header = '<tr style="background:#34495e;color:#fff"><th>标的</th>'
            for ind in ind_cols:
                label = {
                    "rsi": "RSI",
                    "vol_ratio": "量比",
                    "boll_pct_b": "布林%B",
                    "adx": "ADX",
                    "macd_hist": "MACD柱",
                    "deviation": "偏差%",
                    "atr": "ATR",
                }.get(ind, ind)
                header += f"<th>{label}</th>"
            header += "</tr>\n"
            html += header

            # 按标的在报警池内优先排序
            consensus_stocks = set(consensus.consensus_stocks or [])
            sorted_codes = sorted(
                snapshot.keys(),
                key=lambda c: (
                    0 if c in strategy_codes else 1 if c in consensus_stocks else 2
                ),
            )

            for code in sorted_codes[:20]:
                vals = snapshot.get(code, {})
                html += f"<tr><td>{code}</td>"
                for ind in ind_cols:
                    v = vals.get(ind)
                    if v is not None:
                        if ind == "deviation":
                            html += f"<td>{v * 100:.1f}%</td>"
                        else:
                            html += f"<td>{v:.2f}</td>"
                    else:
                        html += "<td>-</td>"
                html += "</tr>\n"
            html += "</table>\n"

        # 背离警告
        if warnings:
            html += "<p style='color:#c44e52;font-size:12px'><strong>⚠ 背离警告:</strong><br>"
            html += "<br>".join(warnings)
            html += "<br><em>建议以共识信号为准，不盲从单一名次</em></p>\n"

        return html

    def _build_backtest_section(self, backtest) -> str:
        """构建回测结果 HTML"""
        if not backtest:
            return (
                "<h3>历史回测</h3>\n"
                '<p style="color:#888;font-size:13px">优化策略未生成（每日 02:00 自动运行 <code>main.py --optimize</code>）。'
                "首次部署后可手动触发: <code>python main.py --optimize</code></p>\n"
            )

        html = "<h3>历史回测</h3>\n"
        group_labels = {"a_share": "A股", "non_a_share": "境外"}

        for group, bt in backtest.items():
            if not bt:
                continue
            label = group_labels.get(group, group)
            html += f"<p><strong>{label} — 基于最新优化策略 (Rank {bt.get('strategy_rank', '?')})</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            html += '<tr style="background:#34495e;color:#fff"><th>指标</th><th>全期</th><th>Ranking</th><th>Purged</th><th>Holdout</th></tr>\n'

            phases = bt.get("phase_metrics", {})
            ranking_phase = phases.get("ranking", phases.get("observe"))
            purged_phase = phases.get("purged", phases.get("deploy"))
            holdout_phase = phases.get("holdout", phases.get("test"))
            # 全期总收益含资金注入，无直接可比超额 → 各阶段超额见分列
            _excess_all = "—"
            total_excess_col = _excess_all
            dd = f"{bt.get('max_drawdown', 0):.1f}%"
            sp = f"{bt.get('sharpe', 0):.3f}"
            trades = str(bt.get("trade_count", 0))

            def _pval(p_obj, key):
                if p_obj is None:
                    return "-"
                v = getattr(p_obj, key, 0)
                if key in ("total_return", "excess_return"):
                    return f"{v:+.1f}%"
                if key == "max_drawdown":
                    return f"{v:.1f}%"
                if key == "sharpe_ratio":
                    return f"{v:.3f}"
                return str(v)

            html += (
                f"<tr><td>超额收益</td><td>{total_excess_col}</td>"
                f"<td>{_pval(ranking_phase, 'excess_return')}</td>"
                f"<td>{_pval(purged_phase, 'excess_return')}</td>"
                f"<td>{_pval(holdout_phase, 'excess_return')}</td></tr>\n"
            )
            html += (
                f"<tr><td>最大回撤</td><td>{dd}</td>"
                f"<td>{_pval(ranking_phase, 'max_drawdown')}</td>"
                f"<td>{_pval(purged_phase, 'max_drawdown')}</td>"
                f"<td>{_pval(holdout_phase, 'max_drawdown')}</td></tr>\n"
            )
            html += (
                f"<tr><td>Sharpe</td><td>{sp}</td>"
                f"<td>{_pval(ranking_phase, 'sharpe_ratio')}</td>"
                f"<td>{_pval(purged_phase, 'sharpe_ratio')}</td>"
                f"<td>{_pval(holdout_phase, 'sharpe_ratio')}</td></tr>\n"
            )
            html += (
                f"<tr><td>交易次数</td><td>{trades}</td>"
                f"<td>{getattr(ranking_phase, 'trade_count', '-')}</td>"
                f"<td>{getattr(purged_phase, 'trade_count', '-')}</td>"
                f"<td>{getattr(holdout_phase, 'trade_count', '-')}</td></tr>\n"
            )

            # 基准对比
            bm = bt.get("benchmarks", {})
            if bm:
                test_excess = getattr(holdout_phase, "excess_return", 0)
                html += "<tr><td>vs基准</td><td colspan='4'>"
                parts = []
                for name, val in bm.items():
                    beat = "✓" if test_excess > val else "✗"
                    parts.append(f"{name}: {val:+.1f}% {beat}")
                html += " | ".join(parts)
                html += "</td></tr>\n"

            html += "</table>\n"

            stocks = bt.get("stocks", [])
            if stocks:
                html += (
                    f"<p style='font-size:12px;color:#888'>入选标的: "
                    f"{', '.join(stocks[:8])}"
                    f"{' +' + str(len(stocks) - 8) if len(stocks) > 8 else ''}</p>\n"
                )

        return html

    @staticmethod
    def _build_placement_section(placements, stock_data):
        """构建未解禁定增表 HTML。

        列：标的编号 | 名称 | 未解禁定增数额 | 占总股本 | 定增价格 | 解禁时间
        """
        if not placements:
            return ""

        # 从 stock_data 取名称
        name_map = {}
        try:
            if stock_data is not None and hasattr(stock_data, "iterrows"):
                for _, row in stock_data.iterrows():
                    name_map[str(row.get("stock_code", ""))] = row.get("stock_name", "")
        except Exception as exc:
            logger.debug("Unable to build placement stock-name mapping: %s", exc)

        rows = ""
        for code, p in sorted(placements.items()):
            name = name_map.get(code, "")
            issue_num = p.get("issue_num")
            issue_price = p.get("issue_price")
            pct = p.get("pct_of_total")
            unlock = p.get("unlock_date") or "—"
            # 数额格式化：亿股
            if issue_num:
                num_str = f"{issue_num / 1e8:.2f}亿股"
            else:
                num_str = "—"
            price_str = f"{issue_price:.2f}元" if issue_price else "—"
            pct_str = f"{pct:.2f}%" if pct is not None else "—"
            rows += (
                f"<tr>"
                f'<td style="padding:8px">{code}</td>'
                f'<td style="padding:8px">{name}</td>'
                f'<td style="text-align:right;padding:8px">{num_str}</td>'
                f'<td style="text-align:right;padding:8px">{pct_str}</td>'
                f'<td style="text-align:right;padding:8px">{price_str}</td>'
                f'<td style="text-align:right;padding:8px">{unlock}</td>'
                f"</tr>\n"
            )

        return (
            '<tr><td style="padding:16px 24px 4px;border-bottom:2px solid #ecf0f1">'
            '<div style="font-size:15px;font-weight:600;color:#2c3e50">'
            "未解禁定增</div></td></tr>\n"
            '<tr><td style="padding:8px 24px 16px">'
            '<table role="presentation" style="width:100%;border-collapse:collapse;'
            'font-size:12px;table-layout:fixed;word-break:break-all" cellpadding="6" cellspacing="0" border="0">\n'
            '<thead><tr style="background:#34495e;color:#fff">'
            '<th style="text-align:left;padding:8px">代码</th>'
            '<th style="text-align:left;padding:8px">名称</th>'
            '<th style="text-align:right;padding:8px">定增数额</th>'
            '<th style="text-align:right;padding:8px">占总股本</th>'
            '<th style="text-align:right;padding:8px">定增价格</th>'
            '<th style="text-align:right;padding:8px">解禁时间</th>'
            "</tr></thead>\n<tbody>\n"
            f"{rows}"
            "</tbody></table></td></tr>\n"
        )

    def _build_strategy_results_section(
        self,
        evaluation_reports,
        signal_scan=None,
    ) -> str:
        """构建搜参策略结果段（策略指标 + 今日信号 + 季末持仓）。

        唯一数据源：session.evaluation_reports（EvaluationReport dict）。

        Args:
            evaluation_reports: {group: EvaluationReport}
            signal_scan: SignalScanner.scan() 结果 (今日触发的策略信号)
        """
        reports = evaluation_reports or {}
        if not reports:
            return ""

        lines: list[str] = []
        lines.append(
            '<div style="margin-top:30px;border-top:2px solid #2c3e50;padding-top:16px">'
        )

        # 策略引擎名 + 评估日期
        first_r = next(iter(reports.values()), None)
        engine_name = getattr(first_r, "engine_name", "?") if first_r else "?"
        engine_label = getattr(first_r, "strategy_label", "?") if first_r else "?"
        ts = (
            (getattr(first_r, "timestamp", "") or "")[:16].replace("T", " ")
            if first_r else ""
        )

        header = f"搜参策略结果 — {engine_label} ({engine_name})"
        if ts:
            header += f"  {ts}"
        lines.append(f'<h3 style="color:#2c3e50">{header}</h3>')

        # 今日信号
        alerts = getattr(signal_scan, "alerts", None) or [] if signal_scan else []
        if alerts:
            lines.append(
                '<div style="margin:10px 0;padding:10px;'
                'border:1px solid #d4e6f1;border-radius:5px;background:#ebf5fb">'
            )
            signal_codes = set()
            for a in alerts:
                code = getattr(a, "stock_code", None) or (
                    a.get("stock_code", "?") if isinstance(a, dict) else "?"
                )
                signal_codes.add(code)
            lines.append(
                f'<p style="margin:0 0 6px;font-weight:600;color:#1a5276">'
                f"今日信号 ({len(alerts)} 条 / {len(signal_codes)} 只标的)</p>"
            )
            lines.append(
                '<table style="font-size:12px;border-collapse:collapse;'
                'width:100%;table-layout:fixed;word-break:break-all">'
                '<tr style="background:#2c3e50;color:#fff">'
                '<th style="width:22%">标的</th><th style="width:33%">规则</th>'
                '<th style="width:45%">当前值</th></tr>'
            )
            map_a = _build_signal_label_map("a_share")
            map_hk = _build_signal_label_map("hk") or _build_signal_label_map(
                "non_a_share"
            )
            map_us = _build_signal_label_map("us") or _build_signal_label_map(
                "non_a_share"
            )
            for a in alerts[:30]:
                code = getattr(a, "stock_code", None) or (
                    a.get("stock_code", "?") if isinstance(a, dict) else "?"
                )
                raw = getattr(a, "rule_label", None) or (
                    a.get("rule_label", "?") if isinstance(a, dict) else "?"
                )
                readable = _readable_signal(code, raw, map_a, map_hk, map_us)
                cv = getattr(a, "current_value", None) or (
                    a.get("current_value", "-") if isinstance(a, dict) else "-"
                )
                lines.append(
                    f"<tr><td>{code}</td><td>{readable}</td><td>{cv}</td></tr>"
                )
            lines.append("</table></div>")
        else:
            lines.append('<p style="color:#888;margin:8px 0">今日信号: 无触发</p>')

        # 各组结果
        group_labels = {
            "a_share": "A股组合",
            "hk": "港股组合",
            "us": "美股组合",
        }
        for group_key, group_label in group_labels.items():
            r = reports.get(group_key)
            if r is None:
                continue
            lines.append(
                f'<h4 style="color:#333;border-left:4px solid #2196f3;'
                f'padding-left:10px;margin:16px 0 8px">{group_label}</h4>'
            )

            qh = r.quarterly_holdings or []
            cash_pcts = [(100 - q.get("pos_pct", 0)) for q in qh if q.get("nav", 0) > 0]
            avg_cash = sum(cash_pcts) / len(cash_pcts) if cash_pcts else None

            benchmark_parts, win_rate_parts = _format_benchmark_comparison(r)

            trc = "#27ae60" if r.total_return >= 0 else "#c0392b"
            rc = "#27ae60" if r.excess_return >= 0 else "#c0392b"
            ts = (r.timestamp or "")[:16].replace("T", " ")
            comp = r.composition or []

            summary = (
                '<div style="border:1px solid #ddd;padding:10px;'
                'margin:6px 0;border-radius:5px;background:#f0f7ff">'
                f"<b>评估期收益 "
                f'<span style="color:{trc}">{r.total_return:+.1f}%</span>'
                f' (超额<span style="color:{rc}">{r.excess_return:+.1f}%</span>)</b> &nbsp; '
                f"最大回撤 {r.max_drawdown:.1f}% &nbsp; "
                f"夏普 {r.sharpe_ratio:.2f} &nbsp; "
                f"交易 {int(r.trade_count)}笔 &nbsp; "
            )
            if avg_cash is not None:
                summary += f"平均现金仓位 {avg_cash:.0f}% &nbsp; "
            if benchmark_parts:
                summary += (
                    "<br><b>三基线收益 / 策略超额</b>: "
                    + " | ".join(benchmark_parts)
                )
            if win_rate_parts:
                summary += (
                    "<br><b>验证期胜率</b>(任意日买入持有到期跑赢): "
                    + " | ".join(win_rate_parts)
                )
            selection = getattr(r, "selection_diagnostics", None) or {}
            ranking = selection.get("ranking_diagnostics", {})
            sensitivity = selection.get("sensitivity", {})
            if ranking:
                summary += (
                    "<br><b>排名筛选（Ranking 窗口）</b>: "
                    f"加权绝对收益 {float(ranking.get('weighted_strategy_return', 0.0)):+.2f}% | "
                    f"正收益窗口 {int(ranking.get('positive_return_windows', 0))}/"
                    f"{int(ranking.get('ranking_window_count', 0))} | "
                    f"基础 WF {float(selection.get('wf_score') or 0.0):+.3f}"
                )
            if sensitivity:
                final_score = selection.get("selection_score")
                if final_score is None:
                    final_score = sensitivity.get("selection_score")
                summary += (
                    "<br><b>稳健性</b>: "
                    f"最差分 {float(sensitivity.get('worst_score', 0.0)):+.3f} | "
                    f"降幅 {float(sensitivity.get('drop', 0.0)):+.3f} | "
                    f"最终选择分 {float(final_score or 0.0):+.3f}"
                )
            summary += (
                '<br><span style="color:#888;font-size:11px">'
                f"评估时间 {ts} · 成分: "
                f"{', '.join(comp) if comp else '—'}</span></div>"
            )
            warming = getattr(r, "warming_codes", None) or []
            if warming:
                summary += (
                    '<div style="font-size:11px;color:#888;margin:4px 0">'
                    f"预热中（暂不交易）: {', '.join(warming)}</div>"
                )
            lines.append(summary)

            final_holdings = getattr(r, "final_holdings", None) or []
            lines.append(
                '<p style="font-size:11px;margin:8px 0 4px">'
                f"<b>期末资产</b> {float(getattr(r, 'final_asset', 0)):,.2f} &nbsp; "
                f"<b>现金</b> {float(getattr(r, 'final_cash', 0)):,.2f} &nbsp; "
                f"<b>持仓市值</b> {float(getattr(r, 'final_holdings_value', 0)):,.2f} &nbsp; "
                f"<b>仓位</b> {float(getattr(r, 'final_position_pct', 0)):.1f}%</p>"
            )
            lines.append(
                '<p style="font-size:11px;margin:8px 0 4px"><b>期末持仓</b></p>'
            )
            if final_holdings:
                lines.append(
                    '<table style="font-size:11px;border-collapse:collapse;'
                    'width:100%;table-layout:fixed;word-break:break-all">'
                    '<tr style="background:#2c3e50;color:#fff">'
                    '<th>代码</th><th>股数</th><th>成本</th><th>期末价</th>'
                    '<th>市值</th><th>权重</th><th>盈亏</th><th>盈亏%</th></tr>'
                )
                for position in final_holdings:
                    pnl = float(position.get("pnl", 0))
                    color = "#27ae60" if pnl >= 0 else "#c0392b"
                    lines.append(
                        f"<tr><td>{position.get('code', '?')}</td>"
                        f"<td>{float(position.get('shares', 0)):.0f}</td>"
                        f"<td>{float(position.get('cost', 0)):.2f}</td>"
                        f"<td>{float(position.get('price', 0)):.2f}</td>"
                        f"<td>{float(position.get('value', 0)):.2f}</td>"
                        f"<td>{float(position.get('weight', 0)):.1f}%</td>"
                        f'<td style="color:{color}">{pnl:+.2f}</td>'
                        f'<td style="color:{color}">'
                        f"{float(position.get('pnl_pct', 0)):+.1f}%</td></tr>"
                    )
                lines.append("</table>")
            else:
                lines.append('<p style="font-size:11px;color:#888">空仓</p>')

            weekly = getattr(r, "weekly_nav_ohlc", None) or {}
            weekly_labels = weekly.get("labels", [])
            weekly_open = weekly.get("open", [])
            weekly_high = weekly.get("high", [])
            weekly_low = weekly.get("low", [])
            weekly_close = weekly.get("close", [])
            weekly_count = min(
                len(weekly_labels), len(weekly_open), len(weekly_high),
                len(weekly_low), len(weekly_close),
            )
            if weekly_count:
                weekly_chart = None
                try:
                    from .chart_generator import generate_candlestick_chart

                    weekly_chart = generate_candlestick_chart({
                        "labels": weekly_labels[:weekly_count],
                        "open": weekly_open[:weekly_count],
                        "high": weekly_high[:weekly_count],
                        "low": weekly_low[:weekly_count],
                        "close": weekly_close[:weekly_count],
                    })
                except Exception as e:
                    logger.warning("周 NAV K线图生成失败: %s", e)
                if weekly_chart:
                    data_uri, _ = weekly_chart
                    lines.append(
                        '<p style="font-size:11px;margin:10px 0 4px">'
                        '<b>周 NAV K线</b></p>'
                        '<div style="text-align:center;margin:6px 0 12px">'
                        f'<img src="{data_uri}" alt="周 NAV K线" '
                        'style="max-width:100%;height:auto;border:1px solid #ddd;'
                        'border-radius:4px" /></div>'
                    )
                else:
                    lines.append(
                        '<p style="font-size:11px;color:#888">'
                        '周 NAV K线：有效自然周不足，暂不绘图</p>'
                    )

            # 季末持仓明细
            if qh:
                lines.append(
                    '<p style="font-size:11px;margin:10px 0 4px">'
                    '<b>季末持仓（自然季度最后有效交易日）</b></p>'
                )
                lines.append(
                    '<table style="font-size:11px;border-collapse:collapse;'
                    'width:100%;margin-top:6px;table-layout:fixed;word-break:break-all">'
                    '<tr style="background:#34495e;color:#fff">'
                    "<th>季度</th><th>日期</th><th>代码</th><th>持股</th>"
                    "<th>成本</th><th>现价</th><th>市值</th>"
                    "<th>盈亏</th><th>盈亏%</th></tr>"
                )
                for q in qh:
                    qn = q["quarter"]
                    qdate = q.get("date", "-")
                    qcs = q["cash"]
                    qp = q["pos_pct"]
                    qnv = q["nav"]
                    qpos = q.get("positions", [])
                    if not qpos:
                        lines.append(
                            f"<tr><td>{qn}</td><td>{qdate}</td>"
                            f"<td colspan=7>空仓 (nav={qnv:.0f})</td></tr>"
                        )
                    for pos in qpos:
                        code = pos["code"]
                        sh = pos["shares"]
                        cb = pos["cost"]
                        px = pos["price"]
                        vl = pos["value"]
                        pn = pos["pnl"]
                        pp = pos["pnl_pct"]
                        color = "#27ae60" if pn >= 0 else "#c0392b"
                        lines.append(
                            f"<tr><td>{qn}</td><td>{qdate}</td><td>{code}</td>"
                            f"<td>{sh:.0f}股</td>"
                            f"<td>{cb:.2f}</td><td>{px:.2f}</td>"
                            f"<td>{vl:.0f}</td>"
                            f'<td style="color:{color}">{pn:+.0f}</td>'
                            f'<td style="color:{color}">{pp:+.1f}%</td></tr>'
                        )
                    if qpos:
                        lines.append(
                            f"<tr><td>{qn}</td><td>{qdate}</td>"
                            f"<td colspan=4>现金: {qcs:.0f}</td>"
                            f"<td colspan=3>仓位: {qp:.0f}%</td></tr>"
                        )
                lines.append("</table>")

        lines.append("</div>")
        return "<br>".join(lines)


    @staticmethod
    def _daily_get(item, key, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _daily_metric(value, unit="", precision=2, default="—") -> str:
        if value is None:
            return default
        try:
            if pd.isna(value):
                return default
        except (TypeError, ValueError):
            pass
        try:
            return f"{float(value):,.{precision}f}{unit}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _daily_row_map(stock_data) -> dict[str, object]:
        if stock_data is None or not hasattr(stock_data, "iterrows"):
            return {}
        rows = {}
        for _, row in stock_data.iterrows():
            code = row.get("stock_code", "")
            if code is not None:
                rows[str(code)] = row
        return rows

    def _daily_alert_codes(self, alert_stocks, signal_scan=None) -> set[str]:
        codes = set()
        for alert in alert_stocks or []:
            code = _alert_value(alert, "stock_code", "")
            if code:
                codes.add(str(code))
        for alert in (getattr(signal_scan, "alerts", None) or []) if signal_scan else []:
            code = _alert_value(alert, "stock_code", "")
            if code:
                codes.add(str(code))
        return codes

    def _daily_anchor(self, row):
        close = row.get("close")
        anchors = {}
        for key in ("ma60", "wma20", "wma30", "wma50"):
            value = row.get(key)
            try:
                if value is not None and not pd.isna(value) and float(value) > 0:
                    anchors[key] = float(value)
            except (TypeError, ValueError):
                continue
        if not anchors or close is None:
            return "—", None, None
        try:
            best = self._pick_best_anchor(float(close), anchors)
            if best:
                return best
            ma60 = anchors.get("ma60")
            if ma60:
                return "ma60", ma60, (float(close) - ma60) / ma60 * 100
        except (TypeError, ValueError):
            pass
        return "—", None, None

    @staticmethod
    def _daily_number(value):
        """Return a finite float for report rendering, otherwise ``None``."""
        try:
            if value is None or pd.isna(value):
                return None
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if np.isfinite(number) else None

    def _daily_alert_code_sets(self, alert_stocks, signal_scan=None):
        alert_codes = set()
        for alert in alert_stocks or []:
            code = str(_alert_value(alert, "stock_code", "") or "").strip()
            if code:
                alert_codes.add(code)
        signal_codes = set()
        for alert in (getattr(signal_scan, "alerts", None) or []) if signal_scan else []:
            code = str(_alert_value(alert, "stock_code", "") or "").strip()
            if code:
                signal_codes.add(code)
        return alert_codes, signal_codes

    def _daily_watchlist_rows(self, stock_data, alert_stocks, signal_scan=None):
        """Build one compact, market-aware row for every configured symbol."""
        if stock_data is None or not hasattr(stock_data, "iterrows"):
            return []
        fresh_entries = {
            str(entry["code"]): entry
            for entry in build_brief_entries(stock_data, datetime.now())
        }
        alert_codes, signal_codes = self._daily_alert_code_sets(
            alert_stocks, signal_scan
        )
        group_order = {"a_share": 0, "hk": 1, "us": 2}
        rows = []
        for _, source_row in stock_data.iterrows():
            code = str(source_row.get("stock_code", "") or "").strip()
            if not code:
                continue
            entry = fresh_entries.get(code)
            raw_name = source_row.get("stock_name", code)
            name = code if raw_name is None or pd.isna(raw_name) else str(raw_name)
            group = _detect_fine_group(code)
            has_signal = code in signal_codes
            has_alert = code in alert_codes
            priority = 0 if has_signal else 1 if has_alert else 2
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "group": group,
                    "entry": entry,
                    "status": "策略" if has_signal else "预警" if has_alert else "",
                    "priority": priority,
                    "sort_key": entry.get("sort_key", float("inf"))
                    if entry
                    else float("inf"),
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                group_order.get(item["group"], 99),
                item["priority"],
                item["sort_key"],
                item["code"],
            ),
        )

    def _build_daily_summary_section(self, rows, alert_stocks, signal_scan=None):
        alert_codes, signal_codes = self._daily_alert_code_sets(
            alert_stocks, signal_scan
        )
        active_count = sum(1 for item in rows if item["entry"] is not None)
        cells = [
            ("行情就绪", f"{active_count}/{len(rows)}"),
            ("告警标的", len(alert_codes)),
            ("触发条件", len(alert_stocks or [])),
            ("策略信号", len(signal_codes)),
        ]
        html = [
            '<section class="daily-section summary-section">'
            '<div class="section-heading">今日概览</div>'
            '<table role="presentation" class="summary-grid"><tr>'
        ]
        for index, (label, value) in enumerate(cells):
            if index and index % 2 == 0:
                html.append("</tr><tr>")
            html.append(
                '<td class="summary-cell"><div class="summary-label">'
                f'{_html_escape(label)}</div><div class="summary-value">'
                f"{_html_escape(value)}</div></td>"
            )
        html.append("</tr></table></section>")
        return "".join(html)

    def _build_daily_action_section(
        self, alert_stocks, signal_scan, watchlist_rows
    ) -> str:
        """Render a deduplicated, action-first queue before the full matrix."""
        names = {item["code"]: item["name"] for item in watchlist_rows}
        prices = {
            item["code"]: item["entry"].get("close")
            for item in watchlist_rows
            if item["entry"] is not None
        }
        actions = {}

        for alert in alert_stocks or []:
            code = str(_alert_value(alert, "stock_code", "") or "").strip()
            if not code:
                continue
            item = actions.setdefault(code, {"signal": [], "alert": []})
            rule = _alert_value(alert, "condition", None) or _alert_value(
                alert, "rule_label", "价格预警"
            )
            if rule:
                item["alert"].append(str(rule))

        for alert in (getattr(signal_scan, "alerts", None) or []) if signal_scan else []:
            code = str(_alert_value(alert, "stock_code", "") or "").strip()
            if not code:
                continue
            item = actions.setdefault(code, {"signal": [], "alert": []})
            rule = _alert_value(alert, "rule_label", "策略信号")
            if rule:
                item["signal"].append(str(rule))

        if not actions:
            return ""

        ordered = sorted(
            actions.items(),
            key=lambda pair: (
                0 if pair[1]["signal"] else 1,
                str(pair[0]),
            ),
        )
        total = len(ordered)
        lines = [
            '<section class="daily-section action-section">'
            '<div class="section-heading">行动清单'
            f'<span class="section-count">{min(total, 8)}/{total}</span></div>'
        ]
        for code, item in ordered[:8]:
            badges = []
            if item["signal"]:
                badges.append('<span class="status-badge status-signal">策略</span>')
            if item["alert"]:
                badges.append('<span class="status-badge status-alert">预警</span>')
            rules = list(dict.fromkeys(item["signal"] + item["alert"]))[:2]
            price = self._daily_metric(prices.get(code), "", 2)
            lines.append(
                '<div class="action-row"><div class="action-main"><strong>'
                f'{_html_escape(code)} · {_html_escape(names.get(code, code))}'
                f'</strong>{"".join(badges)}</div>'
                f'<div class="action-detail">现价 {_html_escape(price)} · '
                f'{_html_escape("；".join(rules))}</div></div>'
            )
        if total > 8:
            lines.append(
                '<div class="muted-note">其余触发标的已在下方完整行情矩阵中标注。</div>'
            )
        lines.append("</section>")
        return "".join(lines)

    def _build_daily_watchlist_section(self, watchlist_rows) -> str:
        labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
        grouped = {group: [] for group in labels}
        for row in watchlist_rows:
            grouped.setdefault(row["group"], []).append(row)

        sections = [
            '<section class="daily-section watchlist-section">'
            '<div class="section-heading">完整行情矩阵'
            f'<span class="section-count">{len(watchlist_rows)}只</span></div>'
        ]
        for group in ("a_share", "hk", "us"):
            rows = grouped.get(group, [])
            if not rows:
                continue
            sections.append(
                f'<div class="group-heading">{_html_escape(labels[group])} · {len(rows)}只</div>'
                '<table role="presentation" class="watchlist-table">'
                '<thead><tr><th>标的</th><th class="mobile-hide">开盘</th><th>收盘</th>'
                '<th class="mobile-hide">锚点</th><th class="mobile-hide">锚值</th>'
                '<th>偏离</th></tr></thead><tbody>'
            )
            for row in rows:
                entry = row["entry"]
                if entry is None:
                    open_text = close_text = anchor_name = anchor_value = deviation = "—"
                    row_class = " watch-row-stale"
                    status = '<span class="status-badge status-stale">未就绪</span>'
                else:
                    open_text = self._daily_metric(entry.get("open"), "", 2)
                    close_text = self._daily_metric(entry.get("close"), "", 2)
                    anchor_name = str(entry.get("anchor_name", "—"))
                    anchor_value = self._daily_metric(entry.get("anchor_val"), "", 2)
                    deviation = str(entry.get("dev_str", "—"))
                    row_class = " watch-row-action" if row["status"] else ""
                    status = (
                        f'<span class="status-badge status-signal">策略</span>'
                        if row["status"] == "策略"
                        else '<span class="status-badge status-alert">预警</span>'
                        if row["status"] == "预警"
                        else ""
                    )
                deviation_color = (
                    "metric-negative"
                    if deviation.startswith("-")
                    else "metric-positive"
                    if deviation.startswith("+")
                    else ""
                )
                sections.append(
                    f'<tr class="{row_class.strip()}"><td class="watch-instrument">'
                    f'<strong>{_html_escape(row["code"])}</strong><span>'
                    f'{_html_escape(row["name"])}</span>{status}</td>'
                    f'<td class="mobile-hide">{_html_escape(open_text)}</td>'
                    f'<td>{_html_escape(close_text)}</td>'
                    f'<td class="mobile-hide">{_html_escape(anchor_name)}</td>'
                    f'<td class="mobile-hide">{_html_escape(anchor_value)}</td>'
                    f'<td class="{deviation_color}">{_html_escape(deviation)}</td></tr>'
                )
            sections.append("</tbody></table>")
        sections.append("</section>")
        return "".join(sections)

    def _daily_holdout_payload(self, report):
        """Return only a complete, numerically trustworthy 22/16/2/4 holdout."""
        selection = self._daily_get(report, "selection_diagnostics", {}) or {}
        if not isinstance(selection, dict):
            return None
        summary = selection.get("holdout_summary", {}) or {}
        if not isinstance(summary, dict):
            return None
        summary_keys = (
            "return_pct",
            "excess_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
        )
        if any(self._daily_number(summary.get(key)) is None for key in summary_keys):
            return None
        windows = [
            item
            for item in selection.get("windows", []) or []
            if isinstance(item, dict) and item.get("role") == "holdout"
        ]
        if len(windows) != 4:
            return None
        try:
            windows = sorted(windows, key=lambda item: int(item["role_index"]))
        except (KeyError, TypeError, ValueError):
            return None
        if [int(item["role_index"]) for item in windows] != [1, 2, 3, 4]:
            return None
        window_keys = ("return", "excess_return", "max_drawdown", "sharpe_ratio")
        if any(
            self._daily_number(window.get(key)) is None
            for window in windows
            for key in window_keys
        ):
            return None
        return {"summary": summary, "windows": windows}

    def _build_daily_nav_boxplot(self, report, max_weeks: int = 6) -> str:
        """Render a compact, email-safe weekly NAV boxplot.

        The preferred source is the daily NAV series carried by
        ``EvaluationReport``.  Older cached reports may only contain weekly
        OHLC, so they get a deterministic OHLC-derived fallback instead of
        expanding into a long text listing.
        """
        nav_dates = self._daily_get(report, "nav_dates", []) or []
        nav_series = self._daily_get(report, "nav_series", []) or []
        boxes = []

        if len(nav_dates) == len(nav_series) and nav_dates:
            frame = pd.DataFrame(
                {
                    "date": pd.to_datetime(nav_dates, errors="coerce"),
                    "nav": pd.to_numeric(nav_series, errors="coerce"),
                }
            ).dropna()
            frame = frame[np.isfinite(frame["nav"])]
            if not frame.empty:
                frame["week"] = frame["date"].dt.to_period("W-SUN")
                for period, group in frame.groupby("week", sort=True):
                    values = group["nav"].to_numpy(dtype=float)
                    if not len(values):
                        continue
                    q1, median, q3 = np.percentile(values, [25, 50, 75])
                    boxes.append(
                        {
                            "label": str(period),
                            "low": float(np.min(values)),
                            "q1": float(q1),
                            "median": float(median),
                            "q3": float(q3),
                            "high": float(np.max(values)),
                        }
                    )

        if not boxes:
            weekly = self._daily_get(report, "weekly_nav_ohlc", {}) or {}
            labels = weekly.get("labels", []) or []
            opens = weekly.get("open", []) or []
            highs = weekly.get("high", []) or []
            lows = weekly.get("low", []) or []
            closes = weekly.get("close", []) or []
            count = min(len(labels), len(opens), len(highs), len(lows), len(closes))
            for index in range(count):
                try:
                    opening = float(opens[index])
                    closing = float(closes[index])
                    low = float(lows[index])
                    high = float(highs[index])
                    values = [low, high, opening, closing]
                    if not all(np.isfinite(value) for value in values):
                        continue
                    boxes.append(
                        {
                            "label": str(labels[index]),
                            "low": min(values),
                            "q1": min(opening, closing),
                            "median": (opening + closing) / 2.0,
                            "q3": max(opening, closing),
                            "high": max(values),
                        }
                    )
                except (TypeError, ValueError):
                    continue

        if not boxes:
            return ""
        boxes = boxes[-max_weeks:]
        plot_low = min(item["low"] for item in boxes)
        plot_high = max(item["high"] for item in boxes)
        span = plot_high - plot_low
        if not np.isfinite(span) or span <= 0:
            span = 1.0
            plot_low -= 0.5

        def _top(value: float) -> str:
            position = (plot_high - value) / span * 100.0
            return f"{max(0.0, min(100.0, position)):.2f}%"

        items = []
        for item in boxes:
            box_top = _top(item["q3"])
            box_bottom = _top(item["q1"])
            box_height = max(4.0, float(box_bottom[:-1]) - float(box_top[:-1]))
            whisker_top = _top(item["high"])
            whisker_height = max(
                2.0,
                float(_top(item["low"])[:-1]) - float(whisker_top[:-1]),
            )
            items.append(
                '<div class="nav-boxplot-item">'
                '<div class="nav-boxplot-plot">'
                f'<span class="nav-boxplot-whisker" style="top:{whisker_top};height:{whisker_height:.2f}%"></span>'
                f'<span class="nav-boxplot-cap nav-boxplot-cap-top" style="top:{whisker_top}"></span>'
                f'<span class="nav-boxplot-cap nav-boxplot-cap-bottom" style="top:{_top(item["low"])}"></span>'
                f'<span class="nav-boxplot-box" style="top:{box_top};height:{box_height:.2f}%"></span>'
                f'<span class="nav-boxplot-median" style="top:{_top(item["median"])}"></span>'
                '</div>'
                '</div>'
            )
        return (
            '<div class="nav-boxplot-card">'
            '<div class="nav-boxplot" role="img" aria-label="最近六周 NAV 箱线图">'
            + "".join(items)
            + "</div>"
            '</div>'
        )


    def _build_daily_announcements_section(self, announcements):
        if not announcements:
            return '<section class="daily-section"><div class="section-heading">近期公告</div><div class="empty-card">暂无近期公告。</div></section>'
        cards = []
        for code, items in announcements.items():
            if not items:
                continue
            lines = [
                '<div class="announcement-card">'
                f'<div class="card-title">{_html_escape(code)}</div>'
            ]
            for index, item in enumerate(items[:10], 1):
                if isinstance(item, dict):
                    title = item.get("title", "未命名公告")
                    date = item.get("date", "—")
                    exchange = str(item.get("exchange", "")).upper()
                    url = _safe_html_url(item.get("url"))
                    summary = item.get("summary", "")
                    dividend_details = item.get("dividend_details") or []
                    llm_dividend = item.get("llm_extracted_dividend") or {}
                else:
                    title, date, exchange, url, summary = str(item), "—", "", "", ""
                    dividend_details, llm_dividend = [], {}
                title_html = (
                    f'<a href="{url}" target="_blank" rel="noopener">{_html_escape(title)}</a>'
                    if url
                    else _html_escape(title)
                )
                lines.append(
                    '<div class="announcement-line">'
                    f'<strong>{index}. [{_html_escape(exchange)}] {_html_escape(date)}</strong><br/>{title_html}'
                    f'{f"<br/>{_html_escape(summary)}" if summary else ""}'
                    '</div>'
                )
                detail_lines = []
                for detail in dividend_details:
                    detail_lines.append(
                        f'{self._daily_get(detail, "announcement_date", "—")}: '
                        f'分红 {_html_escape(self._daily_metric(self._daily_get(detail, "cash_dividend", self._daily_get(detail, "dividend_per_share")), "元/股", 3))}'
                    )
                if llm_dividend.get("success"):
                    detail_lines.append(
                        f'LLM分红 {_html_escape(self._daily_metric(llm_dividend.get("cash_dividend_per_share"), "元/股", 3))} · '
                        f'置信度 {_html_escape(self._daily_metric(llm_dividend.get("confidence"), "", 2))}'
                    )
                if detail_lines:
                    detail_html = "".join(f'<div class="detail-line">{_html_escape(line)}</div>' for line in detail_lines)
                    lines.append(f'<div class="muted-note">{detail_html}</div>')
            lines.append('</div>')
            cards.append("".join(lines))
        if not cards:
            return '<section class="daily-section"><div class="section-heading">近期公告</div><div class="empty-card">暂无近期公告。</div></section>'
        return '<section class="daily-section"><div class="section-heading">近期公告</div>' + "".join(cards) + '</section>'

    def _build_daily_placement_section(self, placements, stock_data):
        if not placements:
            return '<section class="daily-section"><div class="section-heading">未解禁定增</div><div class="empty-card">暂无未解禁定增。</div></section>'
        rows = self._daily_row_map(stock_data)
        cards = []
        for code, item in sorted(placements.items()):
            name = rows.get(str(code), {}).get("stock_name", "") if str(code) in rows else ""
            issue_num = self._daily_get(item, "issue_num")
            issue_num_text = self._daily_metric(issue_num / 1e8, "亿股", 2) if issue_num else "—"
            cards.append(
                '<div class="placement-card">'
                f'<div class="card-title">{_html_escape(code)} · {_html_escape(name)}</div>'
                f'<div class="stock-line">定增数量：{_html_escape(issue_num_text)} · 占总股本：{_html_escape(self._daily_metric(self._daily_get(item, "pct_of_total"), "%", 2))}</div>'
                f'<div class="stock-line">定增价格：{_html_escape(self._daily_metric(self._daily_get(item, "issue_price"), "元", 2))} · 解禁时间：{_html_escape(self._daily_get(item, "unlock_date", "—"))}</div>'
                '</div>'
            )
        return '<section class="daily-section"><div class="section-heading">未解禁定增</div>' + "".join(cards) + '</section>'

    def _build_daily_strategy_section(
        self,
        evaluation_reports,
        signal_scan=None,
        backtest=None,
        alert_codes=None,
        signal_codes=None,
    ) -> str:
        """Render one compact decision card per independent market."""
        reports = evaluation_reports if isinstance(evaluation_reports, dict) else {}
        alert_codes = set(alert_codes or ())
        signal_codes = set(signal_codes or ())
        labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
        alert_counts = {group: 0 for group in labels}
        signal_counts = {group: 0 for group in labels}
        for code in alert_codes:
            alert_counts[_detect_fine_group(code)] = (
                alert_counts.get(_detect_fine_group(code), 0) + 1
            )
        for code in signal_codes:
            signal_counts[_detect_fine_group(code)] = (
                signal_counts.get(_detect_fine_group(code), 0) + 1
            )

        parts = [
            '<section class="daily-section decision-section">'
            '<div class="section-heading">三市场决策板</div>'
        ]
        for group, label in labels.items():
            report = reports.get(group)
            action_text = (
                f'策略信号 {signal_counts[group]} · 价格预警 {alert_counts[group]}'
            )
            if report is None:
                parts.append(
                    '<div class="market-card market-card-inactive"><div class="market-title">'
                    f'<strong>{_html_escape(label)}</strong><span>{_html_escape(action_text)}</span>'
                    '</div><div class="muted-note">该市场未激活独立策略；未读取其他市场结果。</div></div>'
                )
                continue

            total_return = self._daily_get(report, "total_return")
            return_number = self._daily_number(total_return)
            return_color = (
                "metric-positive"
                if return_number is not None and return_number >= 0
                else "metric-negative"
            )
            primary = str(self._daily_get(report, "primary_benchmark", "") or "")
            benchmark_returns = self._daily_get(report, "benchmark_returns", {}) or {}
            benchmark_return = (
                benchmark_returns.get(primary)
                if isinstance(benchmark_returns, dict) and primary
                else None
            )
            benchmark_line = (
                f'主基准 ★{primary} {_html_escape(self._daily_metric(benchmark_return, "%", 1))}'
                if primary
                else "主基准未就绪"
            )
            parts.append(
                '<div class="market-card"><div class="market-title"><strong>'
                f'{_html_escape(label)} · {_html_escape(self._daily_get(report, "strategy_label", "策略评估"))}'
                f'</strong><span>{_html_escape(action_text)}</span></div>'
                f'<div class="market-benchmark">{benchmark_line}</div>'
                '<table role="presentation" class="metric-grid"><tr>'
                '<td class="metric-cell"><div class="metric-label">评估期收益</div>'
                f'<div class="metric-value {return_color}">{_html_escape(self._daily_metric(total_return, "%", 1))}</div></td>'
                '<td class="metric-cell"><div class="metric-label">对主基准超额</div>'
                f'<div class="metric-value">{_html_escape(self._daily_metric(self._daily_get(report, "excess_return"), "%", 1))}</div></td>'
                '</tr><tr><td class="metric-cell"><div class="metric-label">最大回撤</div>'
                f'<div class="metric-value">{_html_escape(self._daily_metric(self._daily_get(report, "max_drawdown"), "%", 1))}</div></td>'
                '<td class="metric-cell"><div class="metric-label">Sharpe / 交易</div>'
                f'<div class="metric-value">{_html_escape(self._daily_metric(self._daily_get(report, "sharpe_ratio"), "", 2))} / '
                f'{_html_escape(self._daily_get(report, "trade_count", "—"))}</div></td></tr></table>'
            )
            holdout = self._daily_holdout_payload(report)
            if holdout is None:
                parts.append(
                    '<div class="holdout-note">84个月 Holdout：报告尚未形成完整 22/16/2/4 产物，'
                    '请查看完整报告。</div>'
                )
            else:
                summary = holdout["summary"]
                parts.append(
                    '<div class="holdout-title">84个月 Holdout · 窗口等权，回撤取最差</div>'
                    '<div class="holdout-summary">'
                    f'收益 {_html_escape(self._daily_metric(summary["return_pct"], "%", 1))} · '
                    f'超额 {_html_escape(self._daily_metric(summary["excess_return_pct"], "%", 1))} · '
                    f'回撤 {_html_escape(self._daily_metric(summary["max_drawdown_pct"], "%", 1))} · '
                    f'Sharpe {_html_escape(self._daily_metric(summary["sharpe_ratio"], "", 2))}'
                    '</div><div class="holdout-chip-row">'
                )
                for window in holdout["windows"]:
                    parts.append(
                        '<span class="holdout-chip"><strong>'
                        f'H{int(window["role_index"])} </strong>'
                        f'R {_html_escape(self._daily_metric(window["return"], "%", 1))} · '
                        f'E {_html_escape(self._daily_metric(window["excess_return"], "%", 1))} · '
                        f'DD {_html_escape(self._daily_metric(window["max_drawdown"], "%", 1))} · '
                        f'S {_html_escape(self._daily_metric(window["sharpe_ratio"], "", 2))}'
                        '</span>'
                    )
                parts.append("</div>")
            parts.append("</div>")
        parts.append("</section>")
        return "".join(parts)

    def _build_daily_nav_boxplot_section(self, evaluation_reports) -> str:
        reports = evaluation_reports if isinstance(evaluation_reports, dict) else {}
        labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
        cards = []
        for group, label in labels.items():
            report = reports.get(group)
            if report is None:
                continue
            boxplot = self._build_daily_nav_boxplot(report, max_weeks=6)
            if boxplot:
                cards.append(
                    '<div class="nav-market-card"><div class="nav-market-title">'
                    f'{_html_escape(label)} · 最近6周</div>{boxplot}</div>'
                )
        if not cards:
            return ""
        return (
            '<section class="daily-section nav-section">'
            '<div class="section-heading">周 NAV 箱线图</div>'
            + "".join(cards)
            + "</section>"
        )

    def _build_daily_events_section(self, announcements, placements, stock_data) -> str:
        """Keep the email event feed short; detailed content remains in the PDF."""
        events = []
        for code, items in (announcements or {}).items():
            for item in items or []:
                if isinstance(item, dict):
                    date = str(item.get("date", "") or "")
                    title = str(item.get("title", "未命名公告") or "未命名公告")
                    url = _safe_html_url(item.get("url"))
                else:
                    date, title, url = "", str(item), ""
                events.append(
                    {
                        "date": date,
                        "code": str(code),
                        "kind": "公告",
                        "title": title,
                        "url": url,
                    }
                )
        row_map = self._daily_row_map(stock_data)
        for code, item in (placements or {}).items():
            if not isinstance(item, dict):
                continue
            unlock_date = str(item.get("unlock_date", "") or "")
            name = row_map.get(str(code), {}).get("stock_name", "")
            issue_num = self._daily_number(item.get("issue_num"))
            issue_price = self._daily_number(item.get("issue_price"))
            pct_of_total = self._daily_number(item.get("pct_of_total"))
            issue_num_text = self._daily_metric(
                issue_num / 1e8 if issue_num is not None else None,
                "亿股",
                2,
            )
            events.append(
                {
                    "date": unlock_date,
                    "code": str(code),
                    "kind": "未解禁定增",
                    "title": (
                        f"{name} · {issue_num_text} · "
                        f"占总股本 {self._daily_metric(pct_of_total, '%', 2)} · "
                        f"定增价 {self._daily_metric(issue_price, '元', 2)} · "
                        f"解禁 {unlock_date or '待确认'}"
                    ),
                    "url": "",
                }
            )
        if not events:
            return ""
        events.sort(key=lambda item: (item["date"], item["code"]), reverse=True)
        selected = events[:6]
        lines = [
            '<section class="daily-section events-section">'
            '<div class="section-heading">事件速览'
            f'<span class="section-count">{len(selected)}/{len(events)}</span></div>'
        ]
        for item in selected:
            title = _html_escape(item["title"])
            if item["url"]:
                title = (
                    f'<a href="{item["url"]}" target="_blank" rel="noopener">'
                    f"{title}</a>"
                )
            lines.append(
                '<div class="event-row"><span class="event-date">'
                f'{_html_escape(item["date"] or "—")}</span><span class="event-kind">'
                f'{_html_escape(item["kind"])}</span><strong>{_html_escape(item["code"])}</strong> '
                f"{title}</div>"
            )
        lines.append(
            '<div class="muted-note">完整公告、定增、技术面和持仓明细请查看 PDF 附件及完整报告。</div>'
            "</section>"
        )
        return "".join(lines)

    @staticmethod
    def _daily_chart_section(chart_png_bytes, portfolio_chart_dict):
        if not chart_png_bytes:
            return ""
        return (
            '<section class="daily-section"><div class="section-heading">关键价格图表</div>'
            '<div class="chart-card"><img src="cid:chart001" alt="关键价格与锚点图表" style="display:block;width:100%;max-width:100%;height:auto;"></div></section>'
        )

    @staticmethod
    def _daily_portfolio_chart_section(portfolio_chart_dict):
        if not isinstance(portfolio_chart_dict, dict) or not portfolio_chart_dict.get(
            "overview"
        ):
            return ""
        return (
            '<section class="daily-section portfolio-overview-section">'
            '<div class="section-heading">组合走势总览</div><div class="chart-card">'
            '<img src="cid:chart002" alt="A股、港股、美股归一化组合净值总览" '
            'style="display:block;width:100%;max-width:100%;height:auto;"></div></section>'
        )

    def _build_daily_ref_portfolio_html(self, session):
        statuses = getattr(session, "ref_portfolio_status", None) or {}
        if not isinstance(statuses, dict) or not statuses:
            return '<section class="daily-section"><div class="section-heading">参考持仓</div><div class="empty-card">暂无参考持仓数据。</div></section>'
        labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
        sections = []
        for group in ("a_share", "hk", "us"):
            status = statuses.get(group)
            if not isinstance(status, dict):
                continue
            lines = [
                '<div class="portfolio-card">'
                f'<div class="card-title">{_html_escape(status.get("_label", labels.get(group, group)))}</div>'
                f'<div class="stock-line">期初 {_html_escape(status.get("inception_date", "—"))} · '
                f'净值 {_html_escape(self._daily_metric(status.get("nav"), "", 0))} · '
                f'回报 {_html_escape(self._daily_metric(status.get("nav_return_pct"), "%", 2))} · '
                f'交易日 {_html_escape(status.get("trading_days", "—"))}</div>'
            ]
            if status.get("requires_manual_reset"):
                lines.append('<div class="muted-note">旧账户未绑定当前运行，需通过机器人 /ref_date 重置后恢复交易。</div>')
            lines.append(f'<div class="stock-line">现金：{_html_escape(self._daily_metric(status.get("cash"), "", 2))}</div></div>')
            sections.append("".join(lines))
        if not sections:
            return '<section class="daily-section"><div class="section-heading">示例账户</div><div class="empty-card">暂无示例账户数据。</div></section>'
        return '<section class="daily-section"><div class="section-heading">参考持仓 · 示例账户</div>' + "".join(sections) + '</section>'

    def _build_daily_report_links(self):
        optimizer_dir = Path("data/optimizer")
        if not optimizer_dir.exists():
            return ""
        report_files = {
            "A股": sorted(optimizer_dir.glob("*_a_share_report.html"), key=lambda p: p.stat().st_mtime, reverse=True),
            "港股": sorted(optimizer_dir.glob("*_hk_report.html"), key=lambda p: p.stat().st_mtime, reverse=True),
            "美股": sorted(optimizer_dir.glob("*_us_report.html"), key=lambda p: p.stat().st_mtime, reverse=True),
            # Keep links to installations that generated the old combined
            # report until their next optimizer run.
            "境外": sorted(optimizer_dir.glob("*_non_a_share_report.html"), key=lambda p: p.stat().st_mtime, reverse=True),
        }
        if not any(report_files.values()):
            return ""
        try:
            from ..health_server.core.global_instances import register_report_token
        except ImportError:
            from health_server.core.global_instances import register_report_token
        health = self.config.get("health_server", {}) or {}
        server_ip = health.get("public_ip") or self._get_server_info().get("ip_address", "")
        server_ip = str(server_ip).split(",")[0].split("(")[0].strip()
        if not server_ip or any(ch in server_ip for ch in '<>"\r\n\t'):
            return ""
        proto = "https" if health.get("ssl", False) else "http"
        port = health.get("port", 1933)
        links = []
        for label, paths in report_files.items():
            if not paths:
                continue
            token = register_report_token(str(paths[0]))
            url = _safe_html_url(f"{proto}://{server_ip}:{port}/report/{token}")
            if url:
                links.append(f'<a href="{url}" target="_blank" rel="noopener">{_html_escape(label)}报告</a>')
        if not links:
            return ""
        return '<div class="link-card">优化报告：' + " · ".join(links) + '（链接短期有效）</div>'

    def _build_mobile_daily_email_body(
        self,
        alert_stocks,
        stock_data,
        announcements=None,
        chart_png_bytes=None,
        portfolio_chart_dict=None,
        signal_scan=None,
        backtest=None,
        evaluation_reports=None,
        placements=None,
        instrument_audit=None,
        ref_portfolio_html="",
    ):
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "daily_email_mobile.html").read_text(encoding="utf-8")
        server_info = self._get_server_info()
        deployment_status = (
            '<div class="deployment-strip"><strong>部署状态</strong> · 节点 '
            f'{_html_escape(server_info.get("hostname"))} · 公网 IP '
            f'{_html_escape(server_info.get("ip_address"))}'
            f'<span> · {_html_escape(server_info.get("system"))} '
            f'{_html_escape(server_info.get("machine"))} '
            f'{_html_escape(server_info.get("kernel_version"))}</span></div>'
        )
        reports = evaluation_reports or {}
        watchlist_rows = self._daily_watchlist_rows(
            stock_data, alert_stocks, signal_scan
        )
        alert_codes, signal_codes = self._daily_alert_code_sets(
            alert_stocks, signal_scan
        )
        return template.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            deployment_status=deployment_status,
            summary_section=self._build_daily_summary_section(
                watchlist_rows, alert_stocks, signal_scan
            ),
            decision_section=self._build_daily_strategy_section(
                reports,
                signal_scan,
                backtest,
                alert_codes=alert_codes,
                signal_codes=signal_codes,
            ),
            action_section=self._build_daily_action_section(
                alert_stocks, signal_scan, watchlist_rows
            ),
            watchlist_section=self._build_daily_watchlist_section(watchlist_rows),
            chart_section=self._daily_chart_section(chart_png_bytes, portfolio_chart_dict),
            portfolio_chart_section=self._daily_portfolio_chart_section(portfolio_chart_dict),
            nav_boxplot_section=self._build_daily_nav_boxplot_section(reports),
            events_section=self._build_daily_events_section(
                announcements, placements, stock_data
            ),
            report_links=self._build_daily_report_links(),
        )

    def _build_email_body(
        self,
        alert_stocks,
        stock_data,
        announcements=None,
        historical_data=None,
        chart_png_bytes=None,
        portfolio_chart_dict=None,
        signal_scan=None,
        backtest=None,
        evaluation_reports=None,
        daily_mode=False,
        placements=None,
        instrument_audit=None,
        ref_portfolio_html="",
    ):
        """
        构建邮件正文（完整版：表格 + 公告 + 图表）

        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            announcements: 公告数据字典（可选）
            historical_data: 完整历史DataFrame字典 stock_code → DataFrame（可选，供图表使用）
            chart_png_bytes: 图表 PNG 原始字节（可选），有值时用 cid:chart001 嵌入

        Returns:
            str: 邮件正文（HTML格式）
        """
        if daily_mode:
            return self._build_mobile_daily_email_body(
                alert_stocks,
                stock_data,
                announcements=announcements,
                chart_png_bytes=chart_png_bytes,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
                evaluation_reports=evaluation_reports,
                placements=placements,
                instrument_audit=instrument_audit,
                ref_portfolio_html=ref_portfolio_html,
            )
        from datetime import datetime
        from pathlib import Path

        # 1. 加载模板
        template_dir = Path(__file__).parent.parent / "templates"
        email_template = (template_dir / "email_template.html").read_text(
            encoding="utf-8"
        )
        alert_section_template = (template_dir / "alert_section.html").read_text(
            encoding="utf-8"
        )

        # 2. 构建满足条件的股票行（拆分为技术指标和基本面指标）
        alert_rows_technical = ""
        alert_rows_fundamental = ""
        seen_fundamental = set()  # 基本面去重：每个股票只加一次
        for alert in alert_stocks:
            if alert.get("type") == "strategy":
                continue  # 策略告警在独立 section 渲染
            if self._is_multi_alert_format(alert):
                # 多层级警报格式
                technical_row, fundamental_row = self._build_alert_rows_multi(
                    alert, stock_data
                )
                alert_rows_technical += technical_row
                # 基本面去重：同一股票只加一次
                multi_code = alert.get("stock_code", "")
                if multi_code and multi_code not in seen_fundamental:
                    alert_rows_fundamental += fundamental_row
                    seen_fundamental.add(multi_code)
            else:
                # 单锚点警报格式（向后兼容）
                stock_code = alert.get("stock_code", "")
                low_price = alert.get("low_price")
                ma60 = alert.get("ma60")
                low_ma60_diff = alert.get("price_difference", 0)  # 最低价与MA60差值
                low_ma60_pct = alert.get(
                    "percentage_difference", 0
                )  # 最低价与MA60百分比差值

                # 从stock_data中查找股票名称、收盘价和其他数据
                stock_row = stock_data[stock_data["stock_code"] == stock_code]
                stock_name = stock_code
                close_price = 0
                if not stock_row.empty:
                    stock_name = stock_row.iloc[0].get("stock_name", stock_code)
                    close_price = stock_row.iloc[0].get("close", 0)

                # 计算收盘价与MA60差值（安全处理None值）
                close_ma60_diff = None
                close_ma60_pct = None
                if (
                    close_price is not None
                    and ma60 is not None
                    and not pd.isna(close_price)
                    and not pd.isna(ma60)
                ):
                    close_ma60_diff = close_price - ma60
                    close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0

                # 获取基本面数据
                dividend_per_share = None
                dividend_yield = None
                pe_ratio = None
                pb_ratio = None
                roe = None

                if not stock_row.empty:
                    dividend_per_share = stock_row.iloc[0].get("dividend_per_share")
                    dividend_yield = stock_row.iloc[0].get("dividend_yield")
                    pe_ratio = stock_row.iloc[0].get("pe_ratio")
                    pb_ratio = stock_row.iloc[0].get("pb_ratio")
                    roe = stock_row.iloc[0].get("roe")

                # 格式化基本面数据
                dividend_per_share_str = (
                    f"{dividend_per_share:.3f}"
                    if dividend_per_share is not None
                    and not pd.isna(dividend_per_share)
                    else "—"
                )
                dividend_yield_str = (
                    f"{dividend_yield:.2f}%"
                    if dividend_yield is not None and not pd.isna(dividend_yield)
                    else "—"
                )
                pe_ratio_str = (
                    f"{pe_ratio:.2f}"
                    if pe_ratio is not None and not pd.isna(pe_ratio)
                    else "—"
                )
                pb_ratio_str = (
                    f"{pb_ratio:.2f}"
                    if pb_ratio is not None and not pd.isna(pb_ratio)
                    else "—"
                )
                roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "—"

                # 确定颜色样式（inline for email clients）
                pos_color = "color:#27ae60"
                neg_color = "color:#c0392b"
                close_diff_style = (
                    pos_color
                    if close_ma60_diff is not None and close_ma60_diff >= 0
                    else neg_color
                    if close_ma60_diff is not None
                    else ""
                )
                close_pct_style = (
                    pos_color
                    if close_ma60_pct is not None and close_ma60_pct >= 0
                    else neg_color
                    if close_ma60_pct is not None
                    else ""
                )
                low_price_str = (
                    f"{low_price:.2f}"
                    if low_price is not None and not pd.isna(low_price)
                    else "—"
                )
                ma60_str = (
                    f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "—"
                )
                close_price_str = (
                    f"{close_price:.2f}"
                    if close_price is not None and not pd.isna(close_price)
                    else "—"
                )
                close_ma60_diff_str = (
                    f"{close_ma60_diff:+.2f}" if close_ma60_diff is not None else "—"
                )
                close_ma60_pct_str = (
                    f"{close_ma60_pct:+.2f}%" if close_ma60_pct is not None else "—"
                )
                low_ma60_diff_str = (
                    f"{low_ma60_diff:.2f}" if low_ma60_diff is not None else "—"
                )
                low_ma60_pct_str = (
                    f"{low_ma60_pct:.2f}%" if low_ma60_pct is not None else "—"
                )

                # 技术指标行
                alert_rows_technical += f"""
                    <tr style="background:#fef9e7">
                        <td>{stock_code}</td>
                        <td>{stock_name}</td>
                        <td>{low_price_str}</td>
                        <td>{ma60_str}</td>
                        <td>{close_price_str}</td>
                        <td style="{close_diff_style}">{close_ma60_diff_str}</td>
                        <td style="{close_pct_style}">{close_ma60_pct_str}</td>
                        <td style="{neg_color}">{low_ma60_diff_str}</td>
                        <td style="{neg_color}">{low_ma60_pct_str}</td>
                        <td>[MA60] 最低价 &lt; MA60</td>
                    </tr>
                """

                # 基本面指标行（去重：同一股票只加一次）
                if stock_code and stock_code not in seen_fundamental:
                    alert_rows_fundamental += f"""
                    <tr style="background:#fef9e7">>
                        <td>{stock_code}</td>
                        <td>{stock_name}</td>
                        <td>{dividend_per_share_str}</td>
                        <td>{dividend_yield_str}</td>
                        <td>{pe_ratio_str}</td>
                        <td>{pb_ratio_str}</td>
                        <td>{roe_str}</td>
                    </tr>
                """
                    seen_fundamental.add(stock_code)

        # 3. 构建所有监控股票行。日报与提醒共用完整字段，避免日报
        #    "精简" 时把锚点和技术面这两类决策数据一起删掉。
        all_rows_price = ""
        all_rows_fundamental = ""
        all_rows_technical = ""

        price_table_header = (
            '<th style="text-align:left;padding:8px">代码</th>\n'
            '        <th style="text-align:left;padding:8px">名称</th>\n'
            '        <th style="text-align:right;padding:8px">开盘</th>\n'
            '        <th style="text-align:right;padding:8px">收盘</th>\n'
            '        <th style="text-align:right;padding:8px">最高</th>\n'
            '        <th style="text-align:right;padding:8px">最低</th>\n'
            '        <th style="text-align:left;padding:8px">锚点</th>\n'
            '        <th style="text-align:right;padding:8px">锚值</th>\n'
            '        <th style="text-align:right;padding:8px">偏离%</th>\n'
            '        <th style="text-align:left;padding:8px">状态</th>'
        )
        for _, row in stock_data.iterrows():
            stock_code = row.get("stock_code", "")
            stock_name = row.get("stock_name", stock_code)
            open_price = row.get("open", 0)
            close_price = row.get("close", 0)
            high_price = row.get("high")
            low_price = row.get("low")
            ma60 = row.get("ma60")

            anchors = {}
            for anchor_key in ("ma60", "wma20", "wma30", "wma50"):
                anchor_value = row.get(anchor_key)
                if anchor_value is not None and not pd.isna(anchor_value):
                    anchors[anchor_key] = float(anchor_value)
            best_anchor = (
                self._pick_best_anchor(float(close_price), anchors)
                if close_price is not None
                and not pd.isna(close_price)
                and anchors
                else None
            )
            if best_anchor:
                anchor_name, anchor_value, anchor_deviation_pct = best_anchor
            else:
                anchor_name, anchor_value, anchor_deviation_pct = "—", None, None

            # 计算收盘价与MA60差值（仅在数据有效时计算）
            if (
                close_price is not None
                and ma60 is not None
                and not pd.isna(close_price)
                and not pd.isna(ma60)
            ):
                close_ma60_diff = close_price - ma60
                close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0
            else:
                close_ma60_diff = None
                close_ma60_pct = None

            # 获取基本面数据
            dividend_per_share = row.get("dividend_per_share")
            dividend_yield = row.get("dividend_yield")
            pe_ratio = row.get("pe_ratio")
            pb_ratio = row.get("pb_ratio")
            roe = row.get("roe")

            # 格式化基本面数据
            dividend_per_share_str = (
                f"{dividend_per_share:.3f}"
                if dividend_per_share is not None and not pd.isna(dividend_per_share)
                else "—"
            )
            dividend_yield_str = (
                f"{dividend_yield:.2f}%"
                if dividend_yield is not None and not pd.isna(dividend_yield)
                else "—"
            )
            pe_ratio_str = (
                f"{pe_ratio:.2f}"
                if pe_ratio is not None and not pd.isna(pe_ratio)
                else "—"
            )
            pb_ratio_str = (
                f"{pb_ratio:.2f}"
                if pb_ratio is not None and not pd.isna(pb_ratio)
                else "—"
            )
            roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "—"
            rsi = row.get("rsi")
            macd_hist = row.get("macd_hist")
            vol_ratio = row.get("vol_ratio")
            adx = row.get("adx")
            boll_pct_b = row.get("boll_pct_b")
            rsi_str = f"{rsi:.1f}" if rsi is not None and not pd.isna(rsi) else "—"
            macd_hist_str = (
                f"{macd_hist:.3f}"
                if macd_hist is not None and not pd.isna(macd_hist)
                else "—"
            )
            vol_ratio_str = (
                f"{vol_ratio:.2f}"
                if vol_ratio is not None and not pd.isna(vol_ratio)
                else "—"
            )
            adx_str = f"{adx:.1f}" if adx is not None and not pd.isna(adx) else "—"
            boll_pct_b_str = (
                f"{boll_pct_b:.2f}"
                if boll_pct_b is not None and not pd.isna(boll_pct_b)
                else "—"
            )

            # 检查是否满足条件（最低价 < MA60）- 安全处理None值
            status = "正常"
            if (
                low_price is not None
                and ma60 is not None
                and not pd.isna(low_price)
                and not pd.isna(ma60)
                and low_price < ma60
            ):
                status = "<span style='color: #f44336;'>提醒</span>"

            # 格式化价格数据（安全处理None值）
            open_price_str = (
                f"{open_price:.2f}"
                if open_price is not None and not pd.isna(open_price)
                else "—"
            )
            close_price_str = (
                f"{close_price:.2f}"
                if close_price is not None and not pd.isna(close_price)
                else "—"
            )
            high_price_str = (
                f"{high_price:.2f}"
                if high_price is not None and not pd.isna(high_price)
                else "—"
            )
            low_price_str = (
                f"{low_price:.2f}"
                if low_price is not None and not pd.isna(low_price)
                else "—"
            )
            ma60_str = f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "—"
            close_ma60_diff_str = (
                f"{close_ma60_diff:+.2f}" if close_ma60_diff is not None else "—"
            )
            close_ma60_pct_str = (
                f"{close_ma60_pct:+.2f}%" if close_ma60_pct is not None else "—"
            )
            anchor_value_str = (
                f"{anchor_value:.2f}" if anchor_value is not None else "—"
            )
            anchor_deviation_pct_str = (
                f"{anchor_deviation_pct:+.2f}%"
                if anchor_deviation_pct is not None
                else "—"
            )

            # 价格技术指标行 (inline styles for email clients)
            pos = "color:#27ae60;text-align:right"
            neg = "color:#c0392b;text-align:right"
            neut = "text-align:right"
            anchor_style = (
                pos
                if anchor_deviation_pct is not None and anchor_deviation_pct >= 0
                else neg
                if anchor_deviation_pct is not None
                else neut
            )
            all_rows_price += (
                f"<tr>"
                f"<td>{stock_code}</td>"
                f"<td>{stock_name}</td>"
                f'<td style="{neut}">{open_price_str}</td>'
                f'<td style="{neut}">{close_price_str}</td>'
                f'<td style="{neut}">{high_price_str}</td>'
                f'<td style="{neut}">{low_price_str}</td>'
                f"<td>{anchor_name}</td>"
                f'<td style="{neut}">{anchor_value_str}</td>'
                f'<td style="{anchor_style}">{anchor_deviation_pct_str}</td>'
                f"<td>{status}</td>"
                f"</tr>"
            )

            # 基本面指标行
            all_rows_fundamental += (
                f"<tr>"
                f"<td>{stock_code}</td>"
                f'<td style="{neut}">{dividend_per_share_str}</td>'
                f'<td style="{neut}">{dividend_yield_str}</td>'
                f'<td style="{neut}">{pe_ratio_str}</td>'
                f'<td style="{neut}">{pb_ratio_str}</td>'
                f'<td style="{neut}">{roe_str}</td>'
                f"</tr>"
            )
            all_rows_technical += (
                f"<tr>"
                f"<td>{stock_code}</td>"
                f'<td style="{neut}">{rsi_str}</td>'
                f'<td style="{neut}">{macd_hist_str}</td>'
                f'<td style="{neut}">{vol_ratio_str}</td>'
                f'<td style="{neut}">{adx_str}</td>'
                f'<td style="{neut}">{boll_pct_b_str}</td>'
                f"</tr>"
            )

        # 4. 构建公告部分
        announcements_section = ""
        if announcements and len(announcements) > 0:
            announcements_section = """
            <h3>近期重要公告</h3>
            <p>以下为监控股票近期发布的重要公告：</p>
            """
            for stock_code, announcement_list in announcements.items():
                if not announcement_list:
                    continue
                announcements_section += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px;">
                    <h4>股票 {stock_code}</h4>
                """
                for i, announcement in enumerate(announcement_list[:5]):
                    title = announcement.get("title", "")
                    date = announcement.get("date", "")
                    url = announcement.get("url", "")
                    exchange = announcement.get("exchange", "").upper()
                    link = (
                        f'<a href="{url}" target="_blank">{title}</a>' if url else title
                    )
                    announcements_section += f"""
                    <div style="margin-bottom: 8px;">
                        <strong>{i + 1}. [{exchange}] {date}</strong><br/>
                        {link}
                    """

                    # 检查是否有官方分红记录
                    dividend_details = announcement.get("dividend_details")
                    if dividend_details and len(dividend_details) > 0:
                        official_info = []
                        for detail in dividend_details:
                            announcement_date = detail.get("announcement_date", "未知")
                            cash_dividend = detail.get("cash_dividend")
                            dividend_per_share = detail.get("dividend_per_share")
                            # 格式化分红值，处理None情况
                            if cash_dividend is not None and not pd.isna(cash_dividend):
                                info = (
                                    f"{announcement_date}: 分红{cash_dividend:.2f}元/股"
                                )
                                official_info.append(info)
                            elif dividend_per_share is not None and not pd.isna(
                                dividend_per_share
                            ):
                                info = f"{announcement_date}: 分红{dividend_per_share:.2f}元/股"
                                official_info.append(info)
                        if official_info:
                            announcements_section += f"""
                            <div style="margin-left: 20px; margin-top: 5px; padding: 5px; background-color: #e8f4fd; border-left: 3px solid #2196f3; font-size: 0.9em;">
                                <strong>官方分红记录:</strong><br/>
                                {", ".join(official_info)}
                            </div>
                            """

                    # 检查是否有LLM提取的分红详情
                    llm_dividend = announcement.get("llm_extracted_dividend")
                    if llm_dividend and llm_dividend.get("success", False):
                        dividend_info = []
                        cash_dividend = llm_dividend.get("cash_dividend_per_share")
                        if cash_dividend is not None and not pd.isna(cash_dividend):
                            dividend_info.append(f"现金分红: {cash_dividend:.3f}元/股")
                        dividend_per_share = llm_dividend.get("dividend_per_share")
                        if dividend_per_share is not None and not pd.isna(
                            dividend_per_share
                        ):
                            dividend_info.append(
                                f"总分红: {dividend_per_share:.3f}元/股"
                            )
                        dividend_date = llm_dividend.get("dividend_date")
                        if dividend_date:
                            dividend_info.append(f"分红日期: {dividend_date}")
                        confidence = llm_dividend.get("confidence")
                        confidence_pct = (
                            f"{confidence * 100:.0f}%"
                            if confidence is not None and not pd.isna(confidence)
                            else "N/A"
                        )

                        if dividend_info:
                            announcements_section += f"""
                            <div style="margin-left: 20px; margin-top: 5px; padding: 5px; background-color: #f8f9fa; border-left: 3px solid #4caf50; font-size: 0.9em;">
                                <strong>LLM提取分红详情（置信度: {confidence_pct}）:</strong><br/>
                                {", ".join(dividend_info)}
                            </div>
                            """
                    announcements_section += """
                    </div>
                    """
                announcements_section += """
                </div>
                """
            announcements_section += """
             <p><em>注：公告信息仅供参考，请以交易所官方公告为准。</em></p>
            """

        # 6b. 构建搜参策略结果段
        strategy_results_section = ""
        if evaluation_reports:
            strategy_results_section = self._build_strategy_results_section(
                evaluation_reports,
                signal_scan=signal_scan,
            )

        # 6c. 投资组合策略分析（旧版，日报模式跳过）
        portfolio_section = ""

        # 7. 走势图表（由调用方生成，通过 chart_png_bytes 传入，使用 CID 内嵌）
        chart_section = ""
        if chart_png_bytes:
            chart_section = """
            <h3>价格走势图</h3>
            <p>近2个月收盘价走势：</p>
            <div style="text-align: center; margin: 20px 0;">
                <img src="cid:chart001"
                     alt="价格走势图"
                     style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" />
            </div>
            """

        # 7b. 投资组合走势图（chart002=A股, chart003=港股, chart004=美股, chart005=非A兼容）
        portfolio_chart_section = ""
        if portfolio_chart_dict:
            cid_map = {
                "a_share": "chart002",
                "hk": "chart003",
                "us": "chart004",
                "non_a_share": "chart005",
            }
            group_titles = {
                "a_share": "A股投资组合净值走势",
                "hk": "港股投资组合净值走势",
                "us": "美股投资组合净值走势",
                "non_a_share": "非A股投资组合净值走势",
            }
            for group_key in ("a_share", "hk", "us", "non_a_share"):
                if group_key in portfolio_chart_dict:
                    cid = cid_map[group_key]
                    title = group_titles.get(group_key, group_key)
                    portfolio_chart_section += f"""
            <h3>{title}</h3>
            <div style="text-align: center; margin: 10px 0 20px 0;">
                <img src="cid:{cid}"
                     alt="{title}"
                     style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" />
            </div>
            """

        # 7b. 策略信号报警 — 日报模式已合并到搜参策略结果段，不单独渲染
        strategy_alert_section = ""
        if signal_scan and not daily_mode:
            strategy_alert_section = self._build_strategy_alert_section(
                signal_scan, alert_stocks, stock_data
            )

        # 7c. 回测分析 — 日报模式跳过
        backtest_section = ""
        if not daily_mode and backtest:
            backtest_section = self._build_backtest_section(backtest)

        # 8. 获取服务器信息
        server_info = self._get_server_info()

        # 7. 构建报警股票部分 — 日报模式跳过
        alert_section = ""
        is_multi_format = False  # 保存格式标志供后续使用
        if alert_stocks and not daily_mode:
            # 确定警报格式（检查第一个警报）
            if alert_stocks and len(alert_stocks) > 0:
                is_multi_format = self._is_multi_alert_format(alert_stocks[0])

            if is_multi_format:
                # 多层级警报格式
                alert_section = f"""
                <h3>满足条件的股票 ({len(alert_stocks)} 只)</h3>
                
                <h4>多层级警报技术指标</h4>
                <table>
                    <tr>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>价格</th>
                        <th>锚点值</th>
                        <th>价格差值</th>
                        <th>百分比(%)</th>
                        <th>锚点名称</th>
                        <th>区间标签</th>
                        <th>连续天数</th>
                        <th>条件</th>
                    </tr>
                    {alert_rows_technical}
                </table>
                
                <h4>基本面指标</h4>
                <table>
                    <tr>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>每股分红(元)</th>
                        <th>股息率(%)</th>
                        <th>PE</th>
                        <th>PB</th>
                        <th>ROE(%)</th>
                        <th>负债率(%)</th>
                    </tr>
                    {alert_rows_fundamental}
                </table>
                """
            else:
                # 单锚点警报格式（使用模板）
                alert_section = alert_section_template.format(
                    alert_count=len(alert_stocks),
                    alert_rows_technical=alert_rows_technical,
                    alert_rows_fundamental=alert_rows_fundamental,
                )

        # 8. 根据警报格式更新模板标题
        if is_multi_format:
            # 替换为多层级警报标题
            email_template = email_template.replace(
                "系统检测到以下股票满足条件：<strong>当天最低价 &lt; MA60（前复权）</strong>",
                "系统检测到以下股票满足多层级警报条件：<strong>多锚点阈值区间突破</strong>",
            )
        else:
            # 确保是单锚点标题（默认）
            email_template = email_template.replace(
                "系统检测到以下股票满足条件：<strong>多锚点阈值区间突破</strong>",
                "系统检测到以下股票满足条件：<strong>当天最低价 &lt; MA60（前复权）</strong>",
            )

        # 8.5. 报告链接（A股 + 境外各一份，30 分钟后过期）
        report_link = ""
        try:
            optimizer_dir = Path("data/optimizer")
            if optimizer_dir.exists():
                a_r = sorted(
                    optimizer_dir.glob("*_a_share_report.html"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                nona_r = sorted(
                    optimizer_dir.glob("*_non_a_share_report.html"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if a_r or nona_r:
                    from ..health_server.core.global_instances import (
                        register_report_token,
                    )

                    hc = self.config.get("health_server", {})
                    server_ip = hc.get("public_ip", "")
                    port = hc.get("port", 1933)
                    use_ssl = hc.get("ssl", False)

                    if not server_ip:
                        try:
                            import urllib.request

                            ip_url = hc.get("ip_detect_url", "https://ifconfig.me")
                            server_ip = (
                                urllib.request.urlopen(ip_url, timeout=5)
                                .read()
                                .decode("utf-8")
                                .strip()
                            )
                        except Exception as e:
                            logger.debug(f"IP检测服务失败: {e}")
                            fi = self._get_server_info().get("ip_address", "localhost")
                            for p in (
                                fi.replace("(优先)", "")
                                .replace("(", "")
                                .replace(")", "")
                                .split(",")
                            ):
                                s = p.strip().split()[0] if p.strip() else ""
                                if s and not s.startswith(
                                    ("172.", "10.", "192.168.", "127.")
                                ):
                                    server_ip = s
                                    break
                            if server_ip == "localhost":
                                server_ip = fi.split(",")[0].strip().split()[0]

                    proto = "https" if use_ssl else "http"
                    links_html = ""
                    for label, report_list in [("A股", a_r), ("境外", nona_r)]:
                        if not report_list:
                            continue
                        token = register_report_token(str(report_list[0]))
                        links_html += (
                            f'<a href="{proto}://{server_ip}:{port}/report/{token}" '
                            f'style="color:#2980b9;text-decoration:none">'
                            f"{label}</a> &nbsp;"
                        )
                    if links_html:
                        report_link = (
                            f'<tr><td style="padding:8px 16px;color:#7f8c8d;font-size:13px">'
                            f"交互报告: {links_html}"
                            f'<span style="font-size:11px">(30分钟)</span></td></tr>'
                        )
        except Exception as e:
            logger.debug(f"交互报告链接生成失败: {e}")

        # 9. 替换主模板变量
        placement_section = self._build_placement_section(placements, stock_data)
        try:
            from ..instruments.audit import render_profile_section
        except ImportError:
            # Some legacy entry points expose ``src`` directly on sys.path and
            # import this module as ``notification.email_notifier``.
            from instruments.audit import render_profile_section

        instrument_profile_section = render_profile_section(instrument_audit)
        html_content = email_template.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            alert_section=alert_section,
            all_rows_price=all_rows_price,
            all_rows_fundamental=all_rows_fundamental,
            instrument_profile_section=instrument_profile_section,
            all_rows_technical=all_rows_technical,
            price_table_header=price_table_header,
            announcements_section=announcements_section,
            placement_section=placement_section,
            chart_section=chart_section,
            portfolio_chart_section=portfolio_chart_section,
            strategy_alert_section=strategy_alert_section,
            backtest_section=backtest_section,
            report_link=report_link,
            portfolio_section=portfolio_section,
            strategy_results_section=strategy_results_section,
            server_hostname=server_info["hostname"],
            server_ip=server_info["ip_address"],
        )

        return html_content

    def _is_multi_alert_format(self, alert):
        """
        判断警报是否为多层级格式

        Args:
            alert: 警报字典

        Returns:
            bool: 如果是多层级格式返回True，否则返回False
        """
        # 多层级警报包含anchor_name字段，单锚点警报包含ma60字段
        return "anchor_name" in alert and "interval_label" in alert

    def _build_alert_rows_multi(self, alert, stock_data):
        """
        构建多层级警报的行HTML

        Args:
            alert: 多层级警报字典
            stock_data: 股票数据DataFrame

        Returns:
            tuple: (technical_row, fundamental_row) HTML字符串
        """
        stock_code = alert.get("stock_code", "")
        # 从stock_data管道获取股票名称
        stock_name = stock_code
        stock_row_lookup = stock_data[stock_data["stock_code"] == stock_code]
        if not stock_row_lookup.empty:
            stock_name = stock_row_lookup.iloc[0].get("stock_name", stock_code)
        anchor_name = alert.get("anchor_name", "")
        anchor_value = alert.get("anchor_value")
        interval_label = alert.get("interval_label", "")
        percentage = alert.get("percentage")
        consecutive_days = alert.get("consecutive_days", 1)
        price = alert.get("low_price")
        price_difference = alert.get("price_difference")

        # 从stock_data中查找基本面数据
        stock_row = stock_data[stock_data["stock_code"] == stock_code]

        # 获取基本面数据
        dividend_per_share = None
        dividend_yield = None
        pe_ratio = None
        pb_ratio = None
        roe = None

        if not stock_row.empty:
            dividend_per_share = stock_row.iloc[0].get("dividend_per_share")
            dividend_yield = stock_row.iloc[0].get("dividend_yield")
            pe_ratio = stock_row.iloc[0].get("pe_ratio")
            pb_ratio = stock_row.iloc[0].get("pb_ratio")
            roe = stock_row.iloc[0].get("roe")

        # 格式化基本面数据
        dividend_per_share_str = (
            f"{dividend_per_share:.3f}"
            if dividend_per_share is not None and not pd.isna(dividend_per_share)
            else "—"
        )
        dividend_yield_str = (
            f"{dividend_yield:.2f}%"
            if dividend_yield is not None and not pd.isna(dividend_yield)
            else "—"
        )
        pe_ratio_str = (
            f"{pe_ratio:.2f}" if pe_ratio is not None and not pd.isna(pe_ratio) else "—"
        )
        pb_ratio_str = (
            f"{pb_ratio:.2f}" if pb_ratio is not None and not pd.isna(pb_ratio) else "—"
        )
        roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "—"

        # 构建技术指标行
        condition = f"{anchor_name} 区间 {interval_label} (连续{consecutive_days}天)"
        price_str = f"{price:.2f}" if price is not None and not pd.isna(price) else "—"
        anchor_value_str = (
            f"{anchor_value:.2f}"
            if anchor_value is not None and not pd.isna(anchor_value)
            else "—"
        )
        price_diff_str = (
            f"{price_difference:+.2f}"
            if price_difference is not None and not pd.isna(price_difference)
            else "—"
        )
        pct_str = (
            f"{percentage:+.2f}%"
            if percentage is not None and not pd.isna(percentage)
            else "—"
        )

        technical_row = f"""
            <tr style="background:#fef9e7">
                <td>{stock_code}</td>
                <td>{stock_name}</td>
                <td>{price_str}</td>
                <td>{anchor_value_str}</td>
                <td>{price_diff_str}</td>
                <td>{pct_str}</td>
                <td>{anchor_name}</td>
                <td>{interval_label}</td>
                <td>{consecutive_days}天</td>
                <td>{condition}</td>
            </tr>
        """

        # 构建基本面指标行
        fundamental_row = f"""
            <tr style="background:#fef9e7">
                <td>{stock_code}</td>
                <td>{stock_name}</td>
                <td>{dividend_per_share_str}</td>
                <td>{dividend_yield_str}</td>
                <td>{pe_ratio_str}</td>
                <td>{pb_ratio_str}</td>
                <td>{roe_str}</td>
            </tr>
        """

        return technical_row, fundamental_row


    def _get_server_info(self):
        """
        获取服务器信息（IP地址和内核版本）

        Returns:
            dict: 包含服务器信息的字典
        """
        try:
            # 获取主机名和IP地址
            hostname = socket.gethostname()
            ip_list = []

            # 方法1: 通过socket.gethostbyname_ex获取所有IP
            try:
                _, _, ip_addresses = socket.gethostbyname_ex(hostname)
                ip_list.extend(ip_addresses)
            except Exception as e:
                logger.debug(f"gethostbyname_ex 失败: {e}")

            # 方法2: 通过hostname -I命令获取所有IP（Linux）
            try:
                result = subprocess.run(
                    ["hostname", "-I"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    ips = result.stdout.strip().split()
                    ip_list.extend(ips)
            except Exception as e:
                logger.debug(f"hostname -I 失败: {e}")

            # 方法3: 获取公网IP（可选）
            try:
                import urllib.request

                ip_url = self.config.get("health_server", {}).get(
                    "ip_detect_url", "https://ifconfig.me"
                )
                public_ip = (
                    urllib.request.urlopen(ip_url, timeout=10)
                    .read()
                    .decode("utf-8")
                    .strip()
                )
                if public_ip and public_ip not in ip_list:
                    ip_list.append(f"{public_ip} (公网)")
            except Exception as e:
                logger.debug(f"公网IP检测失败: {e}")

            # 去重并过滤回环地址
            ip_list = list(set(ip_list))
            ip_list = [ip for ip in ip_list if not ip.startswith("127.")]

            if ip_list:
                ip_address = ", ".join(ip_list)
            else:
                ip_address = "无法获取"

            # 获取内核版本（Linux系统）
            kernel_version = "未知"
            try:
                # 尝试通过platform模块获取
                kernel_version = platform.release()
                if not kernel_version or kernel_version == "":
                    # 尝试通过uname命令获取
                    result = subprocess.run(
                        ["uname", "-r"], capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        kernel_version = result.stdout.strip()
            except Exception as e:
                logger.debug(f"内核版本获取失败: {e}")
                kernel_version = platform.uname().release

            return {
                "hostname": hostname,
                "ip_address": ip_address,
                "kernel_version": kernel_version,
                "system": platform.system(),
                "machine": platform.machine(),
            }
        except Exception as e:
            logger.warning(f"获取服务器信息失败: {e}")
            return {
                "hostname": "未知",
                "ip_address": "无法获取",
                "kernel_version": "未知",
                "system": "未知",
                "machine": "未知",
            }

    # ────────────── 简报方法 ──────────────

    # Thin alias: the implementation moved to report_builders.pick_best_anchor
    # but EmailNotifier._pick_best_anchor is part of the de-facto API
    # (feishu_notifier, telegram_notifier, 11 test call sites).
    _pick_best_anchor = staticmethod(pick_best_anchor)

    # ── 参考持仓 HTML 构建 ────────────────────────────────────

    @staticmethod
    def _build_ref_portfolio_html(session, today_date) -> str:
        """构建参考持仓 HTML 片段。三组（A股/港股/美股）各自展示。"""
        all_statuses = getattr(session, "ref_portfolio_status", None)
        if not isinstance(all_statuses, dict) or not all_statuses:
            return ""

        lines = ["<h3>📊 参考持仓</h3>"]
        group_order = ["a_share", "hk", "us"]

        for gk in group_order:
            status = all_statuses.get(gk)
            if not status:
                continue
            label = status.get("_label", gk)
            lines.append(
                f"<h4>{label}</h4>"
                f"<p>📅 期初: {status['inception_date']} | "
                f"💰 净值: {status['nav']:,.0f} | "
                f"📈 回报: {status['nav_return_pct']:+.2f}% | "
                f"📆 交易日: {status['trading_days']}</p>"
            )

            if status["holdings"]:
                lines.append(
                    "<table><tr><th>代码</th><th>持仓</th><th>现价</th>"
                    "<th>市值</th><th>成本</th></tr>"
                )
                for h in status["holdings"]:
                    lines.append(
                        f"<tr>"
                        f"<td>{h['code']}</td>"
                        f"<td>{h['shares']}</td>"
                        f"<td>{h['price']:.2f}</td>"
                        f"<td>{h['market_value']:,.0f}</td>"
                        f"<td>{h['avg_cost']:.2f}</td>"
                        f"</tr>"
                    )
                lines.append("</table>")
            else:
                lines.append("<p>📭 空仓</p>")

            lines.append(f"<p>💵 现金: {status['cash']:,.2f}</p>")

        return "\n".join(lines)

    def send_brief_report(self, session, report_config: dict):
        """
        发送简报邮件（仅价格+锚点偏离率，无图表/基本面/公告）。

        Args:
            session: SessionContext
            report_config: 简报配置 {"id": "morning_snapshot", "label": "早盘简报", ...}
        """
        from datetime import datetime
        from pathlib import Path

        label = report_config.get("label", "简报")
        stock_data = session.get_all_dataframe()
        today = datetime.now()
        today_date = today.date()

        # ── 构建每只股票的行 ──
        rows_data = build_brief_entries(stock_data, today)

        # ── 策略信号（直接用 SignalScanner 结果，和日报一致）──
        signal_scan = getattr(session, "signal_scan", None)
        strat_html = ""
        if signal_scan and signal_scan.alerts:
            alerts = signal_scan.alerts
            map_a = _build_signal_label_map("a_share")
            map_hk = _build_signal_label_map("hk") or _build_signal_label_map(
                "non_a_share"
            )
            map_us = _build_signal_label_map("us") or _build_signal_label_map(
                "non_a_share"
            )
            strat_html = (
                "<h3>策略信号</h3>"
                f"<p>共 {len(alerts)} 条策略告警</p>"
                "<table><tr><th>代码</th><th>信号</th><th>当前值</th></tr>"
            )
            for a in alerts[:12]:
                code = _alert_value(a, "stock_code", "?")
                raw = _alert_value(a, "rule_label", "?")
                readable = _readable_signal(code, raw, map_a, map_hk, map_us)
                cv = _alert_value(a, "current_value", "-")
                strat_html += (
                    f"<tr><td>{code}</td><td>{readable}</td><td>{cv}</td></tr>"
                )
            strat_html += "</table><br>"
        elif signal_scan:
            strat_html = "<h3>策略信号</h3><p>当前无活跃信号</p><br>"

        # ── 参考持仓状态 ──
        ref_html = self._build_ref_portfolio_html(session, today_date)

        # ── 渲染 HTML ──
        html_rows = []
        for entry in rows_data:
            open_str = (
                f"{entry['open']:.2f}"
                if entry["open"] is not None and not pd.isna(entry["open"])
                else "—"
            )
            close_str = (
                f"{entry['close']:.2f}"
                if entry["close"] is not None and not pd.isna(entry["close"])
                else "—"
            )
            anchor_str = (
                f"{entry['anchor_val']:.2f}" if entry["anchor_val"] is not None else "-"
            )
            dev_color = "#27ae60" if (entry["dev_pct"] or 0) >= 0 else "#c0392b"
            html_row = (
                f"<tr>"
                f"<td>{entry['code']}</td>"
                f"<td>{entry['name']}</td>"
                f"<td>{open_str}</td>"
                f"<td>{close_str}</td>"
                f"<td>{entry['anchor_name']}</td>"
                f"<td>{anchor_str}</td>"
                f'<td style="color:{dev_color}">{entry["dev_str"]}</td>'
                f"</tr>\n"
            )
            html_rows.append(html_row)

        rows = "".join(html_rows)
        active_count = len(rows_data)
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "brief_email.html").read_text(encoding="utf-8")

        body = template.format(
            label=label,
            report_date=today.strftime("%Y-%m-%d"),
            current_time=today.strftime("%H:%M"),
            active_count=active_count,
            total_count=active_count,
            brief_rows=rows,
            strategy_suggestions=strat_html,
            ref_portfolio=ref_html,
        )

        subject = f"{label} - {today.strftime('%Y-%m-%d')}"
        self._send_email(subject, body)

    # ────────────────────────────────────

    def _send_email(
        self,
        subject,
        body,
        chart_png_bytes=None,
        portfolio_chart_dict=None,
        pdf_bytes=None,
        candlestick_png=None,
    ):
        """
        发送邮件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
            chart_png_bytes: 告警走势图 PNG 字节（可选），CID=chart001
            portfolio_chart_dict: 投资组合走势图 {"a_share": bytes, "non_a_share": bytes}
            pdf_bytes: 日报 PDF 附件 bytes（可选）
            candlestick_png: 周K蜡烛图 bytes（可选），CID=candlestick
        """
        import os

        # 保存邮件副本（无论是否跳过发送）
        copy_path = self._save_email_copy(subject, body)
        if copy_path:
            logger.info("邮件副本保存成功，路径: %s", copy_path)
        else:
            logger.error("邮件副本未保存，目标目录: %s", self.email_archive_dir)

        if os.environ.get("SKIP_EMAIL") == "true":
            logger.info(f"跳过邮件发送（测试模式）: 主题={subject}")
            return

        try:
            # 创建 HTML 部分
            html_part = MIMEText(body, "html", "utf-8")
            html_part.set_charset("utf-8")
            html_part["Content-Transfer-Encoding"] = "quoted-printable"

            has_any_chart = chart_png_bytes or portfolio_chart_dict or candlestick_png

            if has_any_chart:
                # 有任意图表：MIMEMultipart("related") 容器，HTML + 内嵌图片
                inner = MIMEMultipart("related")
                inner.policy = policy.default

                # 内嵌 alternative（HTML）
                alt = MIMEMultipart("alternative")
                alt.attach(html_part)
                inner.attach(alt)

                # 添加告警走势图（CID: chart001）
                if chart_png_bytes:
                    image = MIMEImage(chart_png_bytes, "png")
                    image.add_header("Content-ID", "<chart001>")
                    image.add_header(
                        "Content-Disposition", "inline", filename="chart.png"
                    )
                    inner.attach(image)
                    logger.info("告警走势图以 CID chart001 嵌入邮件")

                # 添加周K蜡烛图（CID: candlestick）
                if candlestick_png:
                    cs_img = MIMEImage(candlestick_png, "png")
                    cs_img.add_header("Content-ID", "<candlestick>")
                    cs_img.add_header(
                        "Content-Disposition", "inline", filename="candlestick.png"
                    )
                    inner.attach(cs_img)
                    logger.info("周K蜡烛图以 CID candlestick 嵌入邮件")

                # 日报优先使用单张归一化三市场总览；兼容旧调用方的分市场图。
                if portfolio_chart_dict:
                    cid_map = {
                        "overview": "chart002",
                        "a_share": "chart002",
                        "hk": "chart003",
                        "us": "chart004",
                        "non_a_share": "chart005",
                    }
                    for group_key, png_bytes in portfolio_chart_dict.items():
                        if png_bytes and group_key in cid_map:
                            cid = cid_map[group_key]
                            img = MIMEImage(png_bytes, "png")
                            img.add_header("Content-ID", f"<{cid}>")
                            img.add_header(
                                "Content-Disposition",
                                "inline",
                                filename=f"portfolio_{group_key}.png",
                            )
                            inner.attach(img)
                            logger.info(f"投资组合走势图以 CID {cid} 嵌入邮件")
            else:
                # 无图表：保持原逻辑
                inner = MIMEMultipart("alternative")
                inner.policy = policy.default
                inner.attach(html_part)

            # 如有 PDF 附件，外层包 MIMEMultipart("mixed")
            if pdf_bytes:
                from email.mime.application import MIMEApplication

                msg = MIMEMultipart("mixed")
                msg.policy = policy.default
                msg.attach(inner)
                pdf_part = MIMEApplication(pdf_bytes, "pdf")
                pdf_part.add_header(
                    "Content-Disposition", "attachment", filename="日报.pdf"
                )
                msg.attach(pdf_part)
                logger.info("日报 PDF 已附加到邮件")
            else:
                msg = inner

            # 邮件主题（使用UTF-8编码策略自动处理）
            msg["Subject"] = subject

            # 编码发件人和收件人
            msg["From"] = self.sender_email
            msg["To"] = self.receiver_email

            # 连接到SMTP服务器并发送邮件
            if self.enable_ssl:
                # 使用SSL连接
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    self.smtp_server, self.smtp_port, timeout=30, context=context
                ) as server:
                    # 登录邮箱
                    server.login(self.sender_email, self.sender_password)

                    # 发送邮件
                    server.send_message(msg)
            else:
                # 使用普通SMTP连接
                with smtplib.SMTP(
                    self.smtp_server, self.smtp_port, timeout=30
                ) as server:
                    if self.enable_tls:
                        server.starttls()  # 启用TLS加密

                    # 登录邮箱
                    server.login(self.sender_email, self.sender_password)

                    # 发送邮件
                    server.send_message(msg)

            logger.debug(f"邮件发送成功: {subject}")

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP认证失败: {e}")
            raise
        except smtplib.SMTPException as e:
            logger.error(f"SMTP错误: {e}")
            raise
        except Exception as e:
            logger.error(f"发送邮件时发生未知错误: {e}", exc_info=True)
            raise

    def send_deployment_notification(
        self, deployment_info=None, version=None, summary=None
    ):
        """
        发送部署通知邮件

        Returns:
            (ok, message): ok 为 True 表示邮件已发出，False 表示失败
        """
        try:
            # 获取服务器信息
            server_info = self._get_server_info()

            # 构建部署邮件主题
            subject = f"部署完成通知 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

            # 构建部署邮件正文
            body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>部署完成通知</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; border-bottom: 1px solid #ddd; padding-bottom: 10px; }}
        .info {{ margin: 15px 0; padding: 10px; background-color: #f5f5f5; border-radius: 5px; }}
        .success {{ color: #4caf50; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>部署完成通知</h1>
    <p class="success">✅ 股票量化系统已成功部署到生产服务器</p>
    
    <div class="info">
        <p><strong>部署时间:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
        <p><strong>部署服务器:</strong> {server_info["hostname"]}</p>
        <p><strong>服务器IP:</strong> {server_info["ip_address"]}</p>
        <p><strong>系统信息:</strong> {server_info["system"]} {server_info["machine"]} (内核: {server_info["kernel_version"]})</p>
        {f"<p><strong>部署版本:</strong> {version}</p>" if version else ""}
        {f"<p><strong>部署摘要:</strong> {summary}</p>" if summary else ""}
    </div>
    
    {f'<div class="info"><p><strong>部署详情:</strong> {deployment_info}</p></div>' if deployment_info else ""}
    
    <p><em>注：此邮件由股票量化系统自动发送，用于部署验证。</em></p>
</body>
</html>"""

            # 发送邮件
            self._send_email(subject, body)
            logger.info(f"部署通知邮件发送成功: {subject} (version={version})")
            return True, "sent"

        except Exception as e:
            msg = str(e)
            logger.error(f"发送部署通知邮件失败: {msg}")
            return False, msg

    def send_optimizer_notification(
        self, report, group_name: str = "", full_report: dict | None = None
    ) -> None:
        """发送优化结果邮件。含完整回测报告（日回报测+敏感性+波动率）。"""
        body = build_optimizer_summary(report, group_name, full_report)
        subject = optimizer_notification_title(report, group_name)
        candlestick_png = (full_report or {}).get("candlestick_png")
        self._send_email(subject, body, candlestick_png=candlestick_png)

    def _save_email_copy(self, subject, body):
        """
        保存邮件副本到本地文件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
        """
        try:
            # 生成文件名：日期_时间_主题前30字符
            current_time = datetime.now()
            date_str = current_time.strftime("%Y%m%d")
            time_str = current_time.strftime("%H%M%S")
            # 清理主题中的非法文件名字符
            clean_subject = "".join(
                c if c.isalnum() or c in " _-" else "_" for c in subject
            )
            clean_subject = clean_subject[:50]  # 限制长度

            filename = f"{date_str}_{time_str}_{clean_subject}.html"
            filepath = self.email_archive_dir / filename

            # 直接保存正文（已经是完整 HTML 文档，由 email_template.html 渲染）
            # 仅插入元数据注释，不嵌套 <html>
            meta_comment = (
                f"<!-- 主题: {subject} | "
                f"发送时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"收件人: {self.receiver_email} -->\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(meta_comment)
                f.write(body)

            logger.info(f"邮件副本已保存: {filepath}")
            return filepath

        except Exception as e:
            logger.error(
                "保存邮件副本失败: %s, 目标目录: %s", e, self.email_archive_dir
            )
            return None

    # ── 日报 PDF 生成 ──

    def _chart_deviation_timeline(
        self,
        signal_scan,
        backtest,
        base64=True,
    ) -> str:
        """
        偏离度 30 日折线图: 取偏离绝对值最大的 5 只标的 + 触发信号的标的，
        叠加折线。虚线标注买入阈值。

        Returns:
            base64 PNG 字符串 或 HTML <img> 标签
        """
        import io
        import base64
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ..utils.font_setup import setup_cjk_font

        setup_cjk_font()

        snapshot = getattr(signal_scan, "indicator_snapshot", {}) if signal_scan else {}
        if not snapshot:
            return ""

        # 取偏离度最大的 5 只
        dev_codes = []
        for code, vals in snapshot.items():
            d = vals.get("deviation", 0)
            dev_codes.append((code, abs(d), d))
        dev_codes.sort(key=lambda x: x[1], reverse=True)
        top5 = [c for c, _, _ in dev_codes[:5]]

        # 加触发信号的标的
        strategy_alerts = getattr(signal_scan, "alerts", []) if signal_scan else []
        for a in strategy_alerts:
            code = getattr(a, "stock_code", "")
            if code and code not in top5:
                top5.append(code)
        top5 = top5[:8]  # 最多 8 条线

        # 获取历史数据（需要 session._historical）
        # 这里只能从最近 60 天的历史中提取 deviation
        # 简化: 用 snapshot 做单点标注
        fig, ax = plt.subplots(figsize=(6.5, 2.2), dpi=120)
        colors = [
            "#2d8a56",
            "#c9a84c",
            "#2980b9",
            "#c0392b",
            "#8e44ad",
            "#e67e22",
            "#1abc9c",
            "#34495e",
        ]

        for i, code in enumerate(top5):
            vals = snapshot.get(code, {})
            d = vals.get("deviation", 0) * 100  # → %
            color = colors[i % len(colors)]
            ax.barh(i, d, color=color, height=0.5, alpha=0.85)
            label = f"{code[-4:]} {d:+.1f}%"
            x_pos = d + (0.5 if d >= 0 else -0.5)
            ha = "left" if d >= 0 else "right"
            ax.text(
                x_pos,
                i,
                label,
                va="center",
                ha=ha,
                fontsize=7,
                color=color,
                fontweight="bold",
            )

        # 买入阈值虚线
        ax.axvline(x=-0.5, color="#888", linestyle="--", linewidth=0.6, alpha=0.5)
        ax.text(
            -0.5,
            len(top5) - 0.3,
            " 买入阈值 -0.5%",
            fontsize=6,
            color="#888",
            va="bottom",
        )

        ax.set_yticks(range(len(top5)))
        ax.set_yticklabels([c[-4:] for c in top5], fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel("偏离度 %", fontsize=7)
        ax.axvline(x=0, color="#ccc", linewidth=0.5)
        ax.grid(axis="x", alpha=0.2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        plt.tight_layout(pad=0.5)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)

        if base64:
            b64 = base64.b64encode(buf.read()).decode()
            return f'<img src="data:image/png;base64,{b64}" style="max-width:100%"/>'
        return buf.read()
    def _generate_daily_pdf(
        self,
        session,
        alert_stocks,
        signal_scan,
        backtest,
        stock_data,
    ) -> bytes | None:
        """Delegate to daily_pdf.generate_daily_pdf, injecting the helpers."""
        return generate_daily_pdf(
            session,
            alert_stocks,
            signal_scan,
            backtest,
            stock_data,
            chart_deviation_timeline=self._chart_deviation_timeline,
            pick_best_anchor=self._pick_best_anchor,
            get_server_info=self._get_server_info,
        )
