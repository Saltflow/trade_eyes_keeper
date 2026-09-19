import numpy as np
import pandas as pd

from src.core.data_fetcher import StockDataFetcher


def test_stock_data_fetcher_adds_extended_indicators():
    periods = 40
    close = np.linspace(10.0, 20.0, periods)
    stock_data = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=periods, freq="B"),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.linspace(1000.0, 2000.0, periods),
        },
        index=pd.RangeIndex(100, 100 + periods),
    )

    result = StockDataFetcher._add_extended_indicators(stock_data)

    assert {"rsi", "macd", "adx", "boll_pct_b", "vol_ratio"}.issubset(
        result.columns
    )
    assert result["vol_ratio"].notna().all()
