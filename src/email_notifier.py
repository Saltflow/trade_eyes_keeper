"""
邮件通知模块
发送股票提醒邮件
"""

import logging
import smtplib
import ssl
import pandas as pd
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
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
        self.email_config = config.get('email', {})
        
        # SMTP服务器配置
        self.smtp_server = self.email_config.get('smtp_server', 'smtp.yeah.net')
        self.smtp_port = self.email_config.get('smtp_port', 465)  # 默认465
        self.sender_email = self.email_config.get('sender_email', '')
        self.sender_password = self.email_config.get('sender_password', '')
        self.receiver_email = self.email_config.get('receiver_email', '')
        self.enable_tls = self.email_config.get('enable_tls', False)
        self.enable_ssl = self.email_config.get('enable_ssl', True)  # yeah.net使用SSL
        
        # 邮件副本配置
        self.email_archive_dir = Path(self.email_config.get('archive_dir', './data/email_archive'))
        self.email_archive_dir.mkdir(parents=True, exist_ok=True)
        
        if not self.sender_email or not self.sender_password or not self.receiver_email:
            logger.warning("邮件配置不完整，邮件通知功能可能无法正常工作")
    
    def send_alert(self, alert_stocks, stock_data, analysis_results=None, announcements=None):
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
            body = self._build_email_body(alert_stocks, stock_data, analysis_results, announcements)
            
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
            body = self._build_email_body([], stock_data, analysis_results, announcements)
            
            # 发送邮件
            self._send_email(subject, body)
            
            logger.info(f"成功发送每日报告邮件给 {self.receiver_email}")
            
        except Exception as e:
            logger.error(f"发送每日报告邮件失败: {e}")
    
    def _build_email_body(self, alert_stocks, stock_data, analysis_results=None, announcements=None):
        """
        构建邮件正文（包含提醒股票和LLM分析）
        
        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）
            
        Returns:
            str: 邮件正文（HTML格式）
        """
        # 创建HTML表格
        html_table = """
        <html>
        <head>
            <style>
                table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin: 20px 0;
                }}
                th, td {{
                    border: 1px solid #ddd;
                    padding: 8px;
                    text-align: left;
                }}
                th {{
                    background-color: #f2f2f2;
                    font-weight: bold;
                }}
                .alert-row {{
                    background-color: #fff8e1;
                }}
                .positive {{
                    color: #4caf50;
                }}
                .negative {{
                    color: #f44336;
                }}
            </style>
        </head>
        <body>
            <h2>股票提醒通知</h2>
            <p>系统检测到以下股票满足条件：<strong>当天最低价 &lt; MA60（前复权）</strong></p>
            <p>检测时间：{current_time}</p>
            
            <h3>满足条件的股票 ({alert_count} 只)</h3>
            
            <h4>价格技术指标</h4>
            <table>
                <tr>
                    <th>股票代码</th>
                    <th>股票名称</th>
                    <th>最低价</th>
                    <th>MA60</th>
                    <th>收盘价</th>
                    <th>收盘-MA60差值</th>
                    <th>差值(%)</th>
                    <th>最低价-MA60差值</th>
                    <th>差值(%)</th>
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
            
            <h3>所有监控股票</h3>
            
            <h4>价格技术指标</h4>
            <table>
                <tr>
                    <th>股票代码</th>
                    <th>开盘价</th>
                    <th>收盘价</th>
                    <th>最高价</th>
                    <th>最低价</th>
                    <th>MA60</th>
                    <th>收盘-MA60差值</th>
                    <th>差值(%)</th>
                    <th>状态</th>
                </tr>
                {all_rows_price}
            </table>
            
            <h4>基本面指标</h4>
            <table>
                <tr>
                    <th>股票代码</th>
                    <th>每股分红(元)</th>
                    <th>股息率(%)</th>
                    <th>业绩增长(%)</th>
                    <th>PE</th>
                    <th>PB</th>
                    <th>ROE(%)</th>
                    <th>负债率(%)</th>
                </tr>
                {all_rows_fundamental}
            </table>
            
            {llm_analysis_section}
            
            {announcements_section}
            
            <p><em>注：此邮件由股票量化系统自动发送，请勿直接回复。</em></p>
        </body>
        </html>
        """
        
        # 构建满足条件的股票行（拆分为技术指标和基本面指标）
        alert_rows_technical = ""
        alert_rows_fundamental = ""
        for alert in alert_stocks:
            stock_code = alert.get('stock_code', '')
            stock_name = self._get_stock_name(stock_code)
            low_price = alert.get('low_price', 0)
            ma60 = alert.get('ma60', 0)
            low_ma60_diff = alert.get('price_difference', 0)  # 最低价与MA60差值
            low_ma60_pct = alert.get('percentage_difference', 0)  # 最低价与MA60百分比差值
            
            # 从stock_data中查找收盘价
            close_price = 0
            stock_row = stock_data[stock_data['stock_code'] == stock_code]
            if not stock_row.empty:
                close_price = stock_row.iloc[0].get('close', 0)
            
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
                dividend_per_share = stock_row.iloc[0].get('dividend_per_share')
                dividend_yield = stock_row.iloc[0].get('dividend_yield')
                earnings_growth = stock_row.iloc[0].get('earnings_growth')
                pe_ratio = stock_row.iloc[0].get('pe_ratio')
                pb_ratio = stock_row.iloc[0].get('pb_ratio')
                roe = stock_row.iloc[0].get('roe')
                debt_ratio = stock_row.iloc[0].get('debt_ratio')
            
            # 格式化基本面数据
            dividend_per_share_str = f"{dividend_per_share:.3f}" if dividend_per_share is not None and not pd.isna(dividend_per_share) else "-"
            dividend_yield_str = f"{dividend_yield:.2f}%" if dividend_yield is not None and not pd.isna(dividend_yield) else "-"
            earnings_growth_str = f"{earnings_growth:+.2f}%" if earnings_growth is not None and not pd.isna(earnings_growth) else "-"
            pe_ratio_str = f"{pe_ratio:.2f}" if pe_ratio is not None and not pd.isna(pe_ratio) else "-"
            pb_ratio_str = f"{pb_ratio:.2f}" if pb_ratio is not None and not pd.isna(pb_ratio) else "-"
            roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "-"
            debt_ratio_str = f"{debt_ratio:.2f}%" if debt_ratio is not None and not pd.isna(debt_ratio) else "-"
            
            # 确定颜色类
            close_diff_class = "positive" if close_ma60_diff >= 0 else "negative"
            close_pct_class = "positive" if close_ma60_pct >= 0 else "negative"
            earnings_growth_class = "positive" if earnings_growth is not None and earnings_growth > 0 else "negative" if earnings_growth is not None and earnings_growth < 0 else ""
            
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
        
        # 构建所有监控股票行（拆分为价格技术指标和基本面指标）
        all_rows_price = ""
        all_rows_fundamental = ""
        for _, row in stock_data.iterrows():
            stock_code = row.get('stock_code', '')
            stock_name = self._get_stock_name(stock_code)
            open_price = row.get('open', 0)
            close_price = row.get('close', 0)
            high_price = row.get('high', 0)
            low_price = row.get('low', 0)
            ma60 = row.get('ma60', 0)
            
            # 计算收盘价与MA60差值
            close_ma60_diff = close_price - ma60
            close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0
            
            # 确定颜色类
            diff_class = "positive" if close_ma60_diff >= 0 else "negative"
            pct_class = "positive" if close_ma60_pct >= 0 else "negative"
            
            # 获取基本面数据
            dividend_per_share = row.get('dividend_per_share')
            dividend_yield = row.get('dividend_yield')
            earnings_growth = row.get('earnings_growth')
            pe_ratio = row.get('pe_ratio')
            pb_ratio = row.get('pb_ratio')
            roe = row.get('roe')
            debt_ratio = row.get('debt_ratio')
            
            # 格式化基本面数据
            dividend_per_share_str = f"{dividend_per_share:.3f}" if dividend_per_share is not None and not pd.isna(dividend_per_share) else "-"
            dividend_yield_str = f"{dividend_yield:.2f}%" if dividend_yield is not None and not pd.isna(dividend_yield) else "-"
            earnings_growth_str = f"{earnings_growth:+.2f}%" if earnings_growth is not None and not pd.isna(earnings_growth) else "-"
            pe_ratio_str = f"{pe_ratio:.2f}" if pe_ratio is not None and not pd.isna(pe_ratio) else "-"
            pb_ratio_str = f"{pb_ratio:.2f}" if pb_ratio is not None and not pd.isna(pb_ratio) else "-"
            roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "-"
            debt_ratio_str = f"{debt_ratio:.2f}%" if debt_ratio is not None and not pd.isna(debt_ratio) else "-"
            
            # 确定颜色类
            earnings_growth_class = "positive" if earnings_growth is not None and earnings_growth > 0 else "negative" if earnings_growth is not None and earnings_growth < 0 else ""
            
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
        
        # 构建LLM分析部分
        llm_analysis_section = ""
        if analysis_results and len(analysis_results) > 0:
            llm_analysis_section = """
            <h3>LLM基本面分析</h3>
            """
            for stock_code, analysis in analysis_results.items():
                stock_name = self._get_stock_name(stock_code)
                
                # 提取分析文本
                analysis_text = analysis.get('analysis_text', '')
                summary = analysis.get('summary', {})
                
                # 截断过长的分析文本
                if len(analysis_text) > 1000:
                    analysis_text = analysis_text[:1000] + "... (分析内容过长，已截断)"
                
                # 构建分析卡片
                sentiment = summary.get('sentiment', '中性')
                sentiment_color = '#4caf50' if sentiment == '积极' else '#f44336' if sentiment == '谨慎' else '#ff9800'
                
                llm_analysis_section += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px;">
                    <h4>{stock_code} {stock_name} <span style="color: {sentiment_color}; font-weight: bold;">[{sentiment}]</span></h4>
                    <p><strong>分析摘要:</strong> {analysis_text[:300]}...</p>
                    <p><strong>关键指标:</strong></p>
                    <ul>
                        <li>增长潜力: {'有' if summary.get('has_growth', False) else '无'}</li>
                        <li>分红情况: {'有' if summary.get('has_dividend', False) else '无'}</li>
                        <li>风险提示: {'有' if summary.get('has_risk', False) else '无'}</li>
                    </ul>
                    <p><em>注：LLM分析仅供参考，不构成投资建议。</em></p>
                </div>
                """
         
        # 构建公告部分
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
                    title = announcement.get('title', '')
                    date = announcement.get('date', '')
                    url = announcement.get('url', '')
                    exchange = announcement.get('exchange', '').upper()
                    link = f'<a href="{url}" target="_blank">{title}</a>' if url else title
                    announcements_section += f"""
                    <div style="margin-bottom: 8px;">
                        <strong>{i+1}. [{exchange}] {date}</strong><br/>
                        {link}
                    """
                    # 检查是否有官方分红记录（akshare数据）
                    dividend_details = announcement.get('dividend_details')
                    if dividend_details and len(dividend_details) > 0:
                        # 提取关键字段
                        cash_ratio = dividend_details.get('cash_dividend_ratio')
                        stock_ratio = dividend_details.get('stock_dividend_ratio')
                        cap_ratio = dividend_details.get('capitalization_ratio')
                        record_date = dividend_details.get('record_date')
                        ex_rights_date = dividend_details.get('ex_rights_date')
                        payment_date = dividend_details.get('payment_date')
                        settlement_date = dividend_details.get('settlement_date')
                        dividend_type = dividend_details.get('dividend_type')
                        announcement_date = dividend_details.get('announcement_date')
                        dividend_description = dividend_details.get('dividend_description')
                        
                        official_info = []
                        if cash_ratio and cash_ratio.strip() and cash_ratio != 'nan':
                            official_info.append(f"派息比例: {cash_ratio}")
                        if stock_ratio and stock_ratio.strip() and stock_ratio != 'nan':
                            official_info.append(f"送股比例: {stock_ratio}")
                        if cap_ratio and cap_ratio.strip() and cap_ratio != 'nan':
                            official_info.append(f"转增比例: {cap_ratio}")
                        if record_date and record_date.strip() and record_date != 'nan':
                            official_info.append(f"股权登记日: {record_date}")
                        if ex_rights_date and ex_rights_date.strip() and ex_rights_date != 'nan':
                            official_info.append(f"除权日: {ex_rights_date}")
                        if payment_date and payment_date.strip() and payment_date != 'nan':
                            official_info.append(f"派息日: {payment_date}")
                        if settlement_date and settlement_date.strip() and settlement_date != 'nan':
                            official_info.append(f"股份到账日: {settlement_date}")
                        if dividend_type and dividend_type.strip() and dividend_type != 'nan':
                            official_info.append(f"分红类型: {dividend_type}")
                        if announcement_date and announcement_date.strip() and announcement_date != 'nan':
                            official_info.append(f"公告日期: {announcement_date}")
                        
                        if official_info:
                            announcements_section += f"""
                            <div style="margin-left: 20px; margin-top: 5px; padding: 5px; background-color: #e8f4fd; border-left: 3px solid #2196f3; font-size: 0.9em;">
                                <strong>官方分红记录 (akshare):</strong><br/>
                                {', '.join(official_info)}
                            </div>
                            """
                    
                    # 检查是否有LLM提取的分红详情
                    llm_dividend = announcement.get('llm_extracted_dividend')
                    if llm_dividend and llm_dividend.get('success', False):
                        cash = llm_dividend.get('cash_dividend_per_share')
                        stock_ratio = llm_dividend.get('stock_dividend_ratio')
                        cap_ratio = llm_dividend.get('capitalization_ratio')
                        record_date = llm_dividend.get('record_date')
                        ex_rights_date = llm_dividend.get('ex_rights_date')
                        payment_date = llm_dividend.get('payment_date')
                        total_amount = llm_dividend.get('total_dividend_amount')
                        confidence = llm_dividend.get('confidence_score', 0.0)
                        
                        dividend_info = []
                        if cash is not None:
                            dividend_info.append(f"每股派息: {cash}元")
                        if stock_ratio is not None:
                            dividend_info.append(f"送股比例: {stock_ratio}")
                        if cap_ratio is not None:
                            dividend_info.append(f"转增比例: {cap_ratio}")
                        if record_date:
                            dividend_info.append(f"股权登记日: {record_date}")
                        if ex_rights_date:
                            dividend_info.append(f"除权日: {ex_rights_date}")
                        if payment_date:
                            dividend_info.append(f"派息日: {payment_date}")
                        if total_amount is not None:
                            dividend_info.append(f"分红总额: {total_amount}元")
                        
                        if dividend_info:
                            confidence_pct = f"{confidence*100:.1f}%" if confidence else "未知"
                            announcements_section += f"""
                            <div style="margin-left: 20px; margin-top: 5px; padding: 5px; background-color: #f8f9fa; border-left: 3px solid #4caf50; font-size: 0.9em;">
                                <strong>LLM提取分红详情（置信度: {confidence_pct}）:</strong><br/>
                                {', '.join(dividend_info)}
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
        
        # 替换模板中的变量
        html_content = html_table.format(
            current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            alert_count=len(alert_stocks),
            alert_rows_technical=alert_rows_technical,
            alert_rows_fundamental=alert_rows_fundamental,
            all_rows_price=all_rows_price,
            all_rows_fundamental=all_rows_fundamental,
            llm_analysis_section=llm_analysis_section,
            announcements_section=announcements_section
        )
        
        return html_content
    
    def _get_stock_name(self, stock_code):
        """
        获取股票名称（简单实现，实际可能需要从API获取）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            str: 股票名称
        """
        # 这里可以扩展为从API获取股票名称
        # 暂时返回代码+名称映射
        stock_names = {
            '601728': '中国电信',
            '600938': '中国海油',
            '601985': '中国核电',
            '601919': '中远海控',
            '600795': '国电电力',
            '601398': '工商银行',
            '601088': '中国神华',
            '512810': '华宝中证军工ETF',
            '510880': '华泰柏瑞红利ETF',
            '601818': '光大银行',
            '601390': '中国中铁'
        }
        
        return stock_names.get(stock_code, stock_code)
    
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
        
        if os.environ.get('SKIP_EMAIL') == 'true':
            logger.info(f"跳过邮件发送（测试模式）: 主题={subject}")
            return
        
        try:
            # 创建邮件消息，设置UTF-8编码策略
            msg = MIMEMultipart('alternative')
            msg.policy = policy.default
            
            # 邮件主题（使用UTF-8编码策略自动处理）
            msg['Subject'] = subject
            
            # 编码发件人和收件人
            msg['From'] = self.sender_email
            msg['To'] = self.receiver_email
            
            # 添加HTML内容，确保UTF-8编码
            html_part = MIMEText(body, 'html', 'utf-8')
            html_part.set_charset('utf-8')
            html_part['Content-Transfer-Encoding'] = 'quoted-printable'
            msg.attach(html_part)
            
            # 连接到SMTP服务器并发送邮件
            if self.enable_ssl:
                # 使用SSL连接
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=30, context=context) as server:
                    # 登录邮箱
                    server.login(self.sender_email, self.sender_password)
                    
                    # 发送邮件
                    server.send_message(msg)
            else:
                # 使用普通SMTP连接
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
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
            date_str = current_time.strftime('%Y%m%d')
            time_str = current_time.strftime('%H%M%S')
            # 清理主题中的非法文件名字符
            clean_subject = ''.join(c if c.isalnum() or c in ' _-' else '_' for c in subject)
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
        <p><strong>发送时间:</strong> {current_time.strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p><strong>收件人:</strong> {self.receiver_email}</p>
        <p><strong>文件:</strong> {filename}</p>
    </div>
    <hr>
    {body}
</body>
</html>"""
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(full_html)
            
            logger.info(f"邮件副本已保存: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"保存邮件副本失败: {e}")
            return None
    
