from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

import main
from src.core.data_fetcher import StockDataFetcher


def test_daily_portfolio_history_is_exactly_36_calendar_months_with_buffer():
    end_date = pd.Timestamp("2026-09-18")

    assert main._daily_portfolio_evaluation_start(end_date) == pd.Timestamp(
        "2023-09-18"
    )
    assert main._daily_portfolio_history_days() > (
        36 * 30.4375
    )


def test_daily_fetcher_honors_explicit_long_history_window():
    fetcher = StockDataFetcher(
        {"stocks": ["000001"], "data_source": {"type": "web_crawler"}}
    )
    fetcher._data_source = MagicMock()
    fetcher._data_source.fetch_stock_data.return_value = pd.DataFrame(
        {
            "date": pd.to_datetime(["2023-09-18", "2026-09-18"]),
            "open": [10.0, 10.0],
            "close": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "volume": [1_000, 1_000],
            "amount": [10_000, 10_000],
        }
    )
    fetcher.technical_indicators.calculate_indicators = lambda df, **_: df
    fetcher._add_extended_indicators = lambda df: df
    fetcher._fetch_fundamental_data = lambda _: {
        "dividend_per_share": None,
        "pe_ratio": None,
        "pb_ratio": None,
        "roe": None,
    }
    fetcher._save_to_csv = MagicMock()
    session = SimpleNamespace(_historical={}, errors=[], stocks_data={})
    session_manager = MagicMock()
    session_manager.update_stock_from_dataframe.return_value = True

    fetcher.fetch_to_session(
        session,
        session_manager,
        history_days=main._daily_portfolio_history_days(),
    )

    fetcher._data_source.fetch_stock_data.assert_called_once_with(
        "000001", days=main._daily_portfolio_history_days()
    )
    assert set(session._historical) == {"000001"}
