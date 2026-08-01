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
            # 默认配置路径：项目根 config/alerts.yaml
            # __file__ = src/data/technical_indicators.py
            # 往上三层到项目根: src/data → src → 项目根
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            config_path = os.path.join(project_root, "config", "alerts.yaml")

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

    def _calculate_weekly_ma(
        self,
        data: pd.DataFrame,
        window: int,
        price_col: str = "close",
    ) -> pd.Series:
        """
        计算周线移动平均

        步骤：
        1. 按周重采样，取每周最后收盘价
        2. 在周线上算简单MA
        3. 前向填充回日线

        Args:
            data: 日线DataFrame，必须含 date 和 price_col 列
            window: 周窗口数
            price_col: 价格列名

        Returns:
            pd.Series: 与原始日线长度一致的周MA值
        """
        if data.empty or "date" not in data.columns or price_col not in data.columns:
            logger.warning(f"数据不足以计算周MA{window}")
            return pd.Series([np.nan] * len(data), index=data.index)

        try:
            df = data.copy()
            # 记录原始行号索引（RangeIndex 0,1,2...），最后恢复用
            original_index = data.index
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()

            # 按周重采样，取每周最后一天收盘价
            weekly = df[price_col].resample("W-FRI").last()

            # 计算周线MA（min_periods=1 确保数据尚不足时也出值）
            weekly_ma = weekly.rolling(window=window, min_periods=1).mean()

            # 前向填充回日线：将周MA值填充到对应周的每一天
            daily_ma = weekly_ma.reindex(df.index, method="ffill")

            # 按日期对齐回原始行号索引：daily_ma 是 DatetimeIndex，
            # 用 data["date"] 的日期做映射，恢复成 RangeIndex
            result = daily_ma.reindex(pd.to_datetime(data["date"]))
            result.index = original_index
            return result
        except Exception as e:
            logger.warning(f"计算周MA{window}失败: {e}")
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
                    # 周线移动平均：日线重采样到周频 → 算MA → 前向填充回日线
                    weekly = self._calculate_weekly_ma(
                        result_data, window=window, price_col="close"
                    )
                    result_data[name] = weekly
                    logger.debug(
                        f"计算指标 {name} (周MA{window}) 完成"
                        + (f" for {stock_code}" if stock_code else "")
                    )
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


# ════════════════════════════════════════════════════════════
# 独立指标计算函数（纯 pandas，无类依赖）
# 从已删除的旧指标库迁移而来
# ════════════════════════════════════════════════════════════

COL_RSI = "rsi"
COL_MACD = "macd"
COL_MACD_SIGNAL = "macd_signal"
COL_MACD_HIST = "macd_hist"
COL_ATR = "atr"
COL_BOLL_MA = "boll_ma"
COL_BOLL_UPPER = "boll_upper"
COL_BOLL_LOWER = "boll_lower"
COL_BOLL_PCT_B = "boll_pct_b"
COL_ADX = "adx"
COL_VOL_RATIO = "vol_ratio"


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑 (EMA with alpha = 1/period)"""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 RSI (Wilder 平滑)"""
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df[COL_RSI] = 100.0 - (100.0 / (1.0 + rs))
    return df


def add_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """计算 MACD"""
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    df[COL_MACD] = macd_line
    df[COL_MACD_SIGNAL] = signal_line
    df[COL_MACD_HIST] = macd_line - signal_line
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 ATR (Wilder 平滑)"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df[COL_ATR] = _wilder_smooth(tr, period)
    return df


