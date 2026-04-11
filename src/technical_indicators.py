import logging
import random
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    def __init__(self, historical_data_manager):
        self.hdm = historical_data_manager
        self._cache = {}
        random.seed(random.randint(1, 10000))

    def calculate_ma(self, data, window, price_col="close"):
        """计算移动平均"""
        if data.empty or price_col not in data.columns:
            return pd.Series([], dtype=float)

        try:
            ma = data[price_col].rolling(window=window, min_periods=1).mean()
            # 验证
            if ma.notnull().any():
                valid = ma[ma.notnull()]
                price = data[price_col][valid.index]
                if valid.min() < price.min() * 0.9 or valid.max() > price.max() * 1.1:
                    logger.warning("MA值超出合理范围")
            return ma
        except Exception:
            return pd.Series([np.nan] * len(data), index=data.index)

    def calculate_weekly_ma(self, stock_code, window, weeks=None):
        """计算周线MA"""
        cache_key = f"weekly_ma_{stock_code}_{window}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            weekly_data = self.hdm.get_weekly_data(stock_code, weeks)
            if weekly_data.empty or len(weekly_data) < window:
                return None

            ma_series = self.calculate_ma(weekly_data, window, "close")
            if ma_series.empty or ma_series.isnull().all():
                return None

            latest_ma = ma_series.iloc[-1]
            latest_close = weekly_data["close"].iloc[-1]
            if abs(latest_ma - latest_close) / latest_close > 0.5:
                logger.warning(f"周线MA异常: {stock_code}")

            self._cache[cache_key] = latest_ma
            return latest_ma
        except Exception as e:
            logger.error(f"周线MA错误: {e}")
            return None

    def calculate_daily_ma(self, stock_code, window, days=None):
        """计算日线MA"""
        cache_key = f"daily_ma_{stock_code}_{window}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=window * 2)).strftime(
                "%Y%m%d"
            )

            daily_data = self.hdm.get_historical_data(stock_code, start_date, end_date)
            if daily_data.empty:
                return None

            ma_series = self.calculate_ma(daily_data, window, "close")
            if ma_series.empty or ma_series.isnull().all():
                return None

            latest_ma = ma_series.iloc[-1]
            if "close" in daily_data.columns:
                latest_close = daily_data["close"].iloc[-1]
                if abs(latest_ma - latest_close) / latest_close > 0.3:
                    logger.warning(f"日线MA异常: {stock_code}")

            self._cache[cache_key] = latest_ma
            return latest_ma
        except Exception as e:
            logger.error(f"日线MA错误: {e}")
            return None

    def get_all_anchors(self, stock_code):
        """计算所有锚点"""
        anchors = {
            "ma60": self.calculate_daily_ma(stock_code, 60),
            "wma20": self.calculate_weekly_ma(stock_code, 20),
            "wma30": self.calculate_weekly_ma(stock_code, 30),
            "wma50": self.calculate_weekly_ma(stock_code, 50),
        }
        valid = sum(1 for v in anchors.values() if v is not None)
        logger.info(f"锚点计算: {stock_code}, 有效{valid}/4")
        return anchors

    def clear_cache(self):
        self._cache.clear()
