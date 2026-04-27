"""
指标库

提供趋势、动量、波动率、成交量四大类指标的纯 pandas 计算。
所有函数都对 DataFrame 就地添加列并返回。

指标列表:
  - RSI        (Wilder 平滑, 默认周期 14)
  - MACD       (EMA 快/慢/信号, 默认 12/26/9)
  - ATR        (Wilder 平滑真实波幅, 默认周期 14)
  - Bollinger  (%B / 带宽, 默认窗口 20, std 倍率 2)
  - ADX        (Wilder DMI, 默认周期 14)
  - Volume Ratio (成交量 / 成交量 SMA)
"""

import logging

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# 列名常量，避免拼写错误
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

    # True Range
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Directional Movement
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder smooth
    atr_sm = _wilder_smooth(tr, period)
    plus_di = 100.0 * _wilder_smooth(pd.Series(plus_dm), period) / atr_sm.replace(0, np.nan)
    minus_di = 100.0 * _wilder_smooth(pd.Series(minus_dm), period) / atr_sm.replace(0, np.nan)

    # DX and ADX
    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    df[COL_ADX] = _wilder_smooth(pd.Series(dx), period)
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


# ── 批量计算 ──


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
    """
    一次性计算所有指标，返回 {code: DataFrame_with_indicators}

    Args:
        stocks_data: {stock_code: DataFrame}，每只股票至少含 date/open/high/low/close/volume
        其余参数同各 add_* 函数

    Returns:
        含有新增列的 DataFrame 字典
    """
    result: dict[str, pd.DataFrame] = {}
    for code, df in stocks_data.items():
        df = df.copy()
        # 确保列名小写
        df.columns = [c.lower() for c in df.columns]
        # 确保必要列存在
        for col in ("close", "high", "low", "volume"):
            if col not in df.columns:
                logger.warning("股票 %s 缺少列 '%s'，跳过指标计算", code, col)
                result[code] = df
                continue

        # 按日期排序
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
        except Exception as e:
            logger.warning("股票 %s 指标计算失败: %s", code, e)

        result[code] = df

    return result
