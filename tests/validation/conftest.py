"""
抗硬编码测试框架的fixture配置
"""
import os
import random
from datetime import datetime, time
from unittest.mock import patch
import pytest
import pytz


def load_stock_watchlist():
    """从config.yaml加载股票观察列表，失败时返回默认列表"""
    try:
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 
                                  'config', 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        stocks = config.get('stocks', [])
        if stocks:
            return stocks
    except Exception as e:
        print(f"加载配置失败，使用默认股票列表: {e}")
    
    # 默认股票列表（从当前配置中获取）
    return ['601728', '600938', '601985', '601919', '600795', 
            '601398', '601088', '512810', '510880', '601818', '601390']


@pytest.fixture
def random_stock_code():
    """随机返回一个股票代码"""
    stocks = load_stock_watchlist()
    return random.choice(stocks)


@pytest.fixture
def random_time_point():
    """随机返回一个时间点（小时:分钟）"""
    hour = random.randint(0, 23)
    minute = random.randint(0, 59)
    return time(hour, minute)


@pytest.fixture
def mock_datetime_now():
    """模拟datetime.now()的上下文管理器"""
    def _mock_datetime_now(year=2026, month=3, day=21, hour=15, minute=30, second=0):
        mock_now = datetime(year, month, day, hour, minute, second, tzinfo=pytz.timezone('Asia/Shanghai'))
        
        class MockDateTime:
            @classmethod
            def now(cls, tz=None):
                if tz:
                    return mock_now.astimezone(tz)
                return mock_now.replace(tzinfo=None)
        
        return MockDateTime
    
    return _mock_datetime_now


@pytest.fixture
def random_price_data():
    """生成随机但合理的股价数据"""
    base_price = random.uniform(5.0, 50.0)
    open_price = base_price
    close_price = open_price * random.uniform(0.95, 1.05)  # ±5%波动
    high_price = max(open_price, close_price) * random.uniform(1.0, 1.1)  # 最高价
    low_price = min(open_price, close_price) * random.uniform(0.9, 1.0)   # 最低价
    
    # 确保价格关系: low ≤ close ≤ high, low ≤ open ≤ high
    low_price = min(low_price, open_price, close_price, high_price)
    high_price = max(high_price, open_price, close_price, low_price)
    
    return {
        'open': round(open_price, 2),
        'close': round(close_price, 2),
        'high': round(high_price, 2),
        'low': round(low_price, 2),
        'volume': random.randint(1000000, 100000000),
        'amount': round(random.uniform(10000000, 1000000000), 2)
    }


@pytest.fixture
def random_config():
    """生成随机配置变体"""
    timezones = ['Asia/Shanghai', 'UTC', 'US/Eastern']
    cache_cutoffs = ['15:55', '14:00', '16:30']
    
    return {
        'timezone': random.choice(timezones),
        'cache_bypass_cutoff': random.choice(cache_cutoffs),
        'cache_days': random.choice([7, 14, 30]),
        'run_time': random.choice(['16:00', '15:30', '16:30'])
    }