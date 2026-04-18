#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标计算器 - Session统一数据源版本
支持从配置文件读取指标定义并批量计算
"""

import logging
import warnings
import yaml
import os
from typing import Dict, Optional, Any
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """技术指标计算器 - 支持配置驱动的批量计算"""

    def __init__(self, session_manager=None, session_context=None, config_path=None):
        """
        初始化技术指标计算器

        Args:
            session_manager: SessionManager 实例（可选）
            session_context: SessionContext 实例（可选，优先使用）
            config_path: 配置文件路径（可选，默认使用 config/alerts.yaml）
        """
        self.session_manager = session_manager
        self.session_context = session_context
        self._cache = {}

        # 加载指标配置
        self.config = self._load_config(config_path)
        self.anchors_config = self.config.get("anchors", [])

        logger.info(
            f"TechnicalIndicators 初始化完成，加载 {len(self.anchors_config)} 个指标配置"
        )

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """加载指标配置文件"""
        if config_path is None:
            # 默认配置路径
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config", "alerts.yaml"
            )

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            logger.debug(f"成功加载指标配置: {config_path}")
            return config or {}
        except Exception as e:
            logger.warning(f"加载指标配置失败，使用默认配置: {e}")
            # 默认配置
            return {"anchors": [{"name": "ma60", "type": "daily_ma", "window": 60}]}

    def _get_stock_data(self, stock_code: str):
        """从 Session 获取股票数据"""
        if self.session_context and stock_code in self.session_context.stocks_data:
            return self.session_context.stocks_data[stock_code]

        if self.session_manager:
            logger.warning(
                f"TechnicalIndicators 需要 SessionContext 才能获取 {stock_code} 的数据"
            )

        logger.warning(f"Session 中无股票数据: {stock_code}")
        return None

    def calculate_ma(
        self,
        data: pd.DataFrame,
        window: int,
        price_col: str = "close",
        min_periods: int = 1,
    ) -> pd.Series:
        """
        计算移动平均

        Args:
            data: 包含价格数据的 DataFrame
            window: 窗口大小
            price_col: 价格列名
            min_periods: 最小周期数

        Returns:
            移动平均 Series
        """
        if data.empty or price_col not in data.columns:
            return pd.Series([np.nan] * len(data), index=data.index)

        try:
            ma = data[price_col].rolling(window=window, min_periods=min_periods).mean()
            return ma
        except Exception as e:
            logger.warning(f"计算MA{window}失败: {e}")
            return pd.Series([np.nan] * len(data), index=data.index)

    def calculate_indicators(
        self, data: pd.DataFrame, stock_code: Optional[str] = None
    ) -> pd.DataFrame:
        """
        根据配置计算所有技术指标

        Args:
            data: 包含原始价格数据的 DataFrame
            stock_code: 股票代码（用于日志）

        Returns:
            添加了指标列的 DataFrame
        """
        if data.empty:
            return data

        result_data = data.copy()

        for anchor in self.anchors_config:
            name = anchor.get("name")
            anchor_type = anchor.get("type")
            window = anchor.get("window")

            if not name or not anchor_type or not window:
                logger.warning(f"跳过无效的指标配置: {anchor}")
                continue

            try:
                if anchor_type == "daily_ma":
                    # 日线移动平均
                    result_data[name] = self.calculate_ma(
                        result_data, window=window, min_periods=1
                    )
                    logger.debug(
                        f"计算指标 {name} (MA{window}) 完成"
                        + (f" for {stock_code}" if stock_code else "")
                    )
                elif anchor_type == "weekly_ma":
                    # 周线移动平均（暂未实现，标记为 None）
                    result_data[name] = np.nan
                    logger.debug(f"周线指标 {name} 暂未实现")
                else:
                    logger.warning(f"未知的指标类型: {anchor_type}")
            except Exception as e:
                logger.warning(f"计算指标 {name} 失败: {e}")
                result_data[name] = np.nan

        return result_data

    def calculate_weekly_ma(self, stock_code, window, weeks=None):
        """
        计算周线MA（保留用于向后兼容）

        已废弃：建议从 SessionContext 读取预计算的锚点
        """
        warnings.warn(
            "calculate_weekly_ma 已废弃，建议从 SessionContext 读取预计算的锚点",
            DeprecationWarning,
            stacklevel=2,
        )

        stock_data = self._get_stock_data(stock_code)
        if stock_data is None:
            return None

        if (
            window == 20
            and hasattr(stock_data, "wma20")
            and stock_data.wma20 is not None
        ):
            return stock_data.wma20
        elif (
            window == 30
            and hasattr(stock_data, "wma30")
            and stock_data.wma30 is not None
        ):
            return stock_data.wma30
        elif (
            window == 50
            and hasattr(stock_data, "wma50")
            and stock_data.wma50 is not None
        ):
            return stock_data.wma50

        logger.warning(f"Session 中无 wma{window} 数据: {stock_code}")
        return None

    def calculate_daily_ma(self, stock_code, window, days=None):
        """
        计算日线MA（保留用于向后兼容）

        已废弃：建议从 SessionContext 读取预计算的锚点
        """
        warnings.warn(
            "calculate_daily_ma 已废弃，建议从 SessionContext 读取预计算的锚点",
            DeprecationWarning,
            stacklevel=2,
        )

        stock_data = self._get_stock_data(stock_code)
        if stock_data is None:
            return None

        if window == 60 and hasattr(stock_data, "ma60") and stock_data.ma60 is not None:
            return stock_data.ma60

        logger.warning(f"Session 中无 ma{window} 数据: {stock_code}")
        return None

    def get_all_anchors(self, stock_code):
        """
        获取所有锚点 - 从 Session 读取

        Args:
            stock_code: 股票代码

        Returns:
            dict: 包含所有锚点的字典 {'ma60': ..., 'wma20': ..., ...}
        """
        stock_data = self._get_stock_data(stock_code)
        if stock_data is None:
            logger.warning(f"无法获取股票数据，返回空锚点: {stock_code}")
            return {
                "ma60": None,
                "wma20": None,
                "wma30": None,
                "wma50": None,
            }

        anchors = {}
        # 从配置中读取所有锚点名称
        for anchor in self.anchors_config:
            name = anchor.get("name")
            if name:
                anchors[name] = getattr(stock_data, name, None)

        # 确保返回默认锚点（向后兼容）
        default_anchors = ["ma60", "wma20", "wma30", "wma50"]
        for name in default_anchors:
            if name not in anchors:
                anchors[name] = getattr(stock_data, name, None)

        valid_count = sum(1 for v in anchors.values() if v is not None)
        logger.info(f"锚点读取: {stock_code}, 有效{valid_count}/{len(anchors)}")
        return anchors

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        logger.debug("TechnicalIndicators 缓存已清空")
