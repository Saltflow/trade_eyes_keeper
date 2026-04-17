#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据模型 - 最小化版本
仅包含核心股票价格数据
"""

import logging
from pydantic import BaseModel, Field, validator
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum

logger = logging.getLogger(__name__)


class AdjustmentType(str, Enum):
    NONE = "none"
    QFQ = "qfq"


class DataSource(str, Enum):
    SINA = "sina"
    TENCENT = "tencent"


class PriceBar(BaseModel):
    date: datetime
    open: Optional[float] = None
    close: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None

    @validator("open", "close", "high", "low", "amount")
    def round_3_decimals(cls, v):
        if v is not None:
            return round(v, 3)
        return v


class StockPriceData(BaseModel):
    stock_code: str
    data_source: DataSource
    adjustment_type: AdjustmentType
    last_updated: datetime
    latest: PriceBar
    ma60: Optional[float] = None
    wma20: Optional[float] = None
    wma30: Optional[float] = None
    wma50: Optional[float] = None
    dividend_per_share: Optional[float] = None
    dividend_yield: Optional[float] = None
    pe_ratio: Optional[float] = None

    @validator(
        "ma60",
        "wma20",
        "wma30",
        "wma50",
        "dividend_per_share",
        "dividend_yield",
        "pe_ratio",
    )
    def round_indicators(cls, v):
        if v is not None:
            return round(v, 3)
        return v

    @validator("latest")
    def validate_price_validity(cls, latest_bar):
        """
        价格有效性校验：低于0.1元的价格视为无效，自动设为None，避免生成0值警报
        """
        # 校验最低价
        if latest_bar.low is not None and latest_bar.low < 0.1:
            low_val = latest_bar.low
            latest_bar.low = None
            logger.warning(f"价格有效性校验：最低价{low_val:.2f}无效，已设为None")
        # 校验其他价格
        if latest_bar.open is not None and latest_bar.open < 0.1:
            latest_bar.open = None
        if latest_bar.close is not None and latest_bar.close < 0.1:
            latest_bar.close = None
        if latest_bar.high is not None and latest_bar.high < 0.1:
            latest_bar.high = None
        return latest_bar

    @validator("ma60")
    def validate_ma60_validity(cls, v):
        """MA60有效性校验"""
        if v is not None and v < 0.1:
            logger.warning(f"价格有效性校验：MA60{v:.2f}无效，已设为None")
            return None
        return v

    def to_dataframe(self):
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "date": self.latest.date,
                    "open": self.latest.open,
                    "close": self.latest.close,
                    "high": self.latest.high,
                    "low": self.latest.low,
                    "volume": self.latest.volume,
                    "amount": self.latest.amount,
                    "ma60": self.ma60,
                    "wma20": self.wma20,
                    "wma30": self.wma30,
                    "wma50": self.wma50,
                    "dividend_per_share": self.dividend_per_share,
                    "dividend_yield": self.dividend_yield,
                    "pe_ratio": self.pe_ratio,
                    "stock_code": self.stock_code,
                }
            ]
        )


class AlertStock(BaseModel):
    """警报股票 - 兼容单锚点和多锚点"""

    stock_code: str
    date: Optional[datetime] = None
    condition: Optional[str] = None
    low_price: float
    price: float  # 兼容字段，等于low_price
    ma60: Optional[float] = None
    anchor_name: Optional[str] = None
    anchor_value: Optional[float] = None
    interval_label: Optional[str] = None
    consecutive_days: int = 1
    price_difference: float
    percentage_difference: float
    percentage: Optional[float] = None

    @validator(
        "low_price",
        "price",
        "ma60",
        "anchor_value",
        "percentage",
        "price_difference",
        "percentage_difference",
    )
    def round_prices(cls, v):
        return round(v, 3) if v is not None else None

    @validator("price")
    def price_equals_low_price(cls, v, values):
        if "low_price" in values and v != values["low_price"]:
            return values["low_price"]  # 强制等于low_price
        return v

    @validator("percentage")
    def percentage_equals_percentage_difference(cls, v, values):
        if "percentage_difference" in values:
            return values["percentage_difference"]
        return v

    def to_dict(self):
        """转换为dict（兼容旧代码）"""
        d = {
            "stock_code": self.stock_code,
            "low_price": self.low_price,
            "price": self.low_price,
            "price_difference": self.price_difference,
            "percentage_difference": self.percentage_difference,
            "percentage": self.percentage_difference,
            "consecutive_days": self.consecutive_days,
        }
        if self.date:
            d["date"] = self.date
        if self.condition:
            d["condition"] = self.condition
        if self.ma60:
            d["ma60"] = self.ma60
        if self.anchor_name:
            d["anchor_name"] = self.anchor_name
        if self.anchor_value:
            d["anchor_value"] = self.anchor_value
        if self.interval_label:
            d["interval_label"] = self.interval_label
        return d


class SessionContext(BaseModel):
    """Session全局上下文 - 最小化版本"""

    session_id: str
    created_at: datetime = Field(default_factory=datetime.now)
    config: dict = Field(default_factory=dict)
    stocks_data: dict[str, StockPriceData] = Field(default_factory=dict)
    alerts: list[AlertStock] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)  # 错误日志字段
    # 扩展字段：用于存储非股票数据流
    analysis_results: dict[str, dict] = Field(default_factory=dict)  # LLM基本面分析
    announcements: dict[str, list] = Field(default_factory=dict)  # 公告数据
    financial_analysis_results: dict[str, list] = Field(
        default_factory=dict
    )  # 财报分析
    backtest_results: Optional[list] = None  # 回测结果

    def get_all_dataframe(self):
        """获取所有股票合并DataFrame（兼容旧代码）"""
        import pandas as pd

        return (
            pd.concat(
                [s.to_dataframe() for s in self.stocks_data.values()], ignore_index=True
            )
            if self.stocks_data
            else pd.DataFrame()
        )

    def get_alerts_as_dicts(self):
        """获取警报列表dict格式（兼容旧代码）"""
        return [a.to_dict() for a in self.alerts]
