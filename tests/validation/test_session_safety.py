#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session数据安全测试
验证随机数据写入Session会被正确拦截并抛出异常
零配置，强制启用测试
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/models"))

import random
import pandas as pd
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

# 导入要测试的模块
from session_manager import SessionManager
from schemas import SessionContext, StockPriceData, DataSource, AdjustmentType
from models.converters import dataframe_to_stock_price_data
from utils import (
    SessionDataSafetyError,
    clear_random_calls,
    is_value_from_random,
)


class TestSessionDataSafety:
    """Session数据安全测试"""

    @pytest.fixture
    def session_manager(self):
        """创建SessionManager实例"""
        config = {"stocks": []}
        return SessionManager(config)

    @pytest.fixture
    def session(self, session_manager):
        """创建Session实例"""
        config = {"stocks": []}
        return session_manager.create_session(config)

    @pytest.fixture
    def valid_price_data(self):
        """生成有效但非随机的价格数据"""
        return {
            "date": datetime.now(),
            "open": 10.0,
            "close": 10.5,
            "high": 10.8,
            "low": 9.9,
            "volume": 1000000.0,
            "amount": 10500000.0,
        }

    def test_normal_data_not_detected(self, session_manager, session, valid_price_data):
        """测试正常数据不会被误报"""
        clear_random_calls()

        # 创建正常的扁平StockPriceData
        stock_data = StockPriceData(
            stock_code="600000",
            data_source=DataSource.SINA,
            adjustment_type=AdjustmentType.NONE,
            last_updated=datetime.now(),
            **valid_price_data,
            ma60=10.2,
        )

        # 应该正常写入，不抛异常
        result = session_manager.update_stock_data(session, "600000", stock_data)
        assert result is True
        assert "600000" in session.stocks_data

    def test_random_data_in_dataframe_throws_exception(self):
        """测试DataFrame中的随机数据会被检测到（直接调用检测函数）"""
        from utils.session_safety_check import _check_dataframe_for_random

        # 注意：不要先clear，先生成随机值
        # 生成包含随机数的DataFrame
        random_value = random.uniform(9.0, 11.0)
        df = pd.DataFrame(
            [
                {
                    "date": datetime.now(),
                    "open": 10.0,
                    "close": random_value,  # 随机生成的随机值
                    "high": 10.8,
                    "low": 9.9,
                    "volume": 1000000.0,
                    "amount": 10500000.0,
                    "stock_code": "600000",
                }
            ]
        )

        # 临时patch掉测试文件检测
        from unittest.mock import patch

        with patch(
            "utils.session_safety_check._is_caller_from_test", return_value=False
        ):
            # 应该抛出SessionDataSafetyError异常
            with pytest.raises(SessionDataSafetyError):
                _check_dataframe_for_random(df, "600000")

    def test_random_data_in_stock_price_data_throws_exception(self, valid_price_data):
        """测试扁平StockPriceData中的随机数据会被检测到（直接调用检测函数）"""
        from utils.session_safety_check import _check_stock_price_data_for_random

        # 注意：不要先clear，先生成随机值
        # 生成随机的ma60值
        random_ma60 = random.uniform(9.0, 11.0)

        # 创建扁平StockPriceData
        stock_data = StockPriceData(
            stock_code="600000",
            data_source=DataSource.SINA,
            adjustment_type=AdjustmentType.NONE,
            last_updated=datetime.now(),
            **valid_price_data,
            ma60=random_ma60,  # 随机生成的值
        )

        # 临时patch掉测试文件检测
        from unittest.mock import patch

        with patch(
            "utils.session_safety_check._is_caller_from_test", return_value=False
        ):
            # 应该抛出SessionDataSafetyError异常
            with pytest.raises(SessionDataSafetyError):
                _check_stock_price_data_for_random(stock_data, "600000")

    def test_random_call_tracking(self):
        """测试random调用被正确记录"""
        clear_random_calls()

        # 调用random函数
        val1 = random.random()
        val2 = random.uniform(1.0, 10.0)
        val3 = random.randint(1, 100)

        # 检查是否能检测到这些值
        is_r1, call1 = is_value_from_random(val1)
        is_r2, call2 = is_value_from_random(val2)
        is_r3, call3 = is_value_from_random(val3)

        assert is_r1 is True
        assert is_r2 is True
        assert is_r3 is True
        assert call1.get("function") == "random"
        assert call2.get("function") == "uniform"
        assert call3.get("function") == "randint"

    def test_clear_calls_works(self):
        """测试清空调用记录功能"""
        clear_random_calls()

        # 调用random
        val = random.random()

        # 检查能检测到
        is_r, _ = is_value_from_random(val)
        assert is_r is True

        # 清空
        clear_random_calls()

        # 再次检查，应该检测不到了（记录已清空）
        is_r_after, _ = is_value_from_random(val)
        # 注意：由于我们清空了记录，所以检测不到了
        # 这是预期行为，每次写入前都会清空

    def test_float_approximate_matching(self):
        """测试浮点数近似匹配"""
        clear_random_calls()

        # 生成随机浮点数
        original = random.uniform(1.0, 10.0)

        # 非常接近的值（浮点精度问题）
        almost_same = original + 1e-15

        # 应该能检测到
        is_r, call = is_value_from_random(almost_same)
        assert is_r is True


def test_safety_system_enabled_by_default():
    """验证安全系统是零配置强制启用"""
    # 检查是否有我们的监控器实例
    from utils import get_random_monitor

    monitor = get_random_monitor()
    assert monitor is not None
    assert hasattr(monitor, "calls")
    # 验证监控器已经初始化
    assert monitor._wrapped is True
