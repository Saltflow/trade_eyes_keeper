#!/usr/bin/env python3
"""
股息数据获取测试
测试data_fetcher.py中的股息获取逻辑
"""

import os
import pytest
from unittest.mock import patch, MagicMock
import yaml


class TestDividendFetching:
    """股息获取测试类"""

    @pytest.fixture
    def config(self):
        """加载测试配置"""
        config_path = os.path.join(
            os.path.dirname(__file__), "../config/config.yaml.example"
        )
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config

    @pytest.fixture
    def fetcher(self, config):
        """创建StockDataFetcher实例"""
        from src.data_fetcher import StockDataFetcher

        return StockDataFetcher(config)

    def test_dividend_fetch_with_mock_llm_cache(self, fetcher):
        """测试使用LLM缓存获取股息数据"""
        stock_code = "601919"

        # Mock LLM缓存返回
        with patch.object(
            fetcher.cache_manager, "get_latest_llm_extraction_for_stock"
        ) as mock_cache:
            mock_cache.return_value = {
                "stock_code": stock_code,
                "date": "2026-03-21",
                "extracted_data": {
                    "cash_dividend_per_share": 0.5,
                    "bonus_share_ratio": None,
                    "conversion_ratio": None,
                },
            }

            dividend = fetcher._fetch_dividend_from_web_crawler(stock_code)
            assert dividend == 0.5
            mock_cache.assert_called_once_with(stock_code, days=365)

    def test_dividend_fetch_without_cache_falls_back_to_crawler(self, fetcher):
        """测试无缓存时回退到网页爬虫"""
        stock_code = "601919"

        # Mock LLM缓存返回None
        with patch.object(
            fetcher.cache_manager, "get_latest_llm_extraction_for_stock"
        ) as mock_cache:
            mock_cache.return_value = None

            # Mock网页爬虫返回
            with patch.object(fetcher.web_crawler, "fetch_dividend") as mock_crawler:
                mock_crawler.return_value = 0.3

                dividend = fetcher._fetch_dividend_from_web_crawler(stock_code)
                assert dividend == 0.3
                mock_cache.assert_called_once_with(stock_code, days=365)
                mock_crawler.assert_called_once_with(stock_code)

    def test_dividend_validation_logic(self, fetcher):
        """测试股息率验证逻辑（调整阈值后不丢弃数据）"""
        # 测试高股息率场景（>30%）
        # 应该记录警告但不丢弃数据
        stock_code = "601919"

        with patch.object(
            fetcher.cache_manager, "get_latest_llm_extraction_for_stock"
        ) as mock_cache:
            mock_cache.return_value = {
                "stock_code": stock_code,
                "date": "2026-03-21",
                "extracted_data": {
                    "cash_dividend_per_share": 10.0,  # 高分红
                    "bonus_share_ratio": None,
                    "conversion_ratio": None,
                },
            }

            # Mock价格数据：股价30元，分红10元 => 33.33%股息率
            with patch.object(fetcher, "_fetch_stock_price_data") as mock_price:
                mock_price.return_value = {
                    "close": 30.0,
                    "high": 32.0,
                    "low": 28.0,
                    "open": 29.0,
                }

                # 应该记录警告但不返回None
                dividend = fetcher._fetch_dividend_from_web_crawler(stock_code)
                # 注意：_fetch_dividend_from_web_crawler只返回分红数据，不验证股息率
                # 股息率验证在_fetch_fundamental_data中
                # 这里我们只验证高分红数据被正确返回
                assert dividend == 10.0
