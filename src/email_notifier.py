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
        构建邮件正文（简化版）
        
        Args:
            alert_stocks: 满足条件的股票列表
            stock_data: 完整的股票数据DataFrame
            analysis_results: LLM分析结果字典（可选）
            announcements: 公告数据字典（可选）
            
        Returns:
            str: 邮件正文（HTML格式）
        """
        import pandas as pd
        from datetime import datetime
        
        # 创建HTML表格
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>股票提醒通知</title>
    <style>
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
            font-family: Arial, sans-serif;
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
    
    <h3>股票列表 ({stock_count} 只)</h3>
    <table>
        <tr>
            <th>股票代码</th>
            <th>股票名称</th>
            <th>最低价</th>
            <th>MA60</th>
            <th>每股分红(元)</th>
            <th>股息率(%)</th>
            <th>PE</th>
            <th>PB</th>
            <th>ROE(%)</th>
            <th>状态</th>
        </tr>
        {rows}
    </table>
    
    <p><em>注：此邮件由股票量化系统自动发送，请勿直接回复。</em></p>
</body>
</html>"""
        
        # 构建表格行
        rows = ""
        alert_set = {alert.get('stock_code', '') for alert in alert_stocks}
        
        for _, row in stock_data.iterrows():
            stock_code = row.get('stock_code', '')
            stock_name = self._get_stock_name(stock_code)
            low_price = row.get('low', 0)
            ma60 = row.get('ma60', 0)
            
            # 基本面数据
            dividend_per_share = row.get('dividend_per_share')
            dividend_yield = row.get('dividend_yield')
            pe_ratio = row.get('pe_ratio')
            pb_ratio = row.get('pb_ratio')
            roe = row.get('roe')
            
            # 格式化
            dividend_per_share_str = f"{dividend_per_share:.3f}" if dividend_per_share is not None and not pd.isna(dividend_per_share) else "-"
            dividend_yield_str = f"{dividend_yield:.2f}%" if dividend_yield is not None and not pd.isna(dividend_yield) else "-"
            pe_ratio_str = f"{pe_ratio:.2f}" if pe_ratio is not None and not pd.isna(pe_ratio) else "-"
            pb_ratio_str = f"{pb_ratio:.2f}" if pb_ratio is not None and not pd.isna(pb_ratio) else "-"
            roe_str = f"{roe:.2f}%" if roe is not None and not pd.isna(roe) else "-"
            
            # 状态
            if low_price < ma60:
                status = "<span style='color: #f44336;'>提醒</span>"
                row_class = "alert-row"
            else:
                status = "正常"
                row_class = ""
            
            rows += f"""
                <tr class="{row_class}">
                    <td>{stock_code}</td>
                    <td>{stock_name}</td>
                    <td>{low_price:.2f}</td>
                    <td>{ma60:.2f}</td>
                    <td>{dividend_per_share_str}</td>
                    <td>{dividend_yield_str}</td>
                    <td>{pe_ratio_str}</td>
                    <td>{pb_ratio_str}</td>
                    <td>{roe_str}</td>
                    <td>{status}</td>
                </tr>"""
        
        # 替换变量
        html_content = html.format(
            current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            stock_count=len(stock_data),
            rows=rows
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
    
