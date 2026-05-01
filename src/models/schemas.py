#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据模型 - 扁平化版本
StockPriceData 展平为单一模型，消除 DataFrame ↔ 模型 转换丢字段问题
"""

import logging
import math
from pydantic import BaseModel, Field, validator
from datetime import datetime, date
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
    """保留向后兼容的价格条模型（不再被 StockPriceData 使用）"""

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
    """
    统一股票数据模型 — 扁平化，全程使用同一个类读写
    替代旧的 StockPriceData + PriceBar 嵌套结构
    """

    # ── 股票标识 ──
    stock_code: str
    stock_name: Optional[str] = None

    # ── 行情数据（最新一天） ──
    date: Optional[datetime] = None
    open: Optional[float] = None
    close: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None

    # ── 衍生行情 ──
    amplitude: Optional[float] = None  # 振幅 (high-low)/open*100
    change_pct: Optional[float] = None  # 涨跌幅 %
    change: Optional[float] = None  # 涨跌额
    turnover: Optional[float] = None  # 换手率 %

    # ── 技术指标 ──
    ma60: Optional[float] = None
    wma20: Optional[float] = None
    wma30: Optional[float] = None
    wma50: Optional[float] = None

    # ── 基本面数据 ──
    dividend_per_share: Optional[float] = None  # 每股分红（元）
    dividend_yield: Optional[float] = None  # 股息率（%）
    earnings_growth: Optional[float] = None  # 业绩增长率（%）
    pe_ratio: Optional[float] = None  # 市盈率
    pb_ratio: Optional[float] = None  # 市净率
    roe: Optional[float] = None  # 净资产收益率（%）
    debt_ratio: Optional[float] = None  # 资产负债率（%）

    # ── 元数据 ──
    data_source: Optional[DataSource] = None
    adjustment_type: Optional[AdjustmentType] = None
    last_updated: datetime = Field(default_factory=lambda: datetime.now())

    class Config:
        extra = "allow"  # 允许动态锚点字段（alerts.yaml 自定义指标名）

    # ── 统一 Validator：所有浮点字段保留3位小数 ──
    _float_fields = [
        "open",
        "close",
        "high",
        "low",
        "amount",
        "amplitude",
        "change_pct",
        "change",
        "turnover",
        "ma60",
        "wma20",
        "wma30",
        "wma50",
        "dividend_per_share",
        "dividend_yield",
        "earnings_growth",
        "pe_ratio",
        "pb_ratio",
        "roe",
        "debt_ratio",
    ]

    @validator(*_float_fields, pre=True, always=False)
    def round_floats(cls, v):
        if v is not None:
            try:
                return round(float(v), 3)
            except (ValueError, TypeError):
                return None
        return v

    # ── 价格有效性校验 ──
    @validator("low", "open", "close", "high")
    def validate_price_validity(cls, v, field):
        """低于0.1元的价格视为无效，自动设为None"""
        if v is not None and v < 0.1:
            logger.warning(f"价格有效性校验：{field.name}={v:.2f}无效，已设为None")
            return None
        return v

    @validator("ma60")
    def validate_ma60_validity(cls, v):
        """MA60有效性校验"""
        if v is not None and v < 0.1:
            logger.warning(f"MA60={v:.2f}无效，已设为None")
            return None
        return v

    # ── DataFrame 双向转换 ──

    def to_dict(self) -> dict:
        """
        StockPriceData → dict（用于 DataFrame 重建）
        导出 ALL 字段，无丢失
        """
        result = {}
        # 遍历模型定义的字段
        for field_name in self.__fields__:
            value = getattr(self, field_name, None)
            if isinstance(value, (DataSource, AdjustmentType)):
                value = value.value
            elif isinstance(value, (datetime, date)):
                value = value.isoformat()
            result[field_name] = value
        # 也导出 extra 动态字段（在 __dict__ 中但不在 __fields__ 中的键）
        for key, value in self.__dict__.items():
            if key not in self.__fields__ and not key.startswith("_"):
                result[key] = value
        return result

    def to_dataframe(self):
        """导出为单行 DataFrame（兼容旧接口）"""
        import pandas as pd

        return pd.DataFrame([self.to_dict()])

    @classmethod
    def from_dataframe_row(
        cls,
        row: "pd.Series",
        stock_code: str,
        data_source: str = "sina",
        adjustment_type: str = "none",
    ) -> Optional["StockPriceData"]:
        """
        DataFrame 一行 → StockPriceData
        映射 ALL 列，无丢失
        """
        import pandas as pd

        if row is None:
            return None

        # 必要价格字段校验
        required = ["open", "close", "high", "low"]
        for f in required:
            val = row.get(f)
            if val is None or (isinstance(val, float) and (pd.isna(val) or val <= 0)):
                logger.error(f"股票 {stock_code} 字段 {f} 无效: {val}")
                return None

        # 构建字段映射：从 DataFrame 列名 → StockPriceData 字段
        field_map: Dict[str, Any] = {
            "stock_code": stock_code,
        }

        # 自动映射：遍历 StockPriceData 所有字段，从 row 中取同名列
        for field_name in cls.__fields__:
            if field_name == "stock_code":
                continue
            if field_name in ("data_source", "adjustment_type", "last_updated"):
                continue  # 元数据字段由参数/默认值控制
            val = row.get(field_name)
            if val is not None:
                if isinstance(val, float) and (pd.isna(val) or math.isnan(val)):
                    field_map[field_name] = None
                else:
                    field_map[field_name] = val
            else:
                field_map[field_name] = None

        # 元数据
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
        field_map["data_source"] = ds
        field_map["adjustment_type"] = at

        # 动态字段（row 中有但模型未定义的列）
        extra_fields = {}
        for col in row.index:
            if col not in cls.__fields__ and col != "stock_code":
                val = row.get(col)
                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                    extra_fields[col] = val

        try:
            instance = cls(**field_map, **extra_fields)
            logger.debug(f"股票 {stock_code} 从DataFrame创建StockPriceData成功")
            return instance
        except Exception as e:
            logger.error(f"股票 {stock_code} 创建StockPriceData失败: {e}")
            return None


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
            return values["low_price"]
        return v

    @validator("percentage")
    def percentage_equals_percentage_difference(cls, v, values):
        if "percentage_difference" in values:
            return values["percentage_difference"]
        return v

    def to_dict(self):
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
    """Session全局上下文 - 统一模型版本"""

    session_id: str
    created_at: datetime = Field(default_factory=datetime.now)
    config: dict = Field(default_factory=dict)
    stocks_data: dict[str, StockPriceData] = Field(default_factory=dict)
    alerts: list[AlertStock] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # 扩展字段
    analysis_results: dict[str, dict] = Field(default_factory=dict)
    announcements: dict[str, list] = Field(default_factory=dict)
    financial_analysis_results: dict[str, list] = Field(default_factory=dict)
    portfolio_results: Optional[dict] = None
    signal_scan: Optional[object] = None  # ScanResult from signal_scanner
    backtest: Optional[dict] = None  # backtest results {group: dict}

    def get_all_dataframe(self):
        """获取所有股票合并DataFrame（ALL列，无丢失）"""
        import pandas as pd

        if not self.stocks_data:
            return pd.DataFrame()

        dfs = []
        for s in self.stocks_data.values():
            d = s.to_dict()
            dfs.append(pd.DataFrame([d]))

        return pd.concat(dfs, ignore_index=True)

    def get_alerts_as_dicts(self):
        """返回告警列表（plain dict 直接通过，AlertStock 则转 dict）"""
        result = []
        for a in self.alerts:
            if isinstance(a, dict):
                result.append(a)
            elif hasattr(a, "to_dict"):
                result.append(a.to_dict())
            elif hasattr(a, "dict"):
                result.append(a.dict())
            else:
                result.append({"stock_code": str(a)})
        return result
