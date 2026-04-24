#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据转换工具 - 扁平化版本
DataFrame ↔ StockPriceData 双向转换，无字段丢失
"""

import logging
from typing import Optional, Dict, Any
import pandas as pd

from .schemas import (
    StockPriceData,
    AlertStock,
)

logger = logging.getLogger(__name__)


def dataframe_to_stock_price_data(
    df: pd.DataFrame,
    stock_code: str,
    data_source: str = "sina",
    adjustment_type: str = "none",
) -> Optional[StockPriceData]:
    """
    将DataFrame最后一行转换为StockPriceData（统一入口）
    委托给 StockPriceData.from_dataframe_row，映射ALL列
    """
    # 入参校验
    if df is None:
        logger.error(f"股票 {stock_code} DataFrame为None")
        return None
    if df.empty:
        logger.warning(f"股票 {stock_code} DataFrame为空")
        return None
    if not stock_code or not isinstance(stock_code, str):
        logger.error(f"股票代码无效: {stock_code}")
        return None

    try:
        latest_row = df.iloc[-1]
        return StockPriceData.from_dataframe_row(
            latest_row,
            stock_code=stock_code,
            data_source=data_source,
            adjustment_type=adjustment_type,
        )
    except Exception as e:
        logger.error(f"股票 {stock_code} 转换StockPriceData失败: {e}")
        return None


def stock_price_data_list_to_dataframe(
    data_list: list[StockPriceData],
) -> pd.DataFrame:
    """
    多个StockPriceData → 合并DataFrame（ALL列，无丢失）

    Args:
        data_list: StockPriceData列表

    Returns:
        pd.DataFrame: 包含所有股票最新数据的合并DataFrame
    """
    if not data_list:
        return pd.DataFrame()

    dfs = []
    for spd in data_list:
        d = spd.to_dict()
        dfs.append(pd.DataFrame([d]))

    return pd.concat(dfs, ignore_index=True)


def alert_dict_to_alert_stock(alert_dict: dict) -> Optional[AlertStock]:
    """
    将警报dict转换为AlertStock对象
    """
    try:

        def safe_float(value, default=None):
            if value is None:
                return default
            try:
                return float(value)
            except (ValueError, TypeError):
                return default

        def safe_int(value, default=1):
            if value is None:
                return default
            try:
                return int(value)
            except (ValueError, TypeError):
                return default

        return AlertStock(
            stock_code=alert_dict.get("stock_code", ""),
            date=alert_dict.get("date"),
            condition=alert_dict.get("condition"),
            low_price=safe_float(alert_dict.get("low_price"), 0.0),
            price=safe_float(alert_dict.get("price", alert_dict.get("low_price")), 0.0),
            ma60=safe_float(alert_dict.get("ma60")),
            anchor_name=alert_dict.get("anchor_name"),
            anchor_value=safe_float(alert_dict.get("anchor_value")),
            interval_label=alert_dict.get("interval_label"),
            percentage=safe_float(alert_dict.get("percentage")),
            consecutive_days=safe_int(alert_dict.get("consecutive_days"), 1),
            price_difference=safe_float(alert_dict.get("price_difference"), 0.0),
            percentage_difference=safe_float(
                alert_dict.get("percentage_difference"), 0.0
            ),
        )
    except Exception as e:
        logger.warning(f"转换AlertStock失败: {e}")
        return None
