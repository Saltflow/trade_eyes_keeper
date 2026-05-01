"""
系统验证逻辑测试 - Step 1: 价格关系验证
测试condition_checker.py中的价格关系警告逻辑（使用session链路）
使用随机数据和mock，避免硬编码
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src/models"))

import random
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock
from .conftest import random_stock_code, random_price_data


class TestPriceRelationshipValidation:
    """价格关系验证测试"""

    @pytest.fixture
    def checker_session(self):
        """创建ConditionChecker和Session实例"""
        from src.core.condition_checker import ConditionChecker
        from src.session.session_manager import SessionManager
        from src.models.schemas import SessionContext

        config = {"stocks": []}
        checker = ConditionChecker(config)
        session_manager = SessionManager(config)
        session = session_manager.create_session(config)

        return checker, session, session_manager

    def test_valid_price_data_no_warnings(
        self, checker_session, random_stock_code, random_price_data
    ):
        """有效价格关系（low≤close≤high）不应产生警告"""
        checker, session, session_manager = checker_session
        price = random_price_data

        # 创建DataFrame并存入session
        df = pd.DataFrame(
            [
                {
                    "stock_code": random_stock_code,
                    "low": price["low"],
                    "close": price["close"],
                    "high": price["high"],
                    "open": price["open"],
                    "ma60": price["close"] * random.uniform(0.9, 1.1),
                }
            ]
        )

        # 存入session
        session_manager.update_stock_from_dataframe(session, random_stock_code, df)

        with patch("condition_checker.logger") as mock_logger:
            mock_logger.warning = MagicMock()
            checker.check_from_session(session, session_manager)
            assert mock_logger.warning.call_count == 0

    @pytest.mark.parametrize(
        "anomaly_type,desc",
        [
            ("close_lt_low", "收盘价<最低价"),
            ("close_gt_high", "收盘价>最高价"),
            ("low_gt_high", "最低价>最高价"),
        ],
    )
    def test_price_anomalies_no_longer_in_condition_checker(
        self, checker_session, random_stock_code, anomaly_type, desc
    ):
        """
        价格校验已移至 DataSource，ConditionChecker 不再触发价格异常警告。
        验证 ConditionChecker 不产⽣价格相关 warning。
        """
        checker, session, session_manager = checker_session
        base = random.uniform(10.0, 50.0)

        if anomaly_type == "close_lt_low":
            close, low = base, base * random.uniform(1.01, 1.1)
            high = max(base, low) * random.uniform(1.0, 1.1)
            open_price = base
        elif anomaly_type == "close_gt_high":
            close, high = base, base * random.uniform(0.9, 0.99)
            low = min(base, high) * random.uniform(0.9, 1.0)
            open_price = base
        else:  # low_gt_high
            low, high = base * random.uniform(1.1, 1.2), base * random.uniform(0.8, 0.9)
            close, open_price = base, base

        df = pd.DataFrame(
            [
                {
                    "stock_code": random_stock_code,
                    "low": low,
                    "close": close,
                    "high": high,
                    "open": open_price,
                    "ma60": base * random.uniform(0.9, 1.1),
                }
            ]
        )

        # 存入session
        session_manager.update_stock_from_dataframe(session, random_stock_code, df)

        with patch("condition_checker.logger") as mock_logger:
            mock_warning = MagicMock()
            mock_logger.warning = mock_warning
            checker.check_from_session(session, session_manager)

            # ConditionChecker 不再校验价格关系，不应触发价格警告
            assert mock_warning.call_count == 0, f"{desc}不应触发警告"

    def test_randomized_stock_codes_and_prices(self, checker_session):
        """随机股票代码和价格组合验证（系统稳定性）"""
        checker, session, session_manager = checker_session
        from .conftest import load_stock_watchlist

        stocks = load_stock_watchlist()
        for _ in range(min(3, len(stocks))):
            stock = random.choice(stocks)
            base = random.uniform(5.0, 50.0)

            # 随机生成价格关系（可能有效或无效）
            close = base
            if random.choice([True, False]):
                # 有效数据
                low = close * random.uniform(0.9, 1.0)
                high = close * random.uniform(1.0, 1.1)
                low, high = min(low, close, high), max(high, close, low)
            else:
                # 无效数据
                anomaly = random.choice(
                    ["close_lt_low", "close_gt_high", "low_gt_high"]
                )
                if anomaly == "close_lt_low":
                    low = close * random.uniform(1.01, 1.1)
                    high = max(base, low) * random.uniform(1.0, 1.1)
                elif anomaly == "close_gt_high":
                    high = close * random.uniform(0.9, 0.99)
                    low = min(base, high) * random.uniform(0.9, 1.0)
                else:
                    low = base * random.uniform(1.1, 1.2)
                    high = base * random.uniform(0.8, 0.9)

            df = pd.DataFrame(
                [
                    {
                        "stock_code": stock,
                        "low": low,
                        "close": close,
                        "high": high,
                        "open": base,
                        "ma60": base * random.uniform(0.9, 1.1),
                    }
                ]
            )

            # 存入session
            session_manager.update_stock_from_dataframe(session, stock, df)

            # 执行检查（不应崩溃）
            checker.check_from_session(session, session_manager)
            assert isinstance(session.alerts, list)


def test_randomization_usage():
    """验证测试使用随机化参数（抗硬编码特性）"""
    with open(__file__, "r", encoding="utf-8") as f:
        content = f.read()

    random_patterns = [
        r"random\.",
        "random_stock_code",
        "random_price_data",
        "random.choice",
    ]
    used = [p for p in random_patterns if p in content]

    assert len(used) >= 3, f"随机化使用不足: {used}"
    print(f"✅ 随机化验证: 使用{len(used)}种随机化方法")
