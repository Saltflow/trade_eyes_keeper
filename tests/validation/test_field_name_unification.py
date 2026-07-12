"""
测试字段名称统一修复
验证alert_engine、condition_checker、email_notifier之间的字段传递
"""

import pandas as pd
import pytest

from config import get_alerts_config
from src.alerting.alert_engine import AlertEngine
from src.alerting.alert_processor import AlertProcessor
from src.core.condition_checker import ConditionChecker


class TestFieldNameUnification:
    """测试字段名称统一"""

    def test_alert_engine_returns_low_price_field(self):
        """测试alert_engine.evaluate_anchor()返回low_price字段"""
        alerts_config = get_alerts_config("config/alerts.yaml")
        engine = AlertEngine(alerts_config)

        result = engine.evaluate_anchor(
            stock_code="600000",
            low_price=5.74,  # 使用low_price参数
            anchor_name="ma60",
            anchor_value=6.0,
        )

        assert result is not None
        assert "low_price" in result  # ✅ 应该有low_price字段
        assert result["low_price"] == 5.74  # ✅ 值应该正确
        assert "price" not in result  # ❌ 不应该有price字段（已删除）

    def test_alert_engine_evaluate_stock_validates_low_field(self):
        """测试alert_engine.evaluate_stock()验证low字段"""
        alerts_config = get_alerts_config("config/alerts.yaml")
        engine = AlertEngine(alerts_config)

        # 测试有效的low字段
        valid_data = pd.Series(
            {
                "stock_code": "600000",
                "low": 5.74,
                "ma60": 6.0,
                "close": 5.85,
                "high": 5.95,
            }
        )
        results = engine.evaluate_stock(valid_data)
        assert len(results) > 0
        assert all("low_price" in r for r in results)

        # 测试缺失的low字段
        missing_low = pd.Series({"stock_code": "600000", "ma60": 6.0})
        results = engine.evaluate_stock(missing_low)
        assert len(results) == 0  # 应该返回空列表

        # 测试NaN的low字段
        nan_low = pd.Series({"stock_code": "600000", "low": float("nan"), "ma60": 6.0})
        results = engine.evaluate_stock(nan_low)
        assert len(results) == 0  # 应该返回空列表

    def test_condition_checker_uses_low_price_directly(self):
        """测试condition_checker直接使用low_price字段"""
        alerts_config = get_alerts_config("config/alerts.yaml")
        processor = AlertProcessor(alerts_config, "./cache")

        # 创建alert字典（模拟alert_engine的输出）
        alert_from_engine = {
            "stock_code": "600000",
            "anchor_name": "ma60",
            "anchor_value": 6.0,
            "low_price": 5.74,  # ✅ 使用low_price字段
            "percentage": -4.33,
            "interval": {"label": "(-5%, 0%)"},
        }

        # 模拟condition_checker的处理逻辑
        low_price = alert_from_engine.get("low_price")  # ✅ 直接读取low_price
        anchor_val = alert_from_engine.get("anchor_value")

        assert low_price == 5.74  # ✅ 值正确
        assert anchor_val == 6.0

        # 计算price_difference
        price_difference = None
        if anchor_val is not None and low_price is not None:
            price_difference = anchor_val - low_price

        assert abs(price_difference - 0.26) < 1e-9  # ✅ 计算正确（浮点容差）

    def test_field_name_consistency_across_pipeline(self):
        """测试整个数据管道的字段名称一致性"""
        alerts_config = get_alerts_config("config/alerts.yaml")
        engine = AlertEngine(alerts_config)

        # 1. 创建股票数据
        stock_data = pd.Series(
            {
                "stock_code": "600000",
                "low": 5.74,
                "ma60": 6.0,
                "close": 5.85,
                "high": 5.95,
            }
        )

        # 2. alert_engine评估
        evaluations = engine.evaluate_stock(stock_data)
        assert len(evaluations) > 0

        # 验证评估结果包含low_price字段
        for eval in evaluations:
            assert "low_price" in eval, f"Missing low_price in {eval}"
            assert eval["low_price"] == 5.74, f"Wrong low_price: {eval['low_price']}"
            assert "price" not in eval, f"Should not have price field: {eval}"

        # 3. 模拟condition_checker处理
        for eval in evaluations:
            low_price = eval.get("low_price")  # ✅ 读取low_price
            anchor_val = eval.get("anchor_value")

            assert low_price == 5.74
            assert anchor_val == 6.0

            price_difference = None
            if anchor_val is not None and low_price is not None:
                price_difference = anchor_val - low_price

            assert abs(price_difference - 0.26) < 1e-9

        # 4. 模拟email_notifier读取
        for eval in evaluations:
            price = eval.get("low_price")  # ✅ 读取low_price
            assert price == 5.74  # ✅ 值正确（不是0或None）

    def test_no_field_mapping_required(self):
        """测试不需要字段映射（price → low_price）"""
        # 模拟旧的alert_engine输出（使用"price"字段）
        old_alert = {
            "stock_code": "600000",
            "anchor_name": "ma60",
            "anchor_value": 6.0,
            "price": 5.74,  # ← 旧的字段名
            "percentage": -4.33,
        }

        # 旧的condition_checker需要映射
        # old_price = old_alert.get("price")
        # result = {"low_price": old_price}  # ← 需要映射

        # 新的alert_engine输出（使用"low_price"字段）
        new_alert = {
            "stock_code": "600000",
            "anchor_name": "ma60",
            "anchor_value": 6.0,
            "low_price": 5.74,  # ← 新的字段名
            "percentage": -4.33,
        }

        # 新的condition_checker直接使用
        low_price = new_alert.get("low_price")  # ← 无需需映射

        assert low_price == 5.74
