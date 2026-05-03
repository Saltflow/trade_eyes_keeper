"""
邮件通知模块
发送股票提醒邮件
"""

import logging
import smtplib
import ssl
import pandas as pd
import socket
import platform
import subprocess
from html import escape
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email import policy
from datetime import datetime
from pathlib import Path

from .chart_generator import generate_combined_chart

logger = logging.getLogger(__name__)


class EmailNotifier:
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
            from src.health_server.core.global_instances import set_report_token_timeout
            set_report_token_timeout(timeout)
        except Exception:
            pass

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
            analysis_results = session.analysis_results
            announcements = session.announcements
            financial_analysis_results = session.financial_analysis_results
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
            subject = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果
            portfolio_results = getattr(session, "portfolio_results", None)

            # 生成投资组合走势图（两张: A股 / 非A股）
            portfolio_chart_dict = None
            if portfolio_results:
                try:
                    from src.analysis.portfolio_strategy import generate_portfolio_chart
                    bw = self.config.get("portfolio_strategy", {}).get(
                        "bollinger_window", 90
                    )
                    portfolio_chart_dict = generate_portfolio_chart(
                        portfolio_results, bollinger_window=bw
                    )
                    n_charts = len(portfolio_chart_dict) if portfolio_chart_dict else 0
                    logger.info(f"投资组合图表生成: {n_charts}张" if n_charts else "投资组合图表跳过")
                except Exception as e:
                    logger.error(f"投资组合图表生成失败: {e}")

            # 获取策略信号扫描结果
            signal_scan = getattr(session, "signal_scan", None)
            backtest = getattr(session, "backtest", None)

            # 生成日报 PDF 附件
            pdf_bytes = None
            try:
                pdf_bytes = self._generate_daily_pdf(
                    session, alert_stocks, signal_scan, backtest, stock_data,
                )
                if pdf_bytes:
                    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
            except Exception as e:
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（精简正文）
            body = self._build_email_body(
                alert_stocks,
                stock_data,
                analysis_results,
                announcements,
                financial_analysis_results,
                historical_data=historical_data,
                chart_png_bytes=chart_png_bytes,
                portfolio_results=portfolio_results,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
            )

            # 发送邮件（PDF 作为附件）
            self._send_email(subject, body, chart_png_bytes=chart_png_bytes, portfolio_chart_dict=portfolio_chart_dict, pdf_bytes=pdf_bytes)

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
            analysis_results = session.analysis_results
            announcements = session.announcements
            financial_analysis_results = session.financial_analysis_results
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"

            # 获取投资组合策略结果
            portfolio_results = getattr(session, "portfolio_results", None)

            # 生成投资组合走势图（两张）
            portfolio_chart_dict = None
            if portfolio_results:
                try:
                    from src.analysis.portfolio_strategy import generate_portfolio_chart
                    bw = self.config.get("portfolio_strategy", {}).get(
                        "bollinger_window", 90
                    )
                    portfolio_chart_dict = generate_portfolio_chart(
                        portfolio_results, bollinger_window=bw
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
                    session, [], signal_scan, backtest, stock_data,
                )
            except Exception as e:
                logger.warning("日报 PDF 生成失败: %s", e)

            # 构建邮件内容（使用空警报列表）
            body = self._build_email_body(
                [],
                stock_data,
                analysis_results,
                announcements,
                financial_analysis_results,
                historical_data=historical_data,
                portfolio_results=portfolio_results,
                portfolio_chart_dict=portfolio_chart_dict,
                signal_scan=signal_scan,
                backtest=backtest,
            )

            # 发送邮件
            self._send_email(subject, body, portfolio_chart_dict=portfolio_chart_dict, pdf_bytes=pdf_bytes)

            logger.info(f"每日报告邮件任务完成 ({self.receiver_email}) (来自Session)")

        except Exception as e:
            logger.error(f"从Session发送每日报告邮件失败: {e}")

    def _build_financial_analysis_section(
        self, financial_analysis_results, analysis_results=None, stock_data=None
    ):
        """
        构建财报分析部分HTML

        Args:
            financial_analysis_results: 财报分析结果字典
            analysis_results: LLM分析结果字典（可选，用于整合展示）
            stock_data: 完整的股票数据DataFrame（用于获取股票名称）

        Returns:
            str: HTML格式的财报分析部分
        """
        if not financial_analysis_results:
            return "<h3>财报分析</h3><p>暂无可用财报分析数据（可能无新财报或获取失败）。</p>"

        logger.info(
            f"构建财报分析部分: 收到{len(financial_analysis_results)}只股票的分析结果"
        )

        html = """
            <h3>财报分析</h3>
            <p>基于最新财报的结构化摘要（按股票展示最近2份）：</p>
        """

        for stock_code, reports in financial_analysis_results.items():
            if not reports:
                logger.info(f"股票{stock_code}没有分析报告，跳过")
                continue

            # 从stock_data中查找股票名称
            stock_name = stock_code
            if stock_data is not None:
                stock_row = stock_data[stock_data["stock_code"] == stock_code]
                if not stock_row.empty:
                    stock_name = stock_row.iloc[0].get("stock_name", stock_code)
            logger.info(
                f"处理股票{stock_code}({stock_name})的财报分析，共{len(reports)}份报告"
            )

            # 限制显示的报告数量，显示前2份报告
            max_reports = min(2, len(reports))
            selected_reports = reports[:max_reports]

            for report in selected_reports:
                report_type = str(report.get("report_type", "未知"))
                period_date = str(report.get("period_date", ""))
                analysis = report.get("analysis", {}) or {}
                numeric_fields = report.get("numeric_fields_detected")
                short_circuited = report.get("short_circuited", False)
                success = report.get("success", True)

                # 优先展示包含估值/综合判断的关键字段，最多3项
                analysis_fields = [
                    ("overall_assessment", "总体评估"),
                    ("liquidation_value", "DCF估值"),
                    ("profit_changes", "利润变化"),
                    ("cost_structure", "成本结构"),
                    ("audit_risks", "审计风险"),
                ]
                selected_fields = analysis_fields[: min(3, len(analysis_fields))]

                analysis_items = []
                for field_key, field_name in selected_fields:
                    value = analysis.get(field_key)
                    if value:
                        value_str = escape(str(value))
                        analysis_items.append(
                            f"<li><strong>{field_name}：</strong>{value_str}</li>"
                        )

                if analysis_items:
                    analysis_html = "<ul>" + "".join(analysis_items) + "</ul>"
                else:
                    analysis_html = "<p>无分析数据</p>"

                status_badge = ""
                if not success:
                    status_badge = "<span style='color:#e53935;'>未完成</span>"
                    if short_circuited:
                        status_badge += " · 短路"

                numeric_badge = (
                    f"<span style='font-size:0.85em; color:#666;'>数值字段数: {numeric_fields}</span>"
                    if numeric_fields is not None
                    else ""
                )

                html += f"""
                <div style=\"border: 1px solid #ddd; padding: 12px; margin: 10px 0; border-radius: 6px;\">
                    <div style=\"display: flex; justify-content: space-between; align-items: center;\">
                        <h4 style=\"margin: 0;\">{escape(str(stock_code))} {escape(str(stock_name))}</h4>
                        <div style=\"font-size: 0.9em; color: #666;\">{escape(report_type)} · {escape(period_date)} {status_badge}</div>
                    </div>
                    <div style=\"margin-top: 6px;\">{numeric_badge}</div>
                    <div style=\"margin-top: 8px;\">{analysis_html}</div>
                </div>
                """

        html += "<p><em>注：财报分析基于最新财务报告，数据仅供参考。</em></p>"

        return html

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
                code = a.stock_code if hasattr(a, "stock_code") else a.get("stock_code", "?")
                label = a.rule_label if hasattr(a, "rule_label") else a.get("rule_label", "?")
                cond = a.condition_str if hasattr(a, "condition_str") else a.get("condition", "?")
                cv = a.current_value if hasattr(a, "current_value") else a.get("current_value", "-")
                rank = a.strategy_rank if hasattr(a, "strategy_rank") else a.get("strategy_rank", "?")
                html += (
                    f"<tr>"
                    f"<td>{code}</td>"
                    f"<td>{label}</td>"
                    f"<td style='font-size:11px'>{cond[:60]}</td>"
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
                label = {"rsi": "RSI", "vol_ratio": "量比", "boll_pct_b": "布林%B",
                         "adx": "ADX", "macd_hist": "MACD柱", "deviation": "偏差%",
                         "atr": "ATR"}.get(ind, ind)
                header += f"<th>{label}</th>"
            header += "</tr>\n"
            html += header

            # 按标的在报警池内优先排序
            consensus_stocks = set(consensus.consensus_stocks or [])
            sorted_codes = sorted(snapshot.keys(),
                                  key=lambda c: (0 if c in strategy_codes else
                                                 1 if c in consensus_stocks else 2))

            for code in sorted_codes[:20]:
                vals = snapshot.get(code, {})
                html += f"<tr><td>{code}</td>"
                for ind in ind_cols:
                    v = vals.get(ind)
                    if v is not None:
                        if ind == "deviation":
                            html += f"<td>{v*100:.1f}%</td>"
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
            return ""

        html = "<h3>历史回测</h3>\n"
        group_labels = {"a_share": "A股", "non_a_share": "境外"}

        for group, bt in backtest.items():
            if not bt:
                continue
            label = group_labels.get(group, group)
            html += f"<p><strong>{label} — 基于最新优化策略 (Rank {bt.get('strategy_rank','?')})</strong></p>\n"
            html += '<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:12px" cellpadding="6" cellspacing="0" border="0">\n'
            html += '<tr style="background:#34495e;color:#fff"><th>指标</th><th>全期</th><th>观察0-6m</th><th>部署6-12m</th><th>验证12-24m</th></tr>\n'

            phases = bt.get("phase_metrics", {})
            total = f"{bt.get('total_return', 0):+.1f}%"
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

            html += (f"<tr><td>超额收益</td><td>{total}</td>"
                     f"<td>{_pval(phases.get('observe'), 'excess_return')}</td>"
                     f"<td>{_pval(phases.get('deploy'), 'excess_return')}</td>"
                     f"<td>{_pval(phases.get('test'), 'excess_return')}</td></tr>\n")
            html += (f"<tr><td>最大回撤</td><td>{dd}</td>"
                     f"<td>{_pval(phases.get('observe'), 'max_drawdown')}</td>"
                     f"<td>{_pval(phases.get('deploy'), 'max_drawdown')}</td>"
                     f"<td>{_pval(phases.get('test'), 'max_drawdown')}</td></tr>\n")
            html += (f"<tr><td>Sharpe</td><td>{sp}</td>"
                     f"<td>{_pval(phases.get('observe'), 'sharpe_ratio')}</td>"
                     f"<td>{_pval(phases.get('deploy'), 'sharpe_ratio')}</td>"
                     f"<td>{_pval(phases.get('test'), 'sharpe_ratio')}</td></tr>\n")
            html += (f"<tr><td>交易次数</td><td>{trades}</td>"
                     f"<td>{getattr(phases.get('observe'), 'trade_count', '-')}</td>"
                     f"<td>{getattr(phases.get('deploy'), 'trade_count', '-')}</td>"
                     f"<td>{getattr(phases.get('test'), 'trade_count', '-')}</td></tr>\n")

            # 基准对比
            bm = bt.get("benchmarks", {})
            if bm:
                test_excess = getattr(phases.get("test"), "excess_return", 0)
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
                html += (f"<p style='font-size:12px;color:#888'>入选标的: "
                         f"{', '.join(stocks[:8])}"
                         f"{' +' + str(len(stocks)-8) if len(stocks)>8 else ''}</p>\n")

        return html

    def _build_portfolio_section(self, portfolio_results, portfolio_chart_dict=None):
        """
        构建投资组合策略分析部分HTML

        Args:
            portfolio_results: PortfolioOptimizer.run()返回的结果字典
                {
                    "a_share": {
                        "max_return": PortfolioResult,
                        "min_drawdown": PortfolioResult,
                        "max_sharpe": PortfolioResult
                    },
                    "non_a_share": { ... }
                }

        Returns:
            str: HTML格式的投资组合策略分析部分
        """
        if not portfolio_results:
            return ""

        logger.info("构建投资组合策略分析部分")

        group_labels = {
            "a_share": "A股组合",
            "non_a_share": "非A股组合（港股/美股/新加坡）",
        }
        metric_labels = {
            "max_return": "最高收益",
            "min_drawdown": "最小回撤",
            "max_sharpe": "最优夏普",
        }

        html = """
        <div style="margin-top: 40px; border-top: 1px solid #ddd; padding-top: 20px;">
            <h3>投资组合预期回报</h3>
            <p style="color: #666; font-size: 14px;">基于MA60锚点择时策略，搜索最优投资组合（月度买入/卖出各限15000元）</p>
        """

        for group_key, group_label in group_labels.items():
            group_data = portfolio_results.get(group_key)
            if not group_data:
                continue

            html += f"""
            <div style="margin-top: 25px;">
                <h4 style="color: #333; border-left: 4px solid #2196f3; padding-left: 10px;">{group_label}</h4>
            """

            for metric_key, metric_label in metric_labels.items():
                result = group_data.get(metric_key)
                if not result:
                    continue

                # PortfolioResult is a dataclass, use attribute access
                total_return = (
                    result.total_return if hasattr(result, "total_return") else 0
                )
                max_drawdown = (
                    result.max_drawdown if hasattr(result, "max_drawdown") else 0
                )
                sharpe_ratio = (
                    result.sharpe_ratio if hasattr(result, "sharpe_ratio") else 0
                )
                expected_position = (
                    result.expected_position
                    if hasattr(result, "expected_position")
                    else 0
                )
                trade_count = (
                    result.trade_count if hasattr(result, "trade_count") else 0
                )
                composition = (
                    result.composition if hasattr(result, "composition") else []
                )
                details = (
                    result.stock_details if hasattr(result, "stock_details") else []
                )

                # 构造组合成分字符串
                composition_str = ", ".join(composition) if composition else "无"
                # 各股详情（简要展示前5只）
                details_html = ""
                if details:
                    details_html = "<ul style='margin: 5px 0; padding-left: 20px; font-size: 12px;'>"
                    for d in details[:5]:
                        d_return = (
                            d.get("total_return", 0)
                            if isinstance(d, dict)
                            else getattr(d, "total_return", 0)
                        )
                        d_sharpe = (
                            d.get("sharpe_ratio", 0)
                            if isinstance(d, dict)
                            else getattr(d, "sharpe_ratio", 0)
                        )
                        d_trades = (
                            d.get("trades", 0)
                            if isinstance(d, dict)
                            else getattr(d, "total_trades", 0)
                        )
                        d_code = (
                            d.get("stock_code", "")
                            if isinstance(d, dict)
                            else getattr(d, "stock_code", "")
                        )
                        ret_color = "green" if d_return >= 0 else "red"
                        details_html += (
                            f"<li>{d_code}: "
                            f"收益率 <span style='color:{ret_color};'>{d_return:+.2f}%</span>, "
                            f"夏普 {d_sharpe:.2f}, "
                            f"交易 {d_trades}次"
                            f"</li>"
                        )
                    details_html += "</ul>"

                return_color = "green" if total_return >= 0 else "red"
                dd_color = "red" if max_drawdown < 0 else "green"

                html += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 6px; background-color: #fafafa;">
                    <h5 style="margin: 0 0 10px 0; color: #1565c0;">{metric_label}</h5>
                    <table style="border-collapse: collapse; width: 100%; font-size: 13px;">
                        <tr>
                            <td style="padding: 4px 8px; width: 25%;"><strong>组合收益率</strong></td>
                            <td style="padding: 4px 8px; color: {return_color};">{total_return:+.2f}%</td>
                            <td style="padding: 4px 8px; width: 25%;"><strong>最大回撤</strong></td>
                            <td style="padding: 4px 8px; color: {dd_color};">{max_drawdown:+.2f}%</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 8px;"><strong>夏普比率</strong></td>
                            <td style="padding: 4px 8px;">{sharpe_ratio:.2f}</td>
                            <td style="padding: 4px 8px;"><strong>期末持仓市值</strong></td>
                            <td style="padding: 4px 8px;">{expected_position:,.2f}元</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 8px;"><strong>交易次数</strong></td>
                            <td style="padding: 4px 8px;">{trade_count}</td>
                            <td style="padding: 4px 8px;"><strong>成分股数</strong></td>
                            <td style="padding: 4px 8px;">{len(composition)}</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 8px; vertical-align: top;"><strong>成分股</strong></td>
                            <td style="padding: 4px 8px;" colspan="3">{composition_str}</td>
                        </tr>
                    </table>
                    {details_html}
                </div>
                """

            html += "</div>"

        html += """
            <p style="color: #888; font-size: 12px; margin-top: 15px;">
                <strong>策略说明：</strong>以MA60为锚点，价格跌破-5%/-10%时分批买入（每笔≤5000元），
                突破+5%/+10%/+15%时分批卖出1/4持仓（每笔≤10000元，低于2500元清仓）。
                 月度买入/卖出各限15000元（组合级）。A股无风险利率2%，非A股4.5%。
                 初始资金每组10万元（组合内标的共享）。
            </p>
        </div>
        """

        return html

    def _build_email_body(
        self,
        alert_stocks,
        stock_data,
        analysis_results=None,
        announcements=None,
        financial_analysis_results=None,
        historical_data=None,
        chart_png_bytes=None,
        portfolio_results=None,
        portfolio_chart_dict=None,
        signal_scan=None,
        backtest=None,
    ):
        """
         构建邮件正文（完整版：表格 + LLM分析 + 公告 + 财报分析 + 图表）

        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）
            financial_analysis_results: 财报分析结果字典（可选）
            historical_data: 完整历史DataFrame字典 stock_code → DataFrame（可选，供图表使用）
            chart_png_bytes: 图表 PNG 原始字节（可选），有值时用 cid:chart001 嵌入

        Returns:
            str: 邮件正文（HTML格式）
        """
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
                earnings_growth = None
                pe_ratio = None
                pb_ratio = None
                roe = None
                debt_ratio = None

                if not stock_row.empty:
                    dividend_per_share = stock_row.iloc[0].get("dividend_per_share")
                    dividend_yield = stock_row.iloc[0].get("dividend_yield")
                    earnings_growth = stock_row.iloc[0].get("earnings_growth")
                    pe_ratio = stock_row.iloc[0].get("pe_ratio")
                    pb_ratio = stock_row.iloc[0].get("pb_ratio")
                    roe = stock_row.iloc[0].get("roe")
                    debt_ratio = stock_row.iloc[0].get("debt_ratio")

                # 格式化基本面数据
                dividend_per_share_str = (
                    f"{dividend_per_share:.3f}"
                    if dividend_per_share is not None
                    and not pd.isna(dividend_per_share)
                    else "-"
                )
                dividend_yield_str = (
                    f"{dividend_yield:.2f}%"
                    if dividend_yield is not None and not pd.isna(dividend_yield)
                    else "-"
                )
                earnings_growth_str = (
                    f"{earnings_growth:+.2f}%"
                    if earnings_growth is not None and not pd.isna(earnings_growth)
                    else "-"
                )
                pe_ratio_str = (
                    f"{pe_ratio:.2f}"
                    if pe_ratio is not None and not pd.isna(pe_ratio)
                    else "-"
                )
                pb_ratio_str = (
                    f"{pb_ratio:.2f}"
                    if pb_ratio is not None and not pd.isna(pb_ratio)
                    else "-"
                )
                roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "-"
                debt_ratio_str = (
                    f"{debt_ratio:.2f}%"
                    if debt_ratio is not None and not pd.isna(debt_ratio)
                    else "-"
                )

                # 确定颜色类（安全处理None值）
                close_diff_class = (
                    "positive"
                    if close_ma60_diff is not None and close_ma60_diff >= 0
                    else "negative"
                    if close_ma60_diff is not None
                    else ""
                )
                close_pct_class = (
                    "positive"
                    if close_ma60_pct is not None and close_ma60_pct >= 0
                    else "negative"
                    if close_ma60_pct is not None
                    else ""
                )
                earnings_growth_class = (
                    "positive"
                    if earnings_growth is not None and earnings_growth > 0
                    else "negative"
                    if earnings_growth is not None and earnings_growth < 0
                    else ""
                )

                # 格式化技术指标数据（安全处理None值）
                low_price_str = (
                    f"{low_price:.2f}"
                    if low_price is not None and not pd.isna(low_price)
                    else "-"
                )
                ma60_str = (
                    f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "-"
                )
                close_price_str = (
                    f"{close_price:.2f}"
                    if close_price is not None and not pd.isna(close_price)
                    else "-"
                )
                close_ma60_diff_str = (
                    f"{close_ma60_diff:+.2f}" if close_ma60_diff is not None else "-"
                )
                close_ma60_pct_str = (
                    f"{close_ma60_pct:+.2f}%" if close_ma60_pct is not None else "-"
                )
                low_ma60_diff_str = (
                    f"{low_ma60_diff:.2f}" if low_ma60_diff is not None else "-"
                )
                low_ma60_pct_str = (
                    f"{low_ma60_pct:.2f}%" if low_ma60_pct is not None else "-"
                )

                # 技术指标行
                alert_rows_technical += f"""
                    <tr class="alert-row">
                        <td>{stock_code}</td>
                        <td>{stock_name}</td>
                        <td>{low_price_str}</td>
                        <td>{ma60_str}</td>
                        <td>{close_price_str}</td>
                        <td class="{close_diff_class}">{close_ma60_diff_str}</td>
                        <td class="{close_pct_class}">{close_ma60_pct_str}</td>
                        <td class="positive">{low_ma60_diff_str}</td>
                        <td class="positive">{low_ma60_pct_str}</td>
                        <td>最低价 &lt; MA60</td>
                    </tr>
                """

                # 基本面指标行（去重：同一股票只加一次）
                if stock_code and stock_code not in seen_fundamental:
                    alert_rows_fundamental += f"""
                    <tr class="alert-row">
                        <td>{stock_code}</td>
                        <td>{stock_name}</td>
                        <td>{dividend_per_share_str}</td>
                        <td>{dividend_yield_str}</td>
                        <td class="{earnings_growth_class}">{earnings_growth_str}</td>
                        <td>{pe_ratio_str}</td>
                        <td>{pb_ratio_str}</td>
                        <td>{roe_str}</td>
                        <td>{debt_ratio_str}</td>
                    </tr>
                """
                    seen_fundamental.add(stock_code)

        # 3. 构建所有监控股票行（拆分为价格技术指标和基本面指标）
        all_rows_price = ""
        all_rows_fundamental = ""
        for _, row in stock_data.iterrows():
            stock_code = row.get("stock_code", "")
            stock_name = row.get("stock_name", stock_code)
            open_price = row.get("open", 0)
            close_price = row.get("close", 0)
            high_price = row.get("high")
            low_price = row.get("low")
            ma60 = row.get("ma60")

            # 计算收盘价与MA60差值（仅在数据有效时计算）
            if (
                close_price is not None
                and ma60 is not None
                and not pd.isna(close_price)
                and not pd.isna(ma60)
            ):
                close_ma60_diff = close_price - ma60
                close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0
                diff_class = "positive" if close_ma60_diff >= 0 else "negative"
                pct_class = "positive" if close_ma60_pct >= 0 else "negative"
            else:
                close_ma60_diff = None
                close_ma60_pct = None
                diff_class = ""
                pct_class = ""

            # 获取基本面数据
            dividend_per_share = row.get("dividend_per_share")
            dividend_yield = row.get("dividend_yield")
            earnings_growth = row.get("earnings_growth")
            pe_ratio = row.get("pe_ratio")
            pb_ratio = row.get("pb_ratio")
            roe = row.get("roe")
            debt_ratio = row.get("debt_ratio")

            # 格式化基本面数据
            dividend_per_share_str = (
                f"{dividend_per_share:.3f}"
                if dividend_per_share is not None and not pd.isna(dividend_per_share)
                else "-"
            )
            dividend_yield_str = (
                f"{dividend_yield:.2f}%"
                if dividend_yield is not None and not pd.isna(dividend_yield)
                else "-"
            )
            earnings_growth_str = (
                f"{earnings_growth:+.2f}%"
                if earnings_growth is not None and not pd.isna(earnings_growth)
                else "-"
            )
            pe_ratio_str = (
                f"{pe_ratio:.2f}"
                if pe_ratio is not None and not pd.isna(pe_ratio)
                else "-"
            )
            pb_ratio_str = (
                f"{pb_ratio:.2f}"
                if pb_ratio is not None and not pd.isna(pb_ratio)
                else "-"
            )
            roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "-"
            debt_ratio_str = (
                f"{debt_ratio:.2f}%"
                if debt_ratio is not None and not pd.isna(debt_ratio)
                else "-"
            )

            # 确定颜色类
            earnings_growth_class = (
                "positive"
                if earnings_growth is not None and earnings_growth > 0
                else "negative"
                if earnings_growth is not None and earnings_growth < 0
                else ""
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
                else "-"
            )
            close_price_str = (
                f"{close_price:.2f}"
                if close_price is not None and not pd.isna(close_price)
                else "-"
            )
            high_price_str = (
                f"{high_price:.2f}"
                if high_price is not None and not pd.isna(high_price)
                else "-"
            )
            low_price_str = (
                f"{low_price:.2f}"
                if low_price is not None and not pd.isna(low_price)
                else "-"
            )
            ma60_str = f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "-"
            close_ma60_diff_str = (
                f"{close_ma60_diff:+.2f}" if close_ma60_diff is not None else "-"
            )
            close_ma60_pct_str = (
                f"{close_ma60_pct:+.2f}%" if close_ma60_pct is not None else "-"
            )

            # 价格技术指标行 (inline styles for email clients)
            pos = "color:#27ae60;text-align:right"
            neg = "color:#c0392b;text-align:right"
            neut = "text-align:right"
            diff_style = pos if close_ma60_diff is not None and close_ma60_diff >= 0 else neg if close_ma60_diff is not None else neut
            pct_style = pos if close_ma60_pct is not None and close_ma60_pct >= 0 else neg if close_ma60_pct is not None else neut
            all_rows_price += (
                f'<tr>'
                f'<td>{stock_code}</td>'
                f'<td style="{neut}">{open_price_str}</td>'
                f'<td style="{neut}">{close_price_str}</td>'
                f'<td style="{neut}">{high_price_str}</td>'
                f'<td style="{neut}">{low_price_str}</td>'
                f'<td style="{neut}">{ma60_str}</td>'
                f'<td style="{diff_style}">{close_ma60_diff_str}</td>'
                f'<td style="{pct_style}">{close_ma60_pct_str}</td>'
                f'<td>{status}</td>'
                f'</tr>'
            )

            # 基本面指标行
            eg_style = (
                "color:#27ae60;text-align:right" if earnings_growth is not None and earnings_growth > 0
                else "color:#c0392b;text-align:right" if earnings_growth is not None and earnings_growth < 0
                else "text-align:right"
            )
            all_rows_fundamental += (
                f'<tr>'
                f'<td>{stock_code}</td>'
                f'<td style="{neut}">{dividend_per_share_str}</td>'
                f'<td style="{neut}">{dividend_yield_str}</td>'
                f'<td style="{eg_style}">{earnings_growth_str}</td>'
                f'<td style="{neut}">{pe_ratio_str}</td>'
                f'<td style="{neut}">{pb_ratio_str}</td>'
                f'<td style="{neut}">{roe_str}</td>'
                f'<td style="{neut}">{debt_ratio_str}</td>'
                f'</tr>'
            )

        # 4. 构建LLM分析部分
        llm_analysis_section = ""
        if analysis_results and len(analysis_results) > 0:
            llm_analysis_section = """
            <h3>LLM基本面分析</h3>
            """
            for stock_code, analysis in analysis_results.items():
                # 从stock_data管道获取股票名称
                stock_name = stock_code
                if stock_data is not None:
                    match_s = stock_data[stock_data["stock_code"] == stock_code]
                    if not match_s.empty:
                        stock_name = match_s.iloc[0].get("stock_name", stock_code)

                # 提取分析文本
                analysis_text = analysis.get("analysis_text", "")
                summary = analysis.get("summary", {})

                # 截断过长的分析文本（增加到2000字符以避免过度截断）
                if len(analysis_text) > 2000:
                    analysis_text = analysis_text[:2000] + "... (分析内容过长，已截断)"

                # 构建分析卡片
                sentiment = summary.get("sentiment", "中性")
                sentiment_color = (
                    "#4caf50"
                    if sentiment == "积极"
                    else "#f44336"
                    if sentiment == "谨慎"
                    else "#ff9800"
                )

                # 检查是否有结构化摘要
                structured_summary = analysis.get("structured_summary")

                if structured_summary:
                    # 使用结构化摘要显示
                    sustainability_score = structured_summary.get(
                        "sustainability_score", 3
                    )
                    stability_score = structured_summary.get("stability_score", 3)
                    overall_rating = structured_summary.get("overall_rating", 3)
                    key_factors = structured_summary.get("key_factors", [])
                    major_risks = structured_summary.get("major_risks", [])
                    investment_recommendation = structured_summary.get(
                        "investment_recommendation", ""
                    )

                    # 将Markdown格式的投资建议转换为HTML
                    investment_recommendation_html = self._markdown_to_html(
                        investment_recommendation
                    )

                    # 分数颜色（1-2分红色，3分橙色，4-5分绿色）
                    def get_score_color(score):
                        if score >= 4:
                            return "#4caf50"  # 绿色
                        elif score == 3:
                            return "#ff9800"  # 橙色
                        else:
                            return "#f44336"  # 红色

                    sustainability_color = get_score_color(sustainability_score)
                    stability_color = get_score_color(stability_score)
                    overall_color = get_score_color(overall_rating)

                    # 构建关键因素HTML
                    key_factors_html = ""
                    if key_factors:
                        key_factors_html = "<ul>"
                        for factor in key_factors[:5]:  # 最多显示5个
                            key_factors_html += f"<li>{factor}</li>"
                        key_factors_html += "</ul>"
                    else:
                        key_factors_html = "<p>无关键因素信息</p>"

                    # 构建主要风险HTML
                    major_risks_html = ""
                    if major_risks:
                        major_risks_html = "<ul>"
                        for risk in major_risks[:5]:  # 最多显示5个
                            major_risks_html += f"<li>{risk}</li>"
                        major_risks_html += "</ul>"
                    else:
                        major_risks_html = "<p>无明确风险信息</p>"

                    llm_analysis_section += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px;">
                    <h4>{stock_code} {stock_name} <span style="color: {sentiment_color}; font-weight: bold;">[{sentiment}]</span></h4>
                    
                    <div style="display: flex; justify-content: space-between; margin-bottom: 15px;">
                        <div style="text-align: center; padding: 10px; border-radius: 5px; background-color: #f5f5f5; flex: 1; margin: 0 5px;">
                            <h5 style="margin: 0 0 5px 0; color: #666;">分红可持续性</h5>
                            <div style="font-size: 24px; font-weight: bold; color: {sustainability_color};">{sustainability_score}/5</div>
                        </div>
                        <div style="text-align: center; padding: 10px; border-radius: 5px; background-color: #f5f5f5; flex: 1; margin: 0 5px;">
                            <h5 style="margin: 0 0 5px 0; color: #666;">股价稳定性</h5>
                            <div style="font-size: 24px; font-weight: bold; color: {stability_color};">{stability_score}/5</div>
                        </div>
                        <div style="text-align: center; padding: 10px; border-radius: 5px; background-color: #f5f5f5; flex: 1; margin: 0 5px;">
                            <h5 style="margin: 0 0 5px 0; color: #666;">总体评级</h5>
                            <div style="font-size: 24px; font-weight: bold; color: {overall_color};">{overall_rating}/5</div>
                        </div>
                    </div>
                    
                    <div style="margin-bottom: 15px;">
                        <h5 style="margin: 0 0 5px 0; color: #666;">关键影响因素</h5>
                        {key_factors_html}
                    </div>
                    
                    <div style="margin-bottom: 15px;">
                        <h5 style="margin: 0 0 5px 0; color: #666;">主要风险</h5>
                        {major_risks_html}
                    </div>
                    
                    {f'<div style="margin-bottom: 15px;"><h5 style="margin: 0 0 5px 0; color: #666;">投资建议</h5><div>{investment_recommendation_html}</div></div>' if investment_recommendation else ""}
                    
                    <p style="margin-top: 10px; font-size: 0.9em; color: #999;"><em>注：LLM分析仅供参考，不构成投资建议。分数基于分红可持续性和股价稳定性分析。</em></p>
                </div>
                """
                else:
                    # 使用旧的简单摘要显示（向后兼容）
                    llm_analysis_section += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px;">
                    <h4>{stock_code} {stock_name} <span style="color: {sentiment_color}; font-weight: bold;">[{sentiment}]</span></h4>
                    <p><strong>关键指标:</strong></p>
                    <ul>
                        <li>增长潜力: {"有" if summary.get("has_growth", False) else "无"}</li>
                        <li>分红情况: {"有" if summary.get("has_dividend", False) else "无"}</li>
                        <li>风险提示: {"有" if summary.get("has_risk", False) else "无"}</li>
                    </ul>
                    <p><em>注：LLM分析仅供参考，不构成投资建议。</em></p>
                </div>
                """

        # 5. 构建财报分析部分
        financial_analysis_section = self._build_financial_analysis_section(
            financial_analysis_results, analysis_results, stock_data
        )

        # 6. 构建公告部分
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

        # 6b. 构建投资组合策略分析部分
        portfolio_section = ""
        if portfolio_results:
            portfolio_section = self._build_portfolio_section(portfolio_results, portfolio_chart_dict)

        # 7. 走势图表（由调用方生成，通过 chart_png_bytes 传入，使用 CID 内嵌）
        chart_section = ""
        if chart_png_bytes:
            chart_section = """
            <h3>价格走势图</h3>
            <p>近2个月最低价与最长告警锚点移动平均线：</p>
            <div style="text-align: center; margin: 20px 0;">
                <img src="cid:chart001"
                     alt="价格走势图"
                     style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" />
            </div>
            """

        # 7b. 投资组合走势图（紧贴报警图后面，chart002=A股, chart003=非A股）
        portfolio_chart_section = ""
        if portfolio_chart_dict:
            cid_map = {"a_share": "chart002", "non_a_share": "chart003"}
            group_titles = {"a_share": "A股投资组合净值走势", "non_a_share": "非A股投资组合净值走势"}
            for group_key in ("a_share", "non_a_share"):
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

        # 7b. 策略信号报警（基于优化器共识）
        strategy_alert_section = self._build_strategy_alert_section(
            signal_scan, alert_stocks, stock_data
        ) if signal_scan else ""

        # 7c. 回测分析
        backtest_section = self._build_backtest_section(backtest) if backtest else ""

        # 8. 获取服务器信息
        server_info = self._get_server_info()

        # 7. 构建报警股票部分
        alert_section = ""
        is_multi_format = False  # 保存格式标志供后续使用
        if alert_stocks:
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
                        <th>业绩增长(%)</th>
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
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                nona_r = sorted(
                    optimizer_dir.glob("*_non_a_share_report.html"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if a_r or nona_r:
                    from src.health_server.core.global_instances import register_report_token

                    hc = self.config.get("health_server", {})
                    server_ip = hc.get("public_ip", "")
                    port = hc.get("port", 1933)
                    use_ssl = hc.get("ssl", False)

                    if not server_ip:
                        try:
                            import urllib.request
                            ip_url = hc.get(
                                "ip_detect_url", "https://ifconfig.me"
                            )
                            server_ip = (
                                urllib.request.urlopen(ip_url, timeout=5)
                                .read().decode("utf-8").strip()
                            )
                        except Exception:
                            fi = self._get_server_info().get("ip_address", "localhost")
                            for p in fi.replace("(优先)","").replace("(","").replace(")","").split(","):
                                s = p.strip().split()[0] if p.strip() else ""
                                if s and not s.startswith(("172.","10.","192.168.","127.")):
                                    server_ip = s; break
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
                            f'{label}</a> &nbsp;'
                        )
                    if links_html:
                        report_link = (
                            f'<tr><td style="padding:8px 16px;color:#7f8c8d;font-size:13px">'
                            f'交互报告: {links_html}'
                            f'<span style="font-size:11px">(30分钟)</span></td></tr>'
                        )
        except Exception:
            pass

        # 9. 替换主模板变量
        html_content = email_template.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            alert_section=alert_section,
            all_rows_price=all_rows_price,
            all_rows_fundamental=all_rows_fundamental,
            llm_analysis_section=llm_analysis_section,
            financial_analysis_section=financial_analysis_section,
            announcements_section=announcements_section,
            chart_section=chart_section,
            portfolio_chart_section=portfolio_chart_section,
            strategy_alert_section=strategy_alert_section,
            backtest_section=backtest_section,
            report_link=report_link,
            portfolio_section=portfolio_section,
            server_hostname=server_info["hostname"],
            server_ip=server_info["ip_address"],
            server_kernel=server_info["kernel_version"],
            server_system=server_info["system"],
            server_machine=server_info["machine"],
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
        earnings_growth = None
        pe_ratio = None
        pb_ratio = None
        roe = None
        debt_ratio = None

        if not stock_row.empty:
            dividend_per_share = stock_row.iloc[0].get("dividend_per_share")
            dividend_yield = stock_row.iloc[0].get("dividend_yield")
            earnings_growth = stock_row.iloc[0].get("earnings_growth")
            pe_ratio = stock_row.iloc[0].get("pe_ratio")
            pb_ratio = stock_row.iloc[0].get("pb_ratio")
            roe = stock_row.iloc[0].get("roe")
            debt_ratio = stock_row.iloc[0].get("debt_ratio")

        # 格式化基本面数据
        dividend_per_share_str = (
            f"{dividend_per_share:.3f}"
            if dividend_per_share is not None and not pd.isna(dividend_per_share)
            else "-"
        )
        dividend_yield_str = (
            f"{dividend_yield:.2f}%"
            if dividend_yield is not None and not pd.isna(dividend_yield)
            else "-"
        )
        earnings_growth_str = (
            f"{earnings_growth:+.2f}%"
            if earnings_growth is not None and not pd.isna(earnings_growth)
            else "-"
        )
        pe_ratio_str = (
            f"{pe_ratio:.2f}" if pe_ratio is not None and not pd.isna(pe_ratio) else "-"
        )
        pb_ratio_str = (
            f"{pb_ratio:.2f}" if pb_ratio is not None and not pd.isna(pb_ratio) else "-"
        )
        roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "-"
        debt_ratio_str = (
            f"{debt_ratio:.2f}%"
            if debt_ratio is not None and not pd.isna(debt_ratio)
            else "-"
        )

        # 确定颜色类
        earnings_growth_class = (
            "positive"
            if earnings_growth is not None and earnings_growth > 0
            else "negative"
            if earnings_growth is not None and earnings_growth < 0
            else ""
        )

        # 构建技术指标行
        condition = f"{anchor_name} 区间 {interval_label} (连续{consecutive_days}天)"
        price_str = f"{price:.2f}" if price is not None and not pd.isna(price) else "-"
        anchor_value_str = (
            f"{anchor_value:.2f}"
            if anchor_value is not None and not pd.isna(anchor_value)
            else "-"
        )
        price_diff_str = (
            f"{price_difference:+.2f}"
            if price_difference is not None and not pd.isna(price_difference)
            else "-"
        )
        pct_str = (
            f"{percentage:+.2f}%"
            if percentage is not None and not pd.isna(percentage)
            else "-"
        )

        technical_row = f"""
            <tr class="alert-row">
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
            <tr class="alert-row">
                <td>{stock_code}</td>
                <td>{stock_name}</td>
                <td>{dividend_per_share_str}</td>
                <td>{dividend_yield_str}</td>
                <td class="{earnings_growth_class}">{earnings_growth_str}</td>
                <td>{pe_ratio_str}</td>
                <td>{pb_ratio_str}</td>
                <td>{roe_str}</td>
                <td>{debt_ratio_str}</td>
            </tr>
        """

        return technical_row, fundamental_row

    def _markdown_to_html(self, text):
        """
        将Markdown文本转换为HTML

        Args:
            text: Markdown格式文本

        Returns:
            str: HTML格式文本
        """
        if not text:
            return ""

        try:
            # 尝试使用markdown库
            import markdown

            # 基本扩展，支持粗体、列表等
            html = markdown.markdown(text, extensions=["extra", "nl2br"])
            return html
        except ImportError:
            # 如果markdown库不可用，进行简单转换
            # 替换粗体语法：**text** -> <strong>text</strong>
            import re

            html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
            # 替换斜体语法：*text* -> <em>text</em>
            html = re.sub(r"\*(.*?)\*", r"<em>\1</em>", html)
            # 替换无序列表：* item -> <li>item</li>
            lines = html.split("\n")
            in_list = False
            result_lines = []
            for line in lines:
                if line.strip().startswith("* ") or line.strip().startswith("- "):
                    if not in_list:
                        result_lines.append("<ul>")
                        in_list = True
                    content = line.strip()[2:].strip()
                    result_lines.append(f"<li>{content}</li>")
                else:
                    if in_list:
                        result_lines.append("</ul>")
                        in_list = False
                    result_lines.append(line)
            if in_list:
                result_lines.append("</ul>")
            html = "\n".join(result_lines)
            return html

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
            except Exception:
                pass

            # 方法2: 通过hostname -I命令获取所有IP（Linux）
            try:
                result = subprocess.run(
                    ["hostname", "-I"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    ips = result.stdout.strip().split()
                    ip_list.extend(ips)
            except Exception:
                pass

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
            except Exception:
                pass

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
            except Exception:
                # 最后回退到platform.uname
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

    @staticmethod
    def _pick_best_anchor(
        close: float,
        anchors: dict[str, float | None],
    ) -> tuple[str, float, float] | None:
        """
        选择最优锚点：实际回溯最短 + 偏离率落入警报阈值区间。

        锚点优先级（回溯交易日短→长，越小越优先）:
            ma60(60天) > wma20(~100天) > wma30(~150天) > wma50(~250天)

        警报阈值区间 (来自 alerts.yaml thresholds):
            ≤ -10%, (-10%, -5%], (-5%, 0), [5%, 10%), [10%, 15%), ≥ 15%

        Args:
            close: 现价
            anchors: {"ma60": 5.91, "wma20": 5.78, ...}

        Returns:
            (anchor_name, anchor_value, deviation_pct) 或 None
        """
        # 窗口优先级（按实际交易日排序，数字越小越优先）
        #   ma60: 日线60个交易日 → 最短
        #   wma20: 周线≈100交易日 (20×5)
        #   wma30: 周线≈150交易日 (30×5)
        #   wma50: 周线≈250交易日 (50×5)
        WINDOW_PRIORITY = {
            "ma60": 60,
            "wma20": 100,
            "wma30": 150,
            "wma50": 250,
        }

        def _in_alert_range(dev: float) -> bool:
            if dev <= -10.0 or dev >= 15.0:
                return True
            if -10.0 < dev <= -5.0:
                return True
            if -5.0 < dev < 0.0:
                return True
            if 5.0 <= dev < 10.0:
                return True
            if 10.0 <= dev < 15.0:
                return True
            return False

        candidates = []
        for name, value in anchors.items():
            if value is None or pd.isna(value) or value <= 0:
                continue
            dev = (close - value) / value * 100.0
            if _in_alert_range(dev):
                candidates.append(
                    (name, round(float(value), 2), round(dev, 2),
                     WINDOW_PRIORITY.get(name, 999))
                )

        if not candidates:
            return None

        # 按窗口升序 → 偏离绝对值升序
        candidates.sort(key=lambda x: (x[3], abs(x[2])))
        best = candidates[0]
        return (best[0], best[1], best[2])

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
        rows = ""
        active_count = 0
        total_count = len(stock_data)

        for _, row in stock_data.iterrows():
            code = row.get("stock_code", "")
            name = row.get("stock_name", code)
            open_price = row.get("open")
            close_price = row.get("close")

            # 判断是否为最近交易数据（最近3天内有数据=活跃标的）
            data_date = row.get("date")
            in_trading = False
            if data_date is not None and not pd.isna(data_date):
                try:
                    date_str = str(data_date)[:10]
                    from datetime import datetime as dt_mod
                    data_dt = dt_mod.strptime(date_str, "%Y-%m-%d").date()
                    days_since = (today_date - data_dt).days
                    in_trading = 0 <= days_since <= 3  # 最近3天内
                except Exception:
                    pass

            if not in_trading:
                # 非交易日：跳过不显示
                continue

            active_count += 1

            # 格式化价格
            open_str = f"{open_price:.2f}" if open_price is not None and not pd.isna(open_price) else "-"
            close_str = f"{close_price:.2f}" if close_price is not None and not pd.isna(close_price) else "-"

            # 收集所有锚点
            anchors = {}
            for anchor_name in ("ma60", "wma20", "wma30", "wma50"):
                val = row.get(anchor_name)
                if val is not None and not pd.isna(val):
                    anchors[anchor_name] = float(val)

            # 选最优锚点
            best = None
            if close_price is not None and not pd.isna(close_price) and anchors:
                best = self._pick_best_anchor(float(close_price), anchors)

            if best:
                anchor_name, anchor_val, dev_pct = best
                dev_class = "positive" if dev_pct >= 0 else "negative"
                dev_str = f"{dev_pct:+.2f}%"
                anchor_str = f"{anchor_val:.2f}"
                name_str = anchor_name
            else:
                anchor_str = "-"
                dev_str = "-"
                dev_class = ""
                name_str = "-"

            rows += (
                f'<tr>'
                f'<td>{code}</td>'
                f'<td>{name}</td>'
                f'<td>{open_str}</td>'
                f'<td>{close_str}</td>'
                f'<td>{name_str}</td>'
                f'<td>{anchor_str}</td>'
                f'<td class="{dev_class}">{dev_str}</td>'
                f'</tr>\n'
            )

        # ── 加载模板 ──
        template_dir = Path(__file__).parent.parent / "templates"
        template = (template_dir / "brief_email.html").read_text(encoding="utf-8")

        body = template.format(
            label=label,
            report_date=today.strftime("%Y-%m-%d"),
            current_time=today.strftime("%H:%M"),
            active_count=active_count,
            total_count=active_count,  # 只算活跃的
            brief_rows=rows,
        )

        subject = f"{label} - {today.strftime('%Y-%m-%d')}"
        self._send_email(subject, body)

    # ────────────────────────────────────

    def _send_email(self, subject, body, chart_png_bytes=None, portfolio_chart_dict=None, pdf_bytes=None):
        """
        发送邮件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
            chart_png_bytes: 告警走势图 PNG 字节（可选），CID=chart001
            portfolio_chart_dict: 投资组合走势图 {"a_share": bytes, "non_a_share": bytes}
            pdf_bytes: 日报 PDF 附件 bytes（可选）
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

            has_any_chart = chart_png_bytes or portfolio_chart_dict

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
                    image.add_header("Content-Disposition", "inline", filename="chart.png")
                    inner.attach(image)
                    logger.info("告警走势图以 CID chart001 嵌入邮件")

                # 添加投资组合走势图（CID: chart002=A股, chart003=非A股）
                if portfolio_chart_dict:
                    cid_map = {"a_share": "chart002", "non_a_share": "chart003"}
                    for group_key, png_bytes in portfolio_chart_dict.items():
                        if png_bytes and group_key in cid_map:
                            cid = cid_map[group_key]
                            img = MIMEImage(png_bytes, "png")
                            img.add_header("Content-ID", f"<{cid}>")
                            img.add_header("Content-Disposition", "inline",
                                           filename=f"portfolio_{group_key}.png")
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
                pdf_part.add_header("Content-Disposition", "attachment",
                                    filename="日报.pdf")
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

        Args:
            deployment_info: 部署信息字典，包含部署详情
            version: 部署版本号 (git commit hash)
            summary: 部署摘要
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

        except Exception as e:
            logger.error(f"发送部署通知邮件失败: {e}")

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

            # 创建完整的HTML文件，包含主题和正文
            full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{subject}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; border-bottom: 1px solid #ddd; padding-bottom: 10px; }}
        .meta {{ color: #666; margin-bottom: 20px; font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>{subject}</h1>
    <div class="meta">
        <p><strong>发送时间:</strong> {current_time.strftime("%Y-%m-%d %H:%M:%S")}</p>
        <p><strong>收件人:</strong> {self.receiver_email}</p>
        <p><strong>文件:</strong> {filename}</p>
    </div>
    <hr>
    {body}
</body>
</html>"""

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(full_html)

            logger.info(f"邮件副本已保存: {filepath}")
            return filepath

        except Exception as e:
            logger.error(
                "保存邮件副本失败: %s, 目标目录: %s", e, self.email_archive_dir
            )
            return None

    # ── 日报 PDF 生成 ──

    def _chart_deviation_timeline(
        self, signal_scan, backtest, base64=True,
    ) -> str:
        """
        偏离度 30 日折线图: 取偏离绝对值最大的 5 只标的 + 触发信号的标的，
        叠加折线。虚线标注买入阈值。

        Returns:
            base64 PNG 字符串 或 HTML <img> 标签
        """
        import io, base64
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ..utils.font_setup import setup_cjk_font

        setup_cjk_font()

        snapshot = (getattr(signal_scan, "indicator_snapshot", {})
                    if signal_scan else {})
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
        colors = ["#2d8a56", "#c9a84c", "#2980b9", "#c0392b",
                  "#8e44ad", "#e67e22", "#1abc9c", "#34495e"]

        for i, code in enumerate(top5):
            vals = snapshot.get(code, {})
            d = vals.get("deviation", 0) * 100  # → %
            color = colors[i % len(colors)]
            ax.barh(i, d, color=color, height=0.5, alpha=0.85)
            label = f"{code[-4:]} {d:+.1f}%"
            x_pos = d + (0.5 if d >= 0 else -0.5)
            ha = "left" if d >= 0 else "right"
            ax.text(x_pos, i, label, va="center", ha=ha, fontsize=7,
                    color=color, fontweight="bold")

        # 买入阈值虚线
        ax.axvline(x=-0.5, color="#888", linestyle="--", linewidth=0.6, alpha=0.5)
        ax.text(-0.5, len(top5)-0.3, " 买入阈值 -0.5%", fontsize=6,
                color="#888", va="bottom")

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
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="white")
        plt.close(fig)
        buf.seek(0)

        if base64:
            b64 = base64.b64encode(buf.read()).decode()
            return f'<img src="data:image/png;base64,{b64}" style="max-width:100%"/>'
        return buf.read()

    def _generate_daily_pdf(
        self, session, alert_stocks, signal_scan, backtest, stock_data,
    ) -> bytes | None:
        """生成日报 PDF（xelatex 编译 LaTeX 模板），返回 bytes"""
        import tempfile, subprocess, os, io, re

        try:
            # 1. 图表 PNG
            chart_buf = self._chart_deviation_timeline(signal_scan, backtest, base64=False)
            chart_path = None
            if chart_buf and not isinstance(chart_buf, str):
                fd, chart_path = tempfile.mkstemp(suffix=".png", prefix="chart_")
                with open(fd, "wb") as f:
                    data = chart_buf if isinstance(chart_buf, bytes) else chart_buf.getvalue() if hasattr(chart_buf, 'getvalue') else chart_buf
                    f.write(data)
                chart_section = f"\\includegraphics[width=\\textwidth]{{{chart_path}}}"
            else:
                chart_section = "{\\color{gray}\\small 今日无图表数据}"

            # 2. KPI
            sa = getattr(signal_scan, "alerts", None) or [] if signal_scan else []
            buy_count = len(sa)
            bt_a = backtest.get("a_share", {}) if backtest else {}
            bt_n = backtest.get("non_a_share", {}) if backtest else {}

            ta = bt_a.get("total_return", 0) or 0
            tn = bt_n.get("total_return", 0) or 0
            kpi_buy = str(buy_count)
            kpi_a = f"{ta:+.1f}\\%"
            kpi_n = f"{tn:+.1f}\\%"
            kpi_s = "✓ 策略有效" if ta > 0 or tn > 0 else "✗ 策略无效"
            kpi_color_a = "green" if ta > 0 else "red"
            kpi_color_n = "green" if tn > 0 else "red"
            kpi_color_s = "green" if buy_count > 0 else "red"

            # 3. 触发信号
            trigger_lines = []
            for a in sa:
                code = getattr(a, "stock_code", "?")
                label = getattr(a, "rule_label", "?")
                cv = getattr(a, "current_value", "—")
                trigger_lines.append(
                    f"\\textbf{{{code}}} & {label} & {cv} \\\\"
                )
            trigger_section = (
                "\\begin{tabular}{lll}\n" +
                "\\textbf{标的} & \\textbf{信号规则} & \\textbf{当前值}\\\\\n" +
                "\n".join(trigger_lines) +
                "\n\\end{tabular}"
                if trigger_lines else
                "{\\color{gray}\\small 今日无触发信号}"
            )

            # 4. 表: 合并 indicator_snapshot + fundamentals
            snapshot = (getattr(signal_scan, "indicator_snapshot", {})
                        if signal_scan else {})
            consensus = (getattr(signal_scan, "consensus", None)
                         if signal_scan else None)
            cons_inds = consensus.consensus_indicators if consensus else ["deviation", "rsi"]
            fundamentals = {}
            if stock_data is not None and hasattr(stock_data, "iterrows"):
                for _, row in stock_data.iterrows():
                    code = str(row.get("stock_code", ""))
                    if not code:
                        continue
                    pe = row.get("pe_ratio")
                    pb = row.get("pb_ratio")
                    dy = row.get("dividend_yield")
                    fundamentals[code] = {
                        "pe": f"{pe:.1f}" if pe is not None and not pd.isna(pe) else "—",
                        "pb": f"{pb:.2f}" if pb is not None and not pd.isna(pb) else "—",
                        "dy": f"{dy:.2f}" if dy is not None and not pd.isna(dy) else "—",
                    }

            def _esc(s):
                """LaTeX 转义"""
                return str(s).replace("&", "\\&").replace("%", "\\%").replace("#", "\\#").replace("$", "\\$").replace("_", "\\_")

            header_cols = ["标的"] + cons_inds + ["息\\%", "PE", "PB", "信号"]
            # 列格式: l for 标的, c for signal, r for numbers
            col_fmt = "l" + "r" * len(cons_inds) + "r" * 3 + "c"
            table_rows = ""
            alert_codes = set(getattr(a, "stock_code", "") for a in sa)

            a_codes = sorted(
                [c for c in snapshot if c.isdigit() or c.replace(".", "").isdigit()],
                key=lambda c: (abs(snapshot[c].get("deviation", 0) or 0)),
                reverse=True,
            )
            for code in a_codes:
                vals = snapshot.get(code, {})
                sig = "●" if code in alert_codes else ""
                cells = [_esc(code)]
                for ind in cons_inds:
                    v = vals.get(ind, 0) or 0
                    if ind == "deviation":
                        cells.append(f"{v*100:+.1f}\\%")
                    else:
                        cells.append(f"{v:.2f}")
                fund = fundamentals.get(code, {})
                cells.append(fund.get("dy", "—"))
                cells.append(fund.get("pe", "—"))
                cells.append(fund.get("pb", "—"))
                cells.append(sig)
                row_color = "\\rowcolor{bg!30}" if sig else ""
                # LaTeX 分割 A 股和境外
                is_a = code.isdigit() or code.replace(".", "").isdigit()
                table_rows += f"{row_color}{' & '.join(cells)} \\\\\n"

            nona_codes = sorted(
                [c for c in snapshot if c not in a_codes],
                key=lambda c: (abs(snapshot[c].get("deviation", 0) or 0)),
                reverse=True,
            )
            if nona_codes:
                table_rows += (
                    f"\\multicolumn{{{len(header_cols)}}}{{l}}{{\\color{{navy}}\\textbf{{境外 · {len(nona_codes)} 只}}}}\\\\\n"
                )
            for code in nona_codes:
                vals = snapshot.get(code, {})
                sig = "●" if code in alert_codes else ""
                cells = [_esc(code)]
                for ind in cons_inds:
                    v = vals.get(ind, 0) or 0
                    if ind == "deviation":
                        cells.append(f"{v*100:+.1f}\\%")
                    else:
                        cells.append(f"{v:.2f}")
                fund = fundamentals.get(code, {})
                cells.append(fund.get("dy", "—"))
                cells.append(fund.get("pe", "—"))
                cells.append(fund.get("pb", "—"))
                cells.append(sig)
                row_color = "\\rowcolor{bg!30}" if sig else ""
                table_rows += f"{row_color}{' & '.join(cells)} \\\\\n"

            table_section = (
                "\\small\n"
                "\\rowcolors{2}{white}{stripe}\n"
                f"\\begin{{tabular}}{{{col_fmt}}}\n"
                "\\toprule\n"
                + " & ".join(header_cols) + " \\\\\n"
                "\\midrule\n"
                + table_rows +
                "\\bottomrule\n"
                "\\end{tabular}"
            )

            # 5. 脚注
            buy_sigs = consensus.buy_signal_counts if consensus else {}
            strat_note = " · ".join(list(buy_sigs.keys())[:4]) if buy_sigs else "—"
            bt_text = f"A股策略超额 {ta:+.1f}\\%"
            if bt_a.get("benchmarks"):
                for bn, bv in bt_a["benchmarks"].items():
                    beat = "✓" if ta > bv else "✗"
                    bt_text += f"\\quad vs {bn} {bv:+.1f}\\% {beat}"

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
                for line in md_text.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        if in_list:
                            latex_lines.append("\\end{itemize}")
                            in_list = False
                        if in_table:
                            latex_lines.append("\\end{tabular}")
                            in_table = False
                        latex_lines.append(f"\\section*{{{stripped[2:]}}}")
                    elif stripped.startswith("## "):
                        if in_list:
                            latex_lines.append("\\end{itemize}")
                            in_list = False
                        latex_lines.append(f"\\subsection*{{{stripped[3:]}}}")
                    elif stripped.startswith("- "):
                        if not in_list:
                            latex_lines.append("\\begin{itemize}")
                            in_list = True
                        item = stripped[2:]
                        item = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', item)
                        latex_lines.append(f"  \\item {item}")
                    elif stripped.startswith("---"):
                        latex_lines.append("\\vspace{4pt}\\hrule\\vspace{4pt}")
                    elif stripped.startswith("$$"):
                        formula = stripped.strip("$").strip()
                        latex_lines.append(f"\\[{formula}\\]")
                    elif stripped.startswith("|"):
                        if not in_table:
                            cols = stripped.count("|") - 1
                            latex_lines.append(f"\\begin{{tabular}}{{{'l'*cols}}}")
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
                        item = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', stripped)
                        item = re.sub(r'\$(.+?)\$', r'$\1$', item)
                        latex_lines.append(f"{item}\n")
                if in_list:
                    latex_lines.append("\\end{itemize}")
                if in_table:
                    latex_lines.append("\\end{tabular}")
                appendix_section = "\n".join(latex_lines)

            # 7. 渲染模板
            tex_path = Path(__file__).parent.parent / "templates" / "report_daily.tex"
            template = tex_path.read_text(encoding="utf-8").replace("\r\n", "\n")

            info = self._get_server_info()
            html = template.replace("\\VAR{report_date}", datetime.now().strftime("%Y-%m-%d %A"))
            html = html.replace("\\VAR{server_hostname}", info.get("hostname", ""))
            html = html.replace("\\VAR{kpi_buy}", kpi_buy)
            html = html.replace("\\VAR{kpi_a}", kpi_a)
            html = html.replace("\\VAR{kpi_n}", kpi_n)
            html = html.replace("\\VAR{kpi_s}", kpi_s)
            html = html.replace("\\VAR{kpi_color_a}", kpi_color_a)
            html = html.replace("\\VAR{kpi_color_n}", kpi_color_n)
            html = html.replace("\\VAR{kpi_color_s}", kpi_color_s)
            html = html.replace("\\VAR{chart_section}", chart_section)
            html = html.replace("\\VAR{trigger_section}", trigger_section)
            html = html.replace("\\VAR{table_section}", table_section)
            html = html.replace("\\VAR{strategy_note}", strat_note)
            html = html.replace("\\VAR{backtest_note}", bt_text)
            html = html.replace("\\VAR{appendix_section}", appendix_section)

            # 8. xelatex 编译
            with tempfile.TemporaryDirectory() as tmpdir:
                tex_file = Path(tmpdir) / "report.tex"
                tex_file.write_text(html, encoding="utf-8")

                for _ in range(2):  # 两次编译（交叉引用）
                    result = subprocess.run(
                        ["xelatex", "-interaction=nonstopmode", "-output-directory",
                         tmpdir, str(tex_file)],
                        capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode != 0:
                        log_file = Path(tmpdir) / "report.log"
                        log_tail = ""
                        if log_file.exists():
                            lines = log_file.read_text(errors="replace").split("\n")
                            # 找第一个 "!" 错误行
                            for i, l in enumerate(lines):
                                if l.startswith("!"):
                                    log_tail = "\\n".join(lines[max(0,i-1):i+5])
                                    break
                        logger.warning("xelatex 编译问题: %s", log_tail or result.stderr[-200:])

                pdf_file = Path(tmpdir) / "report.pdf"
                if pdf_file.exists():
                    pdf_bytes = pdf_file.read_bytes()
                    logger.info("日报 PDF 生成成功 (%d bytes)", len(pdf_bytes))
                    return pdf_bytes
                else:
                    log_file = Path(tmpdir) / "report.log"
                    if log_file.exists():
                        logger.error("xelatex 日志: %s", log_file.read_text(errors="replace")[-500:])
                    logger.error("xelatex 未产出 PDF")
                    return None

        except Exception as e:
            logger.error("生成日报 PDF 失败: %s", e)
            return None
        finally:
            if chart_path and os.path.exists(chart_path):
                os.unlink(chart_path)
        try:
            import io
            from weasyprint import HTML

            # 1. 图表
            chart_img = self._chart_deviation_timeline(signal_scan, backtest, base64=True)

            # 2. KPI
            sa = getattr(signal_scan, "alerts", None) or [] if signal_scan else []
            buy_count = len(sa)
            bt_a = backtest.get("a_share", {}) if backtest else {}
            bt_n = backtest.get("non_a_share", {}) if backtest else {}

            def _kpi(vals, labels, colors):
                parts = []
                for v, l, c in zip(vals, labels, colors):
                    cs = "green" if c == "g" else "red" if c == "r" else ""
                    parts.append(
                        f'<div class="kpi"><div class="num {cs}">{v}</div>'
                        f'<div class="label">{l}</div></div>'
                    )
                return "\n".join(parts)

            kpi_vals = [
                str(buy_count),
                f"{bt_a.get('total_return',0):+.1f}%",
                f"{bt_n.get('total_return',0):+.1f}%",
                "✓" if bt_a.get("total_return", 0) > 0 else "✗",
            ]
            kpi_labels = ["A股买入", "A股策略活", "境外策略", "策略状态"]
            kpi_colors = ["", "g" if bt_a.get("total_return",0) > 0 else "r",
                          "g" if bt_n.get("total_return",0) > 0 else "r",
                          "g" if buy_count > 0 else ""]

            # 3. 触发信号行
            trigger_rows = ""
            consensus = getattr(signal_scan, "consensus", None) if signal_scan else None
            consensus_stocks = set(consensus.consensus_stocks) if consensus else set()
            for a in sa:
                code = getattr(a, "stock_code", "?")
                label = getattr(a, "rule_label", "?")
                cv = getattr(a, "current_value", "—")
                bg = "buy"
                trigger_rows += (
                    f'<div class="row {bg}">'
                    f'<span class="code">{code}</span> {label} · {cv}'
                    f'</div>'
                )
            if not trigger_rows:
                trigger_rows = '<div class="row" style="color:#888">今日无触发信号</div>'

            # 4. 表
            snapshot = (getattr(signal_scan, "indicator_snapshot", {})
                        if signal_scan else {})

            # 从 stock_data 提取基本面数据映射
            fundamentals: dict[str, dict[str, str]] = {}
            if stock_data is not None and hasattr(stock_data, "iterrows"):
                for _, row in stock_data.iterrows():
                    code = str(row.get("stock_code", ""))
                    if not code:
                        continue
                    pe = row.get("pe_ratio")
                    pb = row.get("pb_ratio")
                    dy = row.get("dividend_yield")
                    fundamentals[code] = {
                        "pe": f"{pe:.1f}" if pe is not None and not pd.isna(pe) else "—",
                        "pb": f"{pb:.2f}" if pb is not None and not pd.isna(pb) else "—",
                        "dy": f"{dy:.2f}" if dy is not None and not pd.isna(dy) else "—",
                    }
            cons_inds = consensus.consensus_indicators if consensus else ["deviation","rsi"]

            table_rows = ""
            header_cols = ["标的"] + cons_inds + ["息%", "PE", "PB", "信号"]
            num_cols = set(cons_inds + ["息%", "PE", "PB"])
            table_rows += "<tr>" + "".join(
                '<th class="num">' + c + "</th>" if c in num_cols
                else "<th>" + c + "</th>"
                for c in header_cols
            ) + "</tr>"

            # A 股分组
            a_codes = sorted(
                [c for c in snapshot if c.isdigit() or c.replace(".","").isdigit()],
                key=lambda c: (snapshot[c].get("deviation", 0) or 0),
            )
            for code in a_codes:
                vals = snapshot.get(code, {})
                sig = "█" if code in set(getattr(a, "stock_code","") for a in sa) else "—"
                row_class = "signal-buy" if sig == "█" else ""
                cells = [f"<td>{code}</td>"]
                for ind in cons_inds:
                    v = vals.get(ind, 0) or 0
                    if ind == "deviation":
                        color = "green" if v >= 0 else "red"
                        cells.append(f'<td class="num {color}">{v*100:+.1f}%</td>')
                    else:
                        cells.append(f'<td class="num">{v:.2f}</td>')
                fund = fundamentals.get(code, {})
                cells.append(f'<td class="num">{fund.get("dy","—")}</td>')
                cells.append(f'<td class="num">{fund.get("pe","—")}</td>')
                cells.append(f'<td class="num">{fund.get("pb","—")}</td>')
                cells.append(f"<td>{sig}</td>")
                table_rows += f'<tr class="{row_class}">{"".join(cells)}</tr>'

            # 境外分组
            nona_codes = sorted(
                [c for c in snapshot if c not in a_codes],
                key=lambda c: (snapshot[c].get("deviation", 0) or 0),
            )
            if nona_codes:
                table_rows += (
                    f'<tr class="group-divider">'
                    f'<td colspan="{len(header_cols)}">境外 · {len(nona_codes)} 只</td>'
                    f'</tr>'
                )
            for code in nona_codes:
                vals = snapshot.get(code, {})
                sig = "█" if code in set(getattr(a,"stock_code","") for a in sa) else "—"
                row_class = "signal-buy" if sig == "█" else ""
                cells = [f"<td>{code}</td>"]
                for ind in cons_inds:
                    v = vals.get(ind, 0) or 0
                    if ind == "deviation":
                        color = "green" if v >= 0 else "red"
                        cells.append(f'<td class="num {color}">{v*100:+.1f}%</td>')
                    else:
                        cells.append(f'<td class="num">{v:.2f}</td>')
                fund = fundamentals.get(code, {})
                cells.append(f'<td class="num">{fund.get("dy","—")}</td>')
                cells.append(f'<td class="num">{fund.get("pe","—")}</td>')
                cells.append(f'<td class="num">{fund.get("pb","—")}</td>')
                cells.append(f"<td>{sig}</td>")
                table_rows += f'<tr class="{row_class}">{"".join(cells)}</tr>'

            # 5. 脚注
            buy_sigs = consensus.buy_signal_counts if consensus else {}
            strat_note = " · ".join(list(buy_sigs.keys())[:4]) if buy_sigs else "—"
            bt_text = (
                f"A股: 策略 {bt_a.get('total_return',0):+.1f}%"
                if bt_a else ""
            )
            if bt_a.get("benchmarks"):
                for bn, bv in bt_a["benchmarks"].items():
                    beat = "✓" if bt_a.get("total_return",0) > bv else "✗"
                    bt_text += f" vs {bn} {bv:+.1f}% {beat}"

            # 6. 渲染
            template = (
                Path(__file__).parent.parent / "templates" / "report_daily.html"
            ).read_text(encoding="utf-8")

            html = template.replace("__KPI0__", kpi_vals[0])
            html = html.replace("__KPI0_LABEL__", kpi_labels[0])
            html = html.replace("__KPI0_COLOR__", " green" if kpi_colors[0]=="g" else " red" if kpi_colors[0]=="r" else "")
            html = html.replace("__KPI1__", kpi_vals[1])
            html = html.replace("__KPI1_LABEL__", kpi_labels[1])
            html = html.replace("__KPI1_COLOR__", " green" if kpi_colors[1]=="g" else " red" if kpi_colors[1]=="r" else "")
            html = html.replace("__KPI2__", kpi_vals[2])
            html = html.replace("__KPI2_LABEL__", kpi_labels[2])
            html = html.replace("__KPI2_COLOR__", " green" if kpi_colors[2]=="g" else " red" if kpi_colors[2]=="r" else "")
            html = html.replace("__KPI3__", kpi_vals[3])
            html = html.replace("__KPI3_LABEL__", kpi_labels[3])
            html = html.replace("__KPI3_COLOR__", " green" if kpi_colors[3]=="g" else " red" if kpi_colors[3]=="r" else "")

            html = html.replace("{chart_img}", chart_img)
            html = html.replace("{trigger_rows}", trigger_rows)
            html = html.replace("{table_rows}", table_rows)
            html = html.replace("{strategy_footnote}", f"策略信号: {strat_note}")
            html = html.replace("{backtest_footnote}", bt_text)

            info = self._get_server_info()
            html = html.replace("{report_date}",
                datetime.now().strftime("%Y-%m-%d %A"))
            html = html.replace("{server_hostname}",
                info.get("hostname", ""))

            # 附录: 指标方法论 (Markdown → HTML, LaTeX → PNG 内嵌)
            md_path = (
                Path(__file__).parent.parent / "templates" / "appendix_methodology.md"
            )
            if md_path.exists():
                import markdown, re, io, base64
                import matplotlib.pyplot as plt

                md_text = md_path.read_text(encoding="utf-8")

                def _tex_to_img(match):
                    """matplotlib mathtext 渲染 LaTeX → base64 PNG"""
                    formula = match.group(1)
                    display = match.group(0).startswith("$$")
                    w, h = (5.5, 0.45) if display else (3.5, 0.35)
                    fs = 10 if display else 9
                    try:
                        fig, ax = plt.subplots(figsize=(w, h), dpi=100)
                        ax.text(0.5, 0.5, f"${formula}$", fontsize=fs,
                                ha="center", va="center", transform=ax.transAxes)
                        ax.axis("off")
                        buf = io.BytesIO()
                        fig.savefig(buf, format="png", dpi=100,
                                    bbox_inches="tight", facecolor="white")
                        plt.close(fig)
                        buf.seek(0)
                        b64 = base64.b64encode(buf.read()).decode()
                        tag = (
                            f'<div style="text-align:center;margin:8px 0">'
                            f'<img src="data:image/png;base64,{b64}" '
                            f'style="max-width:100%"/></div>'
                        ) if display else (
                            f'<img src="data:image/png;base64,{b64}" '
                            f'style="vertical-align:middle;height:1.1em"/>'
                        )
                        return tag
                    except Exception as e:
                        logger.debug("LaTeX render failed: %s", e)
                        return f"<code>{formula}</code>"

                md_text = re.sub(r"\$\$(.+?)\$\$", _tex_to_img, md_text, flags=re.DOTALL)
                md_text = re.sub(r"\$(.+?)\$", _tex_to_img, md_text)

                appendix = markdown.markdown(
                    md_text,
                    extensions=["tables", "fenced_code", "codehilite"],
                )
                html += (
                    '<div style="page-break-before:always;font-size:9px;line-height:1.5;'
                    'padding:12px 16px">'
                    + appendix +
                    '</div>'
                )

            # WeasyPrint
            pdf_bytes = HTML(string=html).write_pdf()
            return pdf_bytes

        except Exception as e:
            logger.error("生成日报 PDF 失败: %s", e)
            return None
