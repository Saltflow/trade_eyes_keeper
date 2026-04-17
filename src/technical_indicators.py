#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标计算器 - Session统一数据源版本
从 SessionContext 读取锚点，不再实时计算
"""

import logging
import warnings

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """技术指标计算器 - 从 Session 读取版本"""

    def __init__(self, session_manager=None, session_context=None):
        """
        初始化技术指标计算器

        Args:
            session_manager: SessionManager 实例（可选）
            session_context: SessionContext 实例（可选，优先使用）
        """
        self.session_manager = session_manager
        self.session_context = session_context
        self._cache = {}
        logger.info("TechnicalIndicators 初始化完成（Session读取版本）")

    def _get_stock_data(self, stock_code: str):
        """从 Session 获取股票数据"""
        if self.session_context and stock_code in self.session_context.stocks_data:
            return self.session_context.stocks_data[stock_code]

        if self.session_manager:
            # 如果有 session_manager，但没有直接的 session_context
            # 这里假设我们需要通过其他方式获取，或者返回 None
            logger.warning(
                f"TechnicalIndicators 需要 SessionContext 才能获取 {stock_code} 的数据"
            )

        logger.warning(f"Session 中无股票数据: {stock_code}")
        return None

    def calculate_ma(self, data, window, price_col="close"):
        """
        计算移动平均（保留用于向后兼容）

        已废弃：建议从 SessionContext 读取预计算的锚点
        """
        warnings.warn(
            "calculate_ma 已废弃，建议从 SessionContext 读取预计算的锚点",
            DeprecationWarning,
            stacklevel=2,
        )
        import pandas as pd
        import numpy as np

        if data.empty or price_col not in data.columns:
            return pd.Series([], dtype=float)

        try:
            ma = data[price_col].rolling(window=window, min_periods=1).mean()
            if ma.notnull().any():
                valid = ma[ma.notnull()]
                price = data[price_col][valid.index]
                if valid.min() < price.min() * 0.9 or valid.max() > price.max() * 1.1:
                    logger.warning("MA值超出合理范围")
            return ma
        except Exception:
            return pd.Series([np.nan] * len(data), index=data.index)

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

        if window == 20 and stock_data.wma20 is not None:
            return stock_data.wma20
        elif window == 30 and stock_data.wma30 is not None:
            return stock_data.wma30
        elif window == 50 and stock_data.wma50 is not None:
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

        if window == 60 and stock_data.ma60 is not None:
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

        anchors = {
            "ma60": stock_data.ma60,
            "wma20": stock_data.wma20,
            "wma30": stock_data.wma30,
            "wma50": stock_data.wma50,
        }

        valid_count = sum(1 for v in anchors.values() if v is not None)
        logger.info(f"锚点读取: {stock_code}, 有效{valid_count}/4")
        return anchors

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        logger.debug("TechnicalIndicators 缓存已清空")
