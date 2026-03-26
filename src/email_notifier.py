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
import requests
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email import policy
from datetime import datetime
from pathlib import Path

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

        # 邮件副本配置
        self.email_archive_dir = Path(
            self.email_config.get("archive_dir", "./data/email_archive")
        )
        self.email_archive_dir.mkdir(parents=True, exist_ok=True)

        if not self.sender_email or not self.sender_password or not self.receiver_email:
            logger.warning("邮件配置不完整，邮件通知功能可能无法正常工作")

    def send_alert(
        self, alert_stocks, stock_data, analysis_results=None, announcements=None
    ):
        """
        发送股票提醒邮件

        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）
        """
        try:
            # 构建邮件内容
            subject = f"股票提醒 - {datetime.now().strftime('%Y-%m-%d')}"
            body = self._build_email_body(
                alert_stocks, stock_data, analysis_results, announcements
            )

            # 发送邮件
            self._send_email(subject, body)

            logger.info(f"成功发送提醒邮件给 {self.receiver_email}")

        except Exception as e:
            logger.error(f"发送邮件失败: {e}")

    def send_daily_report(self, stock_data, analysis_results=None, announcements=None):
        """
        发送每日报告邮件（即使没有满足条件的股票也发送）

        Args:
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）
        """
        try:
            # 构建邮件内容（使用空提醒列表）
            subject = f"股票日报 - {datetime.now().strftime('%Y-%m-%d')}"
            body = self._build_email_body(
                [], stock_data, analysis_results, announcements
            )

            # 发送邮件
            self._send_email(subject, body)

            logger.info(f"成功发送每日报告邮件给 {self.receiver_email}")

        except Exception as e:
            logger.error(f"发送每日报告邮件失败: {e}")

    def _build_email_body(
        self, alert_stocks, stock_data, analysis_results=None, announcements=None
    ):
        """
        构建邮件正文（完整版：4个表格 + LLM分析 + 公告 + 服务器信息）

        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）

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
            stock_code = alert.get("stock_code", "")
            stock_name = self._get_stock_name(stock_code)
            low_price = alert.get("low_price", 0)
            ma60 = alert.get("ma60", 0)
            low_ma60_diff = alert.get("price_difference", 0)  # 最低价与MA60差值
            low_ma60_pct = alert.get(
                "percentage_difference", 0
            )  # 最低价与MA60百分比差值

            # 从stock_data中查找收盘价和其他数据
            stock_row = stock_data[stock_data["stock_code"] == stock_code]
            close_price = 0
            if not stock_row.empty:
                close_price = stock_row.iloc[0].get("close", 0)

            # 计算收盘价与MA60差值
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
            close_diff_class = "positive" if close_ma60_diff >= 0 else "negative"
            close_pct_class = "positive" if close_ma60_pct >= 0 else "negative"
            earnings_growth_class = (
                "positive"
                if earnings_growth is not None and earnings_growth > 0
                else "negative"
                if earnings_growth is not None and earnings_growth < 0
                else ""
            )

            # 技术指标行
            alert_rows_technical += f"""
                <tr class="alert-row">
                    <td>{stock_code}</td>
                    <td>{stock_name}</td>
                    <td>{low_price:.2f}</td>
                    <td>{ma60:.2f}</td>
                    <td>{close_price:.2f}</td>
                    <td class="{close_diff_class}">{close_ma60_diff:+.2f}</td>
                    <td class="{close_pct_class}">{close_ma60_pct:+.2f}%</td>
                    <td class="positive">{low_ma60_diff:.2f}</td>
                    <td class="positive">{low_ma60_pct:.2f}%</td>
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
            stock_name = self._get_stock_name(stock_code)
            open_price = row.get("open", 0)
            close_price = row.get("close", 0)
            high_price = row.get("high", 0)
            low_price = row.get("low", 0)
            ma60 = row.get("ma60", 0)

            # 计算收盘价与MA60差值
            close_ma60_diff = close_price - ma60
            close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0

            # 确定颜色类
            diff_class = "positive" if close_ma60_diff >= 0 else "negative"
            pct_class = "positive" if close_ma60_pct >= 0 else "negative"

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

            # 检查是否满足条件（最低价 < MA60）
            status = "正常"
            if low_price < ma60:
                status = "<span style='color: #f44336;'>提醒</span>"

            # 价格技术指标行
            all_rows_price += f"""
                <tr>
                    <td>{stock_code}</td>
                    <td>{open_price:.2f}</td>
                    <td>{close_price:.2f}</td>
                    <td>{high_price:.2f}</td>
                    <td>{low_price:.2f}</td>
                    <td>{ma60:.2f}</td>
                    <td class="{diff_class}">{close_ma60_diff:+.2f}</td>
                    <td class="{pct_class}">{close_ma60_pct:+.2f}%</td>
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
                stock_name = self._get_stock_name(stock_code)

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
                    
                    {f'<div style="margin-bottom: 15px;"><h5 style="margin: 0 0 5px 0; color: #666;">投资建议</h5><p>{investment_recommendation_html}</p></div>' if investment_recommendation else ""}
                    
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

        # 5. 构建公告部分
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
                            cash_dividend = detail.get("cash_dividend", 0)
                            dividend_per_share = detail.get("dividend_per_share", 0)
                            info = f"{announcement_date}: 分红{cash_dividend:.2f}元/股"
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
                        if cash_dividend:
                            dividend_info.append(f"现金分红: {cash_dividend:.3f}元/股")
                        dividend_per_share = llm_dividend.get("dividend_per_share")
                        if dividend_per_share:
                            dividend_info.append(
                                f"总分红: {dividend_per_share:.3f}元/股"
                            )
                        dividend_date = llm_dividend.get("dividend_date")
                        if dividend_date:
                            dividend_info.append(f"分红日期: {dividend_date}")
                        confidence = llm_dividend.get("confidence", 0)
                        confidence_pct = f"{confidence * 100:.0f}%"

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

        # 6. 获取服务器信息
        server_info = self._get_server_info()

        # 7. 构建报警股票部分
        alert_section = ""
        if alert_stocks:
            alert_section = alert_section_template.format(
                alert_count=len(alert_stocks),
                alert_rows_technical=alert_rows_technical,
                alert_rows_fundamental=alert_rows_fundamental,
            )

        # 8. 替换主模板变量
        html_content = email_template.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            alert_section=alert_section,
            all_rows_price=all_rows_price,
            all_rows_fundamental=all_rows_fundamental,
            llm_analysis_section=llm_analysis_section,
            announcements_section=announcements_section,
            server_hostname=server_info["hostname"],
            server_ip=server_info["ip_address"],
            server_kernel=server_info["kernel_version"],
            server_system=server_info["system"],
            server_machine=server_info["machine"],
        )

        return html_content

    def _get_stock_name(self, stock_code):
        """
        获取股票名称

        Args:
            stock_code: 股票代码

        Returns:
            str: 股票名称（如果无法获取名称，则返回股票代码）
        """
        stock_code_str = str(stock_code)

        # 尝试从新浪财经实时API获取股票名称
        try:
            # 确定市场代码
            if stock_code_str.startswith(("6", "5", "9")):
                market = "sh"  # 沪市
            elif stock_code_str.startswith(("0", "3", "2")):
                market = "sz"  # 深市
            else:
                # 默认沪市
                market = "sh"

            # 新浪财经实时API
            url = f"http://hq.sinajs.cn/list={market}{stock_code_str}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Referer": "http://finance.sina.com.cn/",
            }

            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()

            # 解析响应格式: var hq_str_sh601728="中国联通,5.83,5.82,..."
            content = response.text
            match = re.search(r'="(.+)"', content)
            if match:
                data_str = match.group(1)
                items = data_str.split(",")
                if len(items) > 0:
                    stock_name = items[0].strip()
                    if stock_name and stock_name != "暂无该股票信息":
                        logger.info(
                            f"从新浪财经API获取股票 {stock_code_str} 名称: {stock_name}"
                        )
                        return stock_name

        except Exception as e:
            logger.warning(f"从新浪财经API获取股票 {stock_code_str} 名称失败: {e}")

        # 如果API失败，尝试从配置文件读取（向后兼容）
        stocks_config = self.config.get("stocks", [])
        for stock_item in stocks_config:
            stock_str = str(stock_item).strip()
            if stock_str.startswith(stock_code_str) and "#" in stock_str:
                parts = stock_str.split("#", 1)
                if len(parts) > 1:
                    name = parts[1].strip()
                    if name:
                        logger.info(f"从配置文件获取股票 {stock_code_str} 名称: {name}")
                        return name

        # 所有方法都失败，返回股票代码
        logger.warning(f"无法获取股票 {stock_code_str} 名称，返回股票代码")
        return stock_code_str

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
        self._save_email_copy(subject, body)

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
            logger.error(f"保存邮件副本失败: {e}")
            return None
