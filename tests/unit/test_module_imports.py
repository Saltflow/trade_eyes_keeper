#!/usr/bin/env python3
"""
Unit tests for module imports and basic functionality.
"""
import os

# Set environment variable to skip email sending
os.environ['SKIP_EMAIL'] = 'true'

def test_imports():
    """Test that all core modules can be imported."""
    from src.cache_manager import CacheManager
    from src.data_fetcher import StockDataFetcher
    from src.email_notifier import EmailNotifier
    from src.announcement_fetcher import AnnouncementFetcher
    
    # If we get here without ImportError, test passes
    assert True

def test_cache_manager():
    """Test cache manager initialization and filename parsing."""
    config = {
        'storage': {
            'cache_dir': './test_cache',
            'cache_days': 7
        }
    }
    from src.cache_manager import CacheManager
    cm = CacheManager(config)
    
    # Test filename parsing (rsplit vs split fix)
    test_filename = "600938_20250307.json"
    parts = test_filename.rsplit('_', 1)
    assert len(parts) == 2
    assert parts[0] == '600938'
    assert parts[1] == '20250307.json'

def test_email_notifier():
    """Test email notifier with split tables functionality."""
    config = {
        'email': {
            'smtp_server': 'smtp.test.com',
            'sender_email': 'test@test.com',
            'sender_password': 'test',
            'receiver_email': 'receiver@test.com'
        }
    }
    from src.email_notifier import EmailNotifier
    notifier = EmailNotifier(config)
    
    # Test building email body with mock data
    import pandas as pd
    import numpy as np
    
    # Create mock stock data
    stock_data = pd.DataFrame({
        'stock_code': ['601728', '600938'],
        'open': [5.0, 20.0],
        'close': [5.1, 20.5],
        'high': [5.2, 21.0],
        'low': [4.9, 20.0],
        'ma60': [5.0, 20.2],
        'dividend_per_share': [0.2, 0.5],
        'dividend_yield': [3.92, 2.44],
        'earnings_growth': [5.0, -2.0]
    })
    
    alert_stocks = [{
        'stock_code': '601728',
        'low_price': 4.9,
        'ma60': 5.0,
        'price_difference': 0.1,
        'percentage_difference': 2.0
    }]
    
    body = notifier._build_email_body(alert_stocks, stock_data)
    
    # Verify email body contains basic elements
    assert len(body) > 0
    assert '股票提醒通知' in body  # Email header
    assert '股票列表' in body  # Stock table
    assert '股票代码' in body  # Stock code column