def add_bollinger(
    df: pd.DataFrame,
    window: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """计算布林带 (%B)"""
    close = df["close"]
    ma = close.rolling(window=window, min_periods=1).mean()
    std = close.rolling(window=window, min_periods=1).std()
    upper = ma + num_std * std
    lower = ma - num_std * std
    df[COL_BOLL_MA] = ma
    df[COL_BOLL_UPPER] = upper
    df[COL_BOLL_LOWER] = lower
    bandwidth = upper - lower
    df[COL_BOLL_PCT_B] = np.where(
        bandwidth > 0,
        (close - lower) / bandwidth,
        0.5,
    )
    return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """计算 ADX (Wilder DMI)"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr_sm = _wilder_smooth(tr, period)
    plus_di = (
        100.0 * _wilder_smooth(pd.Series(plus_dm), period) / atr_sm.replace(0, np.nan)
    )
    minus_di = (
        100.0 * _wilder_smooth(pd.Series(minus_dm), period) / atr_sm.replace(0, np.nan)
    )

    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    df["plus_di"] = np.asarray(plus_di, dtype=float)
    df["minus_di"] = np.asarray(minus_di, dtype=float)
    df[COL_ADX] = np.asarray(
        _wilder_smooth(pd.Series(dx), period), dtype=float
    )
    return df


def add_volume_ratio(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """计算量比 = 成交量 / 成交量 SMA(window)"""
    vol_ma = df["volume"].rolling(window=window, min_periods=1).mean()
    df[COL_VOL_RATIO] = np.where(
        vol_ma > 0,
        df["volume"] / vol_ma,
        1.0,
    )
    return df


def _add_ma60_and_deviation(df: pd.DataFrame) -> None:
    """补均线、偏离和因果 MA200 斜率。"""
    ma60 = df["close"].rolling(60, min_periods=1).mean()
    df["ma60"] = ma60
    df["deviation"] = (df["close"] - ma60) / ma60.replace(0, float("nan"))
    ma200 = df["close"].rolling(200, min_periods=1).mean()
    df["ma200"] = ma200
    df["ma200_dev"] = (df["close"] - ma200) / ma200.replace(0, float("nan"))
    df["ma200_slope"] = ma200 / ma200.shift(20).replace(0, float("nan")) - 1.0


def _add_rolling_percentile_ranks(df: pd.DataFrame, window: int = 252) -> None:
    """计算供所有策略复用的因果滚动分位值（0.0～1.0）。"""
    for src_col, out_col in [
        ("adx", "adx_pct"), ("rsi", "rsi_pct"),
        ("deviation", "deviation_pct"), ("vol_ratio", "vol_ratio_pct"),
        ("ma200_dev", "ma200_dev_pct"),
    ]:
        if src_col not in df.columns:
            continue
        arr = df[src_col].values.astype(float)
        result = np.full(len(arr), np.nan)
        for t in range(window - 1, len(arr)):
            lo = t - window + 1
            win = arr[lo:t + 1]
            win = win[~np.isnan(win)]
            if len(win) >= 20:
                result[t] = (win <= arr[t]).sum() / len(win)
        df[out_col] = result


def compute_all(
    stocks_data: dict[str, pd.DataFrame],
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    atr_period: int = 14,
    boll_window: int = 20,
    boll_std: float = 2.0,
    adx_period: int = 14,
    vol_window: int = 20,
) -> dict[str, pd.DataFrame]:
    """一次性计算所有指标，返回 {code: DataFrame_with_indicators}"""
    result: dict[str, pd.DataFrame] = {}
    for code, df in stocks_data.items():
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        for col in ("close", "high", "low", "volume"):
            if col not in df.columns:
                logger.warning("股票 %s 缺少列 '%s'，跳过指标计算", code, col)
                result[code] = df
                continue
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
        try:
            add_rsi(df, period=rsi_period)
            add_macd(df, fast=macd_fast, slow=macd_slow, signal=macd_signal)
            add_atr(df, period=atr_period)
            add_bollinger(df, window=boll_window, num_std=boll_std)
            add_adx(df, period=adx_period)
            add_volume_ratio(df, window=vol_window)
            # ── INDICATOR_NAMES 补齐列（百分位策略依赖）──
            _add_ma60_and_deviation(df)
            _add_rolling_percentile_ranks(df, window=252)
        except Exception as e:
            logger.warning("股票 %s 指标计算失败: %s", code, e)
        result[code] = df
    return result
