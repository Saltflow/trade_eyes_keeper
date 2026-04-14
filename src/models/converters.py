#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据转换工具 - 最小化版本
DataFrame ↔ Model 双向转换
"""

import logging
from datetime import datetime
from typing import Optional
import pandas as pd

from .schemas import (
    PriceBar,
    StockPriceData,
    DataSource,
    AdjustmentType,
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
    将DataFrame转换为StockPriceData
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

        # 必要字段校验
        required_price_fields = ["open", "close", "high", "low"]
        for field in required_price_fields:
            if (
                field not in latest_row
                or pd.isna(latest_row.get(field))
                or latest_row.get(field) <= 0
            ):
                logger.error(
                    f"股票 {stock_code} 字段 {field} 无效: {latest_row.get(field)}"
                )
                return None

        ds = (
            DataSource(data_source)
            if data_source in DataSource.__members__
            else DataSource.SINA
        )
        at = (
            AdjustmentType(adjustment_type)
            if adjustment_type in AdjustmentType.__members__
            else AdjustmentType.NONE
        )

        latest_bar = PriceBar(
            date=latest_row.get("date"),
            open=float(latest_row.get("open", 0)),
            close=float(latest_row.get("close", 0)),
            high=float(latest_row.get("high", 0)),
            low=float(latest_row.get("low"))
            if latest_row.get("low") is not None
            else None,
            volume=float(latest_row.get("volume", 0)),
            amount=float(latest_row.get("amount"))
            if pd.notna(latest_row.get("amount"))
            else None,
        )

        return StockPriceData(
            stock_code=stock_code,
            data_source=ds,
            adjustment_type=at,
            last_updated=datetime.now(),
            latest=latest_bar,
            ma60=float(latest_row.get("ma60"))
            if pd.notna(latest_row.get("ma60"))
            else None,
            dividend_per_share=float(latest_row.get("dividend_per_share"))
            if pd.notna(latest_row.get("dividend_per_share"))
            else None,
            dividend_yield=float(latest_row.get("dividend_yield"))
            if pd.notna(latest_row.get("dividend_yield"))
            else None,
            pe_ratio=float(latest_row.get("pe_ratio"))
            if pd.notna(latest_row.get("pe_ratio"))
            else None,
        )

    except Exception as e:
        logger.error(
            f"股票 {stock_code} 转换StockPriceData失败: {str(e)}，行数据: {latest_row.to_dict() if 'latest_row' in locals() else '无'}"
        )
        return None


def alert_dict_to_alert_stock(alert_dict: dict) -> Optional[AlertStock]:
    """
    将警报dict转换为AlertStock对象
    """
    try:
        # 安全地获取数值字段
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
