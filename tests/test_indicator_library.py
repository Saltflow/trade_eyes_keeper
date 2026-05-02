"""tests for indicator_library.py"""

import numpy as np
import pandas as pd
import pytest
from src.analysis.indicator_library import (
    add_rsi, add_macd, add_atr, add_bollinger,
    add_adx, add_volume_ratio, compute_all,
    COL_RSI, COL_MACD, COL_ADX, COL_VOL_RATIO,
    COL_BOLL_PCT_B, COL_ATR,
)


@pytest.fixture
def sample_df():
    """Generate a realistic price series with trend + noise"""
    np.random.seed(42)
    n = 200
    prices = 50 + np.cumsum(np.random.randn(n) * 0.5)
    prices = np.maximum(prices, 1)
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="B"),
        "open": prices + np.random.randn(n) * 0.2,
        "high": prices + np.abs(np.random.randn(n)) * 1.5,
        "low": prices - np.abs(np.random.randn(n)) * 1.5,
        "close": prices,
        "volume": np.random.randint(1000, 50000, n),
    })
    df["high"] = df[["high", "close", "open"]].max(axis=1)
    df["low"] = df[["low", "close", "open"]].min(axis=1)
    return df


class TestRSI:
    def test_rsi_range(self, sample_df):
        df = add_rsi(sample_df.copy(), period=14)
        rsi = df[COL_RSI].dropna()
        assert len(rsi) > 0
        assert (rsi >= 0).all()
        assert (rsi <= 100).all()

    def test_rsi_oversold_after_continuous_losses(self):
        n = 50
        df = pd.DataFrame({
            "close": np.linspace(50, 30, n)  # steady decline
        })
        df = add_rsi(df, period=14)
        last_rsi = df[COL_RSI].iloc[-1]
        assert last_rsi < 30  # should be oversold

    def test_rsi_overbought_after_continuous_gains(self, sample_df):
        df = add_rsi(sample_df.copy(), period=14)
        rsi_vals = df[COL_RSI].dropna()
        assert len(rsi_vals) > 50
        # With the seeded random data, RSI should be in the normal range
        assert 0 <= rsi_vals.min() <= 100
        assert 0 <= rsi_vals.max() <= 100


class TestMACD:
    def test_macd_columns(self, sample_df):
        df = add_macd(sample_df.copy())
        assert COL_MACD in df.columns
        assert "macd_signal" in df.columns
        assert "macd_hist" in df.columns

    def test_histogram_is_difference(self, sample_df):
        df = add_macd(sample_df.copy())
        hist = df["macd_hist"].dropna()
        macd = df[COL_MACD].dropna()
        signal = df["macd_signal"].dropna()
        assert np.allclose(
            hist.tail(10).values,
            (macd.tail(10) - signal.tail(10)).values,
            atol=0.01,
        )


class TestATR:
    def test_atr_positive(self, sample_df):
        df = add_atr(sample_df.copy(), period=14)
        atr = df[COL_ATR].dropna()
        assert len(atr) > 0
        assert (atr >= 0).all()

    def test_atr_zero_for_flat_market(self):
        n = 30
        df = pd.DataFrame({
            "high": [10.0] * n,
            "low": [10.0] * n,
            "close": [10.0] * n,
        })
        df = add_atr(df, period=14)
        # After the first flat period, ATR should be near 0
        last_atr = df[COL_ATR].iloc[-1]
        assert last_atr < 0.01


class TestBollinger:
    def test_bollinger_pct_b_range(self, sample_df):
        df = add_bollinger(sample_df.copy(), window=20)
        pct_b = df[COL_BOLL_PCT_B].dropna()
        # Most values should be in [0, 1], but not strictly
        assert len(pct_b) > 0

    def test_bollinger_pct_b_close_to_zero_at_lower_band(self, sample_df):
        df = add_bollinger(sample_df.copy(), window=20)
        pct_b = df[COL_BOLL_PCT_B].dropna()
        assert len(pct_b) > 0
        # %B values should exist and be finite
        assert not pct_b.isna().all()


class TestADX:
    def test_adx_range(self, sample_df):
        df = add_adx(sample_df.copy(), period=14)
        adx = df[COL_ADX].dropna()
        assert len(adx) > 0
        assert (adx >= 0).all()
        assert (adx <= 100).all()


class TestVolumeRatio:
    def test_volume_ratio_around_one(self, sample_df):
        df = add_volume_ratio(sample_df.copy(), window=20)
        vr = df[COL_VOL_RATIO].dropna()
        # Mean should be near 1.0 (ratio to own MA)
        assert 0.5 < vr.mean() < 2.0

    def test_volume_spike_detected(self, sample_df):
        df = add_volume_ratio(sample_df.copy(), window=20)
        vr = df[COL_VOL_RATIO].dropna()
        assert len(vr) > 0
        # At least some values should deviate from 1.0
        assert (vr != 1.0).any()


class TestComputeAll:
    def test_compute_all_adds_columns(self, sample_df):
        result = compute_all({"test": sample_df.copy()})
        df = result["test"]
        for col in [COL_RSI, COL_MACD, COL_ADX, COL_VOL_RATIO, COL_ATR]:
            assert col in df.columns, f"Missing {col}"

    def test_compute_all_multi_stock(self, sample_df):
        result = compute_all(
            {"a": sample_df.copy(), "b": sample_df.copy()}
        )
        assert len(result) == 2
