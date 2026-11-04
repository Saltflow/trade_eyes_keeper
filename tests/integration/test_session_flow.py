"""Offline stock-source, model-conversion and session integration contracts."""

from unittest.mock import Mock

import pandas as pd

from src.core.data_fetcher import StockDataFetcher
from src.models.converters import (
    dataframe_to_stock_price_data,
    stock_price_data_list_to_dataframe,
)
from src.models.schemas import SessionContext
from src.session.session_manager import SessionManager


def test_session_manager_creation():
    manager = SessionManager({"stocks": []})
    assert manager.global_config == {"stocks": []}


def test_session_creation_has_only_current_report_fields():
    manager = SessionManager({"stocks": []})
    session = manager.create_session()
    assert isinstance(session, SessionContext)
    assert manager.get_session(session.session_id) is session
    assert session.stocks_data == {}
    assert {"alerts", "signal_scan", "portfolio_results", "backtest",
            "opt_data_a", "opt_data_non_a", "opt_data_hk", "opt_data_us"}.isdisjoint(
        session.__fields__
    )


def stock_frame():
    return pd.DataFrame([{
        "date": pd.Timestamp("2026-11-04"),
        "open": 10.0, "close": 10.5, "high": 11.0, "low": 9.8,
        "volume": 1000000, "amount": 10000000, "ma60": 10.2,
        "custom_observed_indicator": 42.25,
    }])


def test_fetch_to_session_uses_source_data_and_writes_only_temp_history(tmp_path):
    config = {"stocks": ["000001"], "storage": {"data_dir": str(tmp_path)}}
    manager = SessionManager(config)
    session = manager.create_session()
    fetcher = StockDataFetcher(config)
    fetcher._data_source = Mock()
    fetcher._data_source.fetch_stock_data.return_value = stock_frame()
    fetcher._fetch_fundamental_data = Mock(return_value={
        "dividend_per_share": 0.21, "pe_ratio": 10.0, "pb_ratio": 1.2, "roe": 12.0,
    })
    fetcher.technical_indicators.calculate_indicators = Mock(
        side_effect=lambda frame, **_: frame
    )
    fetcher.fetch_to_session(session, manager)
    fetcher._data_source.fetch_stock_data.assert_called_once_with("000001", days=730)
    assert set(session.stocks_data) == {"000001"}
    assert session.stocks_data["000001"].close == 10.5
    assert session.stocks_data["000001"].dividend_yield == 2.0
    assert session.stocks_data["000001"].roe == 12.0
    assert session.errors == []
    assert (tmp_path / "000001_history.csv").is_file()
    assert session._historical["000001"].iloc[-1]["close"] == 10.5


def test_stock_model_roundtrip_keeps_dynamic_indicator():
    stock = dataframe_to_stock_price_data(stock_frame(), "000001")
    assert stock is not None
    frame = stock_price_data_list_to_dataframe([stock])
    assert frame.iloc[0]["custom_observed_indicator"] == 42.25
    session = SessionContext(session_id="roundtrip", stocks_data={"000001": stock})
    assert session.get_all_dataframe().iloc[0]["custom_observed_indicator"] == 42.25
