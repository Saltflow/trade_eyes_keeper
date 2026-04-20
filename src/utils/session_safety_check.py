#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Session数据安全检查装饰器
零配置，强制启用
检测随机数据写入Session并抛出异常
"""

import logging
import pandas as pd
from functools import wraps
from typing import Any, Callable

from .random_monitor import (
    is_value_from_random,
    clear_random_calls,
    SessionDataSafetyError,
)

logger = logging.getLogger(__name__)


def _is_caller_from_test() -> bool:
    """检查调用是否来自测试文件"""
    import traceback

    stack = traceback.extract_stack()
    for frame in stack:
        filename = frame[0] if len(frame) > 0 else ""
        if "tests/" in filename or "test_" in filename:
            return True
    return False


def _check_dataframe_for_random(df: pd.DataFrame, stock_code: str):
    """检查DataFrame中的数据是否来自random"""
    if df is None or df.empty:
        return

    if stock_code is None:
        stock_code = "unknown"

    # 如果来自测试文件，跳过检查（测试本身需要用random生成数据）
    if _is_caller_from_test():
        return

    # 检查数值列
    numeric_columns = ["open", "close", "high", "low", "volume", "amount", "ma60"]

    for col in numeric_columns:
        if col not in df.columns:
            continue

        # 检查每一行的数值
        for idx, value in df[col].items():
            if pd.isna(value):
                continue

            is_random, call_info = is_value_from_random(value)
            if is_random:
                error_msg = (
                    f"安全违规：检测到随机生成的数据被写入Session！\n"
                    f"股票代码: {stock_code}\n"
                    f"字段: {col}\n"
                    f"值: {value}\n"
                    f"Random函数: {call_info.get('function')}\n"
                    f"调用位置:\n{_format_stack(call_info.get('stack', []))}"
                )
                logger.critical(error_msg)
                raise SessionDataSafetyError(error_msg, call_info, value)


def _check_stock_price_data_for_random(stock_data: Any, stock_code: str):
    """检查StockPriceData对象中的数据是否来自random"""
    if stock_data is None:
        return

    if stock_code is None:
        stock_code = "unknown"

    # 如果来自测试文件，跳过检查（测试本身需要用random生成数据）
    if _is_caller_from_test():
        return

    # 检查PriceBar
    if hasattr(stock_data, "latest"):
        latest = stock_data.latest
        fields_to_check = [
            ("open", latest.open),
            ("close", latest.close),
            ("high", latest.high),
            ("low", latest.low),
            ("volume", latest.volume),
            ("amount", latest.amount),
        ]

        for field_name, value in fields_to_check:
            if value is None:
                continue

            is_random, call_info = is_value_from_random(value)
            if is_random:
                error_msg = (
                    f"安全违规：检测到随机生成的数据被写入Session！\n"
                    f"股票代码: {stock_code}\n"
                    f"字段: latest.{field_name}\n"
                    f"值: {value}\n"
                    f"Random函数: {call_info.get('function')}\n"
                    f"调用位置:\n{_format_stack(call_info.get('stack', []))}"
                )
                logger.critical(error_msg)
                raise SessionDataSafetyError(error_msg, call_info, value)

    # 检查技术指标
    indicator_fields = [
        ("ma60", stock_data.ma60),
        ("wma20", getattr(stock_data, "wma20", None)),
        ("wma30", getattr(stock_data, "wma30", None)),
        ("wma50", getattr(stock_data, "wma50", None)),
    ]

    for field_name, value in indicator_fields:
        if value is None:
            continue

        is_random, call_info = is_value_from_random(value)
        if is_random:
            error_msg = (
                f"安全违规：检测到随机生成的数据被写入Session！\n"
                f"股票代码: {stock_code}\n"
                f"字段: {field_name}\n"
                f"值: {value}\n"
                f"Random函数: {call_info.get('function')}\n"
                f"调用位置:\n{_format_stack(call_info.get('stack', []))}"
            )
            logger.critical(error_msg)
            raise SessionDataSafetyError(error_msg, call_info, value)


def _format_stack(stack: list) -> str:
    """格式化调用栈信息"""
    if not stack:
        return "  (无调用栈信息)"

    formatted = []
    for frame in stack:
        # frame格式: (filename, line_number, function_name, text)
        if len(frame) >= 4:
            filename, line_no, func_name, text = frame
            formatted.append(f'  File "{filename}", line {line_no}, in {func_name}')
            if text:
                formatted.append(f"    {text}")

    return "\n".join(formatted)


def safe_session_write(func: Callable) -> Callable:
    """Session写入安全检查装饰器

    零配置，强制启用
    检测到随机数据写入Session立即抛出异常
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        # 提取参数（先提取，在清空前检查）
        stock_code = None
        df = None
        stock_data = None

        if len(args) >= 3:
            # update_stock_data(self, session, stock_code, data)
            # update_stock_from_dataframe(self, session, stock_code, df, **kwargs)
            stock_code = args[2]

            if func.__name__ == "update_stock_data" and len(args) >= 4:
                stock_data = args[3]
            elif func.__name__ == "update_stock_from_dataframe" and len(args) >= 4:
                df = args[3]

        # 也检查kwargs
        if stock_code is None:
            stock_code = kwargs.get("stock_code")
        if df is None:
            df = kwargs.get("df")
        if stock_data is None:
            stock_data = kwargs.get("data")

        # 在写入前检查数据是否有random值
        try:
            if df is not None:
                _check_dataframe_for_random(df, stock_code)
            if stock_data is not None:
                _check_stock_price_data_for_random(stock_data, stock_code)
        except SessionDataSafetyError:
            # 检测到问题，直接抛出，不执行写入
            raise

        # 清空记录，为下一次做准备
        clear_random_calls()

        # 执行原始函数
        result = func(*args, **kwargs)
        return result

    logger.info(f"已应用Session安全检查到: {func.__name__}")
    return wrapper
