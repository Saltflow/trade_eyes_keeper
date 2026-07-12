"""
测试警报处理器（抗硬编码随机测试）
"""
import random
import tempfile
import os
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import pytest

from src.alerting.alert_processor import AlertProcessor
from tests.validation.test_utils import RandomTestParameterGenerator


class MockAlertsConfig:
    """模拟警报配置"""
    def __init__(self, thresholds=None):
        self.thresholds = thresholds or [-10, -5, 0, 5, 10, 15]
        # alert_engine 遍历 self.config.anchors（{name, ...} 列表）
        self.anchors = [
            {"name": "ma60"},
            {"name": "wma20"},
        ]
    
    def get_intervals(self):
        """获取区间定义"""
        # 简化的区间定义
        return [
            {"label": "<-10%", "lower": -float("inf"), "upper": -10},
            {"label": "(-10%, -5%]", "lower": -10, "upper": -5},
            {"label": "(-5%, 0)", "lower": -5, "upper": 0},
            {"label": "[0%, 5%)", "lower": 0, "upper": 5},
            {"label": "[5%, 10%)", "lower": 5, "upper": 10},
            {"label": "[10%, 15%)", "lower": 10, "upper": 15},
            {"label": ">=15%", "lower": 15, "upper": float("inf")},
        ]


@pytest.fixture
def temp_cache_dir():
    """创建临时缓存目录"""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def random_stock_data():
    """生成随机股票数据"""
    gen = RandomTestParameterGenerator()
    
    # 随机股票代码
    stock_code = gen.random_stock_code(exclude_etfs=True)
    
    # 随机价格数据
    price_data = gen.random_price_data()
    
    # 随机移动平均锚点值（基于收盘价的正负波动）
    base_price = price_data['close']
    ma60 = base_price * random.uniform(0.8, 1.2)  # ±20%波动
    wma20 = base_price * random.uniform(0.85, 1.15)  # ±15%波动
    
    # 创建股票数据Series
    stock_series = pd.Series({
        'stock_code': stock_code,
        'close': price_data['close'],
        'open': price_data['open'],
        'high': price_data['high'],
        'low': price_data['low'],
        'volume': price_data['volume'],
        'amount': price_data['amount'],
        'ma60': ma60,
        'wma20': wma20,
    })
    
    return stock_series


def test_alert_processor_integration(temp_cache_dir, random_stock_data):
    """测试警报处理器集成功能"""
    # 创建配置
    config = MockAlertsConfig()
    
    # 创建处理器
    processor = AlertProcessor(config, temp_cache_dir)
    
    # 处理单个股票
    alerts = processor.process_stock(random_stock_data)
    
    # 验证返回类型
    assert isinstance(alerts, list)
    
    # 根据锚点值，可能会有0个或多个警报
    # 我们只验证处理过程不抛出异常
    assert True  # 占位断言，实际测试中应更具体


def test_process_stock_dataframe(temp_cache_dir):
    """测试处理DataFrame功能"""
    gen = RandomTestParameterGenerator()
    config = MockAlertsConfig()
    processor = AlertProcessor(config, temp_cache_dir)
    
    # 创建随机股票数据DataFrame
    stocks_data = []
    for _ in range(random.randint(1, 5)):  # 1-5只随机股票
        price_data = gen.random_price_data()
        stock_code = gen.random_stock_code(exclude_etfs=True)
        
        stocks_data.append({
            'stock_code': stock_code,
            'close': price_data['close'],
            'open': price_data['open'],
            'high': price_data['high'],
            'low': price_data['low'],
            'volume': price_data['volume'],
            'amount': price_data['amount'],
            'ma60': price_data['close'] * random.uniform(0.8, 1.2),
            'wma20': price_data['close'] * random.uniform(0.85, 1.15),
        })
    
    df = pd.DataFrame(stocks_data)
    
    # 处理整个DataFrame
    all_alerts = processor.process_stock_dataframe(df)
    
    assert isinstance(all_alerts, list)
    
    # 验证日志输出（通过捕获日志）
    import logging
    logger = logging.getLogger('src.alert_processor')
    with pytest.MonkeyPatch.context() as mp:
        log_messages = []
        def mock_info(msg):
            log_messages.append(msg)
        mp.setattr(logger, 'info', mock_info)
        
        # 重新处理以捕获日志
        processor.process_stock_dataframe(df)
        
        # 验证至少有一条日志
        assert len(log_messages) > 0


def test_empty_stock_code(temp_cache_dir):
    """测试空股票代码处理"""
    config = MockAlertsConfig()
    processor = AlertProcessor(config, temp_cache_dir)
    
    # 创建没有股票代码的数据
    empty_data = pd.Series({
        'close': 10.0,
        'open': 9.8,
        'high': 10.2,
        'low': 9.7,
        # 缺少stock_code
    })
    
    alerts = processor.process_stock(empty_data)
    assert alerts == []  # 应该返回空列表


if __name__ == "__main__":
    # 直接运行测试
    pytest.main([__file__, "-v"])
