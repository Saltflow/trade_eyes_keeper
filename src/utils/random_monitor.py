#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Random库监控器
自动包装random库，记录所有调用，防止随机数据写入Session
零配置，强制启用
"""

import random
import traceback
import logging
from typing import List, Dict, Any, Callable

logger = logging.getLogger(__name__)


class SessionDataSafetyError(Exception):
    """Session数据安全异常：检测到随机生成的数据被写入Session"""

    def __init__(
        self,
        message: str,
        random_call_info: Dict[str, Any],
        data_value: Any,
    ):
        self.message = message
        self.random_call_info = random_call_info
        self.data_value = data_value
        super().__init__(message)


class RandomMonitor:
    """Random库监控器 - 单例模式"""

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if RandomMonitor._initialized:
            return

        self.calls: List[Dict[str, Any]] = []
        self._original_random = None
        self._wrapped = False

        # 自动包装
        self.wrap()
        RandomMonitor._initialized = True
        logger.info("Random监控器已启动（零配置，强制启用）")

    def wrap(self):
        """包装random库所有公开方法"""
        if self._wrapped:
            return

        self._original_random = random
        self._wrapped = True

        # 获取所有公开方法
        for name in dir(self._original_random):
            if name.startswith("_"):
                continue

            attr = getattr(self._original_random, name)
            if not callable(attr):
                continue

            # 创建包装函数
            wrapper = self._create_wrapper(name, attr)
            setattr(random, name, wrapper)

        logger.debug(f"已包装 {len(dir(self._original_random))} 个random方法")

    def _create_wrapper(self, func_name: str, original_func: Callable):
        """创建包装函数"""

        def wrapper(*args, **kwargs):
            # 调用原始函数
            result = original_func(*args, **kwargs)

            # 记录调用
            call_info = {
                "function": func_name,
                "args": args,
                "kwargs": kwargs,
                "result": result,
                "stack": traceback.extract_stack()[:-1],  # 去掉wrapper本身
            }
            self.calls.append(call_info)

            # 限制记录数量，防止内存泄漏
            if len(self.calls) > 10000:
                self.calls = self.calls[-5000:]

            return result

        return wrapper

    def is_value_from_random(self, value: Any) -> tuple[bool, Dict[str, Any]]:
        """检查某个值是否来自random调用

        Args:
            value: 要检查的值

        Returns:
            (是否来自random, 调用信息)
        """
        # 先检查精确匹配
        for call in reversed(self.calls):
            if call["result"] == value:
                return True, call

        # 检查浮点数近似匹配（考虑浮点精度和四舍五入）
        if isinstance(value, float):
            for call in reversed(self.calls):
                if isinstance(call["result"], float):
                    # 1. 检查浮点精度匹配
                    if abs(call["result"] - value) < 1e-10:
                        return True, call
                    # 2. 检查四舍五入到3位小数后的匹配（Pydantic会四舍五入）
                    if round(call["result"], 3) == round(value, 3):
                        return True, call

        return False, {}

    def clear_calls(self):
        """清空调用记录"""
        self.calls = []
        logger.debug("Random调用记录已清空")


# 全局单例，自动初始化
_monitor = RandomMonitor()


# 导出便捷函数
def is_value_from_random(value: Any) -> tuple[bool, Dict[str, Any]]:
    """检查值是否来自random（便捷函数）"""
    return _monitor.is_value_from_random(value)


def clear_random_calls():
    """清空random调用记录（便捷函数）"""
    _monitor.clear_calls()


def get_random_monitor() -> RandomMonitor:
    """获取监控器实例（便捷函数）"""
    return _monitor
