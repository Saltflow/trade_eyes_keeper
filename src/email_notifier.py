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

        # SMTP服务器配置
        self.smtp_server = self.email_config.get("smtp_server", "smtp.yeah.net")
        self.smtp_port = self.email_config.get("smtp_port", 465)  # 默认465
        self.sender_email = self.email_config.get("sender_email", "")
        self.sender_password = self.email_config.get("sender_password", "")
        self.receiver_email = self.email_config.get("receiver_email", "")
        self.enable_tls = self.email_config.get("enable_tls", False)
        self.enable_ssl = self.email_config.get("enable_ssl", True)  # yeah.net使用SSL

        # 邮件副本配置：使用项目根目录的绝对路径避免工作目录漂移
        archive_dir_config = self.email_config.get("archive_dir", "data/email_archive")
        archive_dir_path = Path(archive_dir_config)
        if not archive_dir_path.is_absolute():
            project_root = Path(__file__).resolve().parent.parent
            archive_dir_path = (project_root / archive_dir_config).resolve()

        self.email_archive_dir = archive_dir_path
        self.email_archive_dir.mkdir(parents=True, exist_ok=True)
        logger.info("邮件副本目录已初始化为: %s", self.email_archive_dir)

        if not self.sender_email or not self.sender_password or not self.receiver_email:
            logger.warning("邮件配置不完整，邮件通知功能可能无法正常工作")

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
            backtest_results = session.backtest_results
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 构建邮件主题
            subject = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"

            # 构建邮件内容
            body = self._build_email_body(
                alert_stocks,
                stock_data,
                analysis_results,
                announcements,
                financial_analysis_results,
                backtest_results,
                historical_data=historical_data,
            )

            # 发送邮件
            self._send_email(subject, body)

            logger.info(
                f"成功发送提醒邮件给 {self.receiver_email} "
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
            backtest_results = session.backtest_results
            # 历史数据（由 data_fetcher 暂存，供图表使用）
            historical_data = getattr(session, "_historical", {})

            # 构建邮件主题
            subject = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"

            # 构建邮件内容（使用空警报列表）
            body = self._build_email_body(
                [],
                stock_data,
                analysis_results,
                announcements,
                financial_analysis_results,
                backtest_results,
                historical_data=historical_data,
            )

            # 发送邮件
            self._send_email(subject, body)

            logger.info(f"成功发送每日报告邮件给 {self.receiver_email} (来自Session)")

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
            return "&lt;h3&gt;财报分析&lt;/h3&gt;&lt;p&gt;暂无可用财报分析数据（可能无新财报或获取失败）。&lt;/p&gt;"

        logger.info(
            f"构建财报分析部分: 收到{len(financial_analysis_results)}只股票的分析结果"
        )

        html = """
            &lt;h3&gt;财报分析&lt;/h3&gt;
            &lt;p&gt;基于最新财报的结构化摘要（按股票展示最近2份）：&lt;/p&gt;
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

    def _build_backtest_section(self, backtest_results):
        """
        构建回测结果部分HTML

        Args:
            backtest_results: 回测结果列表，格式与backtest_framework.py输出一致

        Returns:
            str: HTML格式的回测部分
        """
        if not backtest_results:
            return "<h3>回测分析</h3><p>暂无回测数据。</p>"

        logger.info(f"构建回测分析部分: 收到{len(backtest_results)}只股票的回测结果")

        # 生成表格部分
        table_html = '<table style="border-collapse: collapse; width: 100%; margin-top: 20px;">\n'
        table_html += "    <thead>\n"
        table_html += "        <tr>\n"
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">股票代码</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">2年前持有至今</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">1年前持有至今</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">6个月前持有至今</th>\n'
        table_html += '            <th style="border: 1px solid #ddd; padding: 12px; text-align: center; background-color: #f2f2f2; font-weight: bold;">2个月前持有至今</th>\n'
        table_html += "        </tr>\n"
        table_html += "    </thead>\n"
        table_html += "    <tbody>\n"

        for stock_result in backtest_results:
            table_html += f"        <tr>\n"
            table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center;">{stock_result["stock_code"]}</td>\n'
            for ret in stock_result["returns"]:
                if "error" in ret:
                    table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center; color: red;">计算失败</td>\n'
                else:
                    value = ret["current_value"]
                    profit_pct = ret["profit_pct"]
                    color = "green" if profit_pct >= 0 else "red"
                    if value is not None and not pd.isna(value):
                        table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center; color: {color};">{value:.2f}元<br><small>({profit_pct:+.2f}%)</small></td>\n'
                    else:
                        table_html += f'            <td style="border: 1px solid #ddd; padding: 12px; text-align: center; color: red;">数据不可用</td>\n'
            table_html += "        </tr>\n"

        table_html += "    </tbody>\n"
        table_html += "</table>\n"

        # 构建完整的回测部分HTML
        html = f"""
        <div style="margin-top: 40px; border-top: 1px solid #ddd; padding-top: 20px;">
            <h3>近两年监控股票回测分析</h3>
            <p style="color: #666; font-size: 14px;">假设每个起始点买入1万元，计算到今天的价值（交易成本：买入千分之2）</p>
            
            {table_html}
            
            <div style="margin-top: 20px; font-size: 12px; color: #888;">
                <p>注：</p>
                <ul>
                    <li>数据来源：新浪财经/腾讯财经/东方财富公开数据</li>
                    <li>交易成本：买入时收取千分之2（0.2%）手续费，卖出无费用</li>
                    <li>最小交易单位：100股（手）</li>
                    <li>不考虑分红和分红再投资</li>
                    <li>日期处理：自动匹配最近的交易日</li>
                </ul>
            </div>
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
        backtest_results=None,
        historical_data=None,
    ):
        """
         构建邮件正文（完整版：4个表格 + LLM分析 + 公告 + 服务器信息 + 财报分析 + 回测分析 + 图表）

        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）
            financial_analysis_results: 财报分析结果字典（可选）
            backtest_results: 回测结果列表，格式与backtest_framework.py输出一致（可选）
            historical_data: 完整历史DataFrame字典 stock_code → DataFrame（可选，供图表使用）

        Returns:
            str: 邮件正文（HTML格式）
        """
        from datetime import datetime
        from pathlib import Path

        # 1. 加载模板
        template_dir = Path(__file__).parent / "templates"
        email_template = (template_dir / "email_template.html").read_text(
            encoding="utf-8"
        )
        alert_section_template = (template_dir / "alert_section.html").read_text(
            encoding="utf-8"
        )

        # 2. 构建满足条件的股票行（拆分为技术指标和基本面指标）
        alert_rows_technical = ""
        alert_rows_fundamental = ""
        for alert in alert_stocks:
            if self._is_multi_alert_format(alert):
                # 多层级警报格式
                technical_row, fundamental_row = self._build_alert_rows_multi(
                    alert, stock_data
                )
                alert_rows_technical += technical_row
                alert_rows_fundamental += fundamental_row
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

                # 基本面指标行
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

            # 价格技术指标行
            all_rows_price += f"""
                <tr>
                    <td>{stock_code}</td>
                    <td>{open_price_str}</td>
                    <td>{close_price_str}</td>
                    <td>{high_price_str}</td>
                    <td>{low_price_str}</td>
                    <td>{ma60_str}</td>
                    <td class="{diff_class}">{close_ma60_diff_str}</td>
                    <td class="{pct_class}">{close_ma60_pct_str}</td>
                    <td>{status}</td>
                </tr>
            """

            # 基本面指标行
            all_rows_fundamental += f"""
                <tr>
                    <td>{stock_code}</td>
                    <td>{dividend_per_share_str}</td>
                    <td>{dividend_yield_str}</td>
                    <td class="{earnings_growth_class}">{earnings_growth_str}</td>
                    <td>{pe_ratio_str}</td>
                    <td>{pb_ratio_str}</td>
                    <td>{roe_str}</td>
                    <td>{debt_ratio_str}</td>
                </tr>
            """

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

        # 6. 构建回测分析部分
        backtest_section = ""
        if backtest_results:
            backtest_section = self._build_backtest_section(backtest_results)

        # 7. 生成走势图表（告警股票的价格 + 最长锚点MA曲线）
        chart_section = ""
        if alert_stocks and historical_data:
            try:
                b64_png = generate_combined_chart(
                    historical_data=historical_data,
                    alerts=alert_stocks,
                    stock_data=stock_data,
                    trading_days=60,
                )
                if b64_png:
                    chart_section = f"""
                    <h3>价格走势图</h3>
                    <p>近2个月最低价与最长告警锚点移动平均线：</p>
                    <div style="text-align: center; margin: 20px 0;">
                        <img src="data:image/png;base64,{b64_png}"
                             alt="价格走势图"
                             style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 4px;" />
                    </div>
                    """
                    logger.info("走势图表已生成并嵌入邮件")
            except Exception as e:
                logger.warning(f"生成走势图表失败: {e}")

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
            backtest_section=backtest_section,
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

                public_ip = (
                    urllib.request.urlopen("https://ifconfig.me", timeout=10)
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

    def _send_email(self, subject, body):
        """
        发送邮件

        Args:
            subject: 邮件主题
            body: 邮件正文（HTML格式）
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
            # 创建邮件消息，设置UTF-8编码策略
            msg = MIMEMultipart("alternative")
            msg.policy = policy.default

            # 邮件主题（使用UTF-8编码策略自动处理）
            msg["Subject"] = subject

            # 编码发件人和收件人
            msg["From"] = self.sender_email
            msg["To"] = self.receiver_email

            # 添加HTML内容，确保UTF-8编码
            html_part = MIMEText(body, "html", "utf-8")
            html_part.set_charset("utf-8")
            html_part["Content-Transfer-Encoding"] = "quoted-printable"
            msg.attach(html_part)

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

    def send_deployment_notification(self, deployment_info=None):
        """
        发送部署通知邮件

        Args:
            deployment_info: 部署信息字典，包含部署详情
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
    </div>
    
    {f'<div class="info"><p><strong>部署详情:</strong> {deployment_info}</p></div>' if deployment_info else ""}
    
    <p><em>注：此邮件由股票量化系统自动发送，用于部署验证。</em></p>
</body>
</html>"""

            # 发送邮件
            self._send_email(subject, body)
            logger.info(f"部署通知邮件发送成功: {subject}")

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
