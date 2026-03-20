#!/usr/bin/env python3
"""
测试分红数据爬虫
"""
import os
import sys

def test_dividend_scraping():
    """测试分红数据爬虫"""
    from src.web_crawler import StockWebCrawler
    
    config = {}
    crawler = StockWebCrawler(config)
    
    stocks = ['601728', '600938', '601985']
    
    for stock in stocks:
        result = crawler.fetch_dividend_data(stock)
        # 结果可能是None或字典
        if result:
            assert isinstance(result, dict)
            # 确保有必要的字段
            assert 'dividend_per_share' in result
            assert 'last_dividend_date' in result
            assert 'dividend_history' in result
            # 每股分红应为数字或None
            if result['dividend_per_share'] is not None:
                assert isinstance(result['dividend_per_share'], (int, float))
        else:
            # 允许返回None（例如网络问题）
            pass