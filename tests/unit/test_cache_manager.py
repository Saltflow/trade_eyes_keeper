#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for cache manager functionality.
"""
import os
import tempfile
from pathlib import Path

from src.cache_manager import CacheManager

def test_cache_manager_initialization():
    """Test cache manager can be initialized with config."""
    config = {
        'storage': {
            'cache_dir': './test_cache',
            'cache_days': 7
        }
    }
    cache_manager = CacheManager(config)
    assert cache_manager is not None
    assert cache_manager.cache_days == 7

def test_cache_operations(tmp_path):
    """Test basic cache operations (set/get) with temporary directory."""
    cache_dir = tmp_path / 'cache'
    config = {
        'storage': {
            'cache_dir': str(cache_dir),
            'cache_days': 7
        }
    }
    cache_manager = CacheManager(config)
    
    test_stock_code = '601728'
    test_data = {
        'stock_code': test_stock_code,
        'close': 5.97,
        'open': 6.02,
        'high': 6.05,
        'low': 5.90,
        'ma60': 6.18,
        'dividend_per_share': 0.25,
        'dividend_yield': 4.19
    }
    
    # Set cache
    cache_manager.set_stock_data_cache(test_stock_code, test_data)
    
    # Get cache
    cached_data = cache_manager.get_stock_data_cache(test_stock_code)
    assert cached_data is not None
    assert cached_data['stock_code'] == test_stock_code
    assert 'data' in cached_data
    assert cached_data['data']['close'] == 5.97
    
    # Test analysis cache
    test_analysis = {
        'stock_code': test_stock_code,
        'rating': '买入',
        'risk_level': '低',
        'summary': '基本面良好'
    }
    
    cache_manager.set_analysis_cache(test_stock_code, test_analysis)
    cached_analysis = cache_manager.get_analysis_cache(test_stock_code)
    assert cached_analysis is not None
    assert 'analysis' in cached_analysis
    assert cached_analysis['analysis'].get('rating') == '买入'
    
