#!/usr/bin/env python3
"""
测试邮件编码
"""
import os
import pandas as pd

def test_email_encoding():
    """测试邮件编码"""
    from src.email_notifier import EmailNotifier
    
    # 设置环境变量跳过实际邮件发送
    os.environ['SKIP_EMAIL'] = 'true'
    
    config = {
        'email': {
            'smtp_server': 'smtp.yeah.net',
            'smtp_port': 465,
            'sender_email': 'test@example.com',
            'sender_password': 'fake_password',
            'receiver_email': 'receiver@example.com',
            'enable_ssl': True,
            'enable_tls': False
        }
    }
    
    notifier = EmailNotifier(config)
    
    # 创建模拟股票数据
    stock_data = pd.DataFrame({
        'stock_code': ['601728', '600938'],
        'open': [5.90, 40.00],
        'close': [5.97, 40.49],
        'high': [6.00, 41.00],
        'low': [5.85, 39.50],
        'ma60': [5.80, 39.00],
        'dividend_per_share': [0.181, 0.666],
        'dividend_yield': [3.04, 1.65],
        'earnings_growth': [None, None]
    })
    
    # 模拟提醒股票
    alert_stocks = [{'stock_code': '601728', 'low_price': 5.85, 'ma60': 5.80, 'price_difference': -0.05, 'percentage_difference': -0.86}]
    
    # 模拟LLM分析结果
    analysis_results = {
        '601728': {
            'analysis_text': '中国电信是一家优秀的电信运营商，具有稳定的现金流和分红政策。',
            'summary': {'sentiment': '积极', 'has_growth': True, 'has_dividend': True, 'has_risk': False}
        }
    }
    
    # 测试构建邮件内容（包含中文字符）
    body = notifier._build_email_body(alert_stocks, stock_data, analysis_results)
    assert len(body) > 0
    # 检查邮件内容包含关键表格标题（简化版邮件格式）
    assert '股票提醒通知' in body
    assert '股票列表' in body
    # 检查中文字符正常
    assert '中国电信' in body
    
