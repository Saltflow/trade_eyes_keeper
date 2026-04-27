"""
测试警报状态管理器（抗硬编码随机测试）
"""

import random
import tempfile
import os
from pathlib import Path
from datetime import datetime, timedelta

import pytest

from src.alerting.alert_state_manager import AlertStateManager


@pytest.fixture
def temp_cache_dir():
    """创建临时缓存目录"""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def random_interval_labels():
    """生成随机区间标签"""
    base_labels = [
        "<-10%",
        "(-10%, -5%]",
        "(-5%, 0)",
        "[0%, 5%)",
        "[5%, 10%)",
        "[10%, 15%)",
        ">=15%",
    ]
    return random.sample(base_labels, k=random.randint(3, 7))


def test_new_stock_allows_alert(
    temp_cache_dir, random_stock_code, random_interval_labels
):
    """测试新股票允许警报（连续天数=1）"""
    asm = AlertStateManager(temp_cache_dir)
    interval = random.choice(random_interval_labels)

    should_alert, consecutive = asm.should_alert(random_stock_code, "ma60", interval)
    assert should_alert is True
    assert consecutive == 1


def test_same_day_no_increase(
    temp_cache_dir, random_stock_code, random_interval_labels
):
    """测试同一天重复检查不增加连续天数"""
    asm = AlertStateManager(temp_cache_dir)
    interval = random.choice(random_interval_labels)
    today = datetime.now().date().isoformat()

    asm.update(random_stock_code, "wma20", interval, today)

    should_alert, consecutive = asm.should_alert(
        random_stock_code, "wma20", interval, today
    )
    assert should_alert is True
    assert consecutive == 1


def test_consecutive_days_increase(
    temp_cache_dir, random_stock_code, random_interval_labels
):
    """测试连续天数正确累加（最大5天）"""
    asm = AlertStateManager(temp_cache_dir)
    interval = random.choice(random_interval_labels)
    base_date = datetime.now() - timedelta(days=5)

    for i in range(5):
        current_date = (base_date + timedelta(days=i)).date().isoformat()
        asm.update(random_stock_code, "wma30", interval, current_date)

        should_alert, consecutive = asm.should_alert(
            random_stock_code, "wma30", interval, current_date
        )
        assert should_alert is True
        assert consecutive == i + 1


def test_sixth_day_rejects(temp_cache_dir, random_stock_code, random_interval_labels):
    """测试第6天正确拒绝警报"""
    asm = AlertStateManager(temp_cache_dir)
    interval = random.choice(random_interval_labels)
    base_date = datetime.now() - timedelta(days=6)

    # 更新前5天
    for i in range(5):
        current_date = (base_date + timedelta(days=i)).date().isoformat()
        asm.update(random_stock_code, "ma60", interval, current_date)

    # 第6天
    day6_date = (base_date + timedelta(days=5)).date().isoformat()
    should_alert, consecutive = asm.should_alert(
        random_stock_code, "ma60", interval, day6_date
    )
    assert should_alert is False
    assert consecutive == 6


def test_new_interval_resets_count(
    temp_cache_dir, random_stock_code, random_interval_labels
):
    """测试区间切换正确重置计数器"""
    asm = AlertStateManager(temp_cache_dir)

    if len(random_interval_labels) >= 2:
        interval1, interval2 = random.sample(random_interval_labels, 2)
    else:
        interval1 = "(-10%, -5%]"
        interval2 = "[5%, 10%)"

    base_date = datetime.now() - timedelta(days=3)

    # 更新interval1的前2天
    for i in range(2):
        current_date = (base_date + timedelta(days=i)).date().isoformat()
        asm.update(random_stock_code, "wma50", interval1, current_date)

    # 切换到新interval2
    day3_date = (base_date + timedelta(days=2)).date().isoformat()
    asm.reset_for_new_interval(random_stock_code, "wma50", interval2)
    asm.update(random_stock_code, "wma50", interval2, day3_date)

    should_alert, consecutive = asm.should_alert(
        random_stock_code, "wma50", interval2, day3_date
    )
    assert should_alert is True
    assert consecutive == 1


def test_clear_all(temp_cache_dir, random_stock_code, random_interval_labels):
    """测试清除所有状态功能"""
    asm = AlertStateManager(temp_cache_dir)
    interval = random.choice(random_interval_labels)

    asm.update(random_stock_code, "ma60", interval)
    asm.update(random_stock_code, "wma20", interval)

    assert len(asm._state.get("alerts", {})) > 0

    asm.clear_all()

    assert len(asm._state.get("alerts", {})) == 0


def test_persistence(temp_cache_dir, random_stock_code, random_interval_labels):
    """测试状态持久化（保存和加载）"""
    interval = random.choice(random_interval_labels)

    # 创建第一个实例并更新
    asm1 = AlertStateManager(temp_cache_dir)
    asm1.update(random_stock_code, "wma30", interval)

    # 创建第二个实例（应该加载保存的状态）
    asm2 = AlertStateManager(temp_cache_dir)
    key = asm2._key(random_stock_code, "wma30", interval)

    assert key in asm2._state.get("alerts", {})
