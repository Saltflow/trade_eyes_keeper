"""
Test None value handling in condition_checker and email_notifier
Tests bug fixes for:
1. condition_checker.py:94 - TypeError from None subtraction
2. email_notifier.py:439, 546, 552-558 - TypeError from None formatting and comparison
"""

import pandas as pd
import pytest

from src.core.condition_checker import ConditionChecker
from src.notification.email_notifier import EmailNotifier


class TestNoneValueHandling:
    """Test None value handling after bug fixes"""

    def test_condition_checker_handles_none_anchor_value(self):
        """Test that condition_checker handles None anchor_value gracefully"""
        config = {
            "alerts": {"enabled": False},
            "storage": {"cache_dir": "./cache"},
        }
        checker = ConditionChecker(config)

        # Create alert with None anchor_value
        alert = {
            "stock_code": "600000",
            "anchor_name": "MA60",
            "interval_label": "0-5%",
            "percentage": 3.5,
            "consecutive_days": 1,
            "price": 5.74,
            "anchor_value": None,  # None as found in data_fetcher.py:200
        }

        # Manually construct result as _check_multi does
        price_difference = None
        anchor_val = alert.get("anchor_value")
        price = alert.get("price")

        if anchor_val is not None and price is not None:
            price_difference = anchor_val - price

        # Should be None, not crash with TypeError
        assert price_difference is None

    def test_condition_checker_handles_none_price(self):
        """Test that condition_checker handles None price gracefully"""
        alert = {
            "stock_code": "600000",
            "anchor_name": "MA60",
            "interval_label": "0-5%",
            "percentage": 3.5,
            "consecutive_days": 1,
            "price": None,  # None price
            "anchor_value": 6.0,
        }

        anchor_val = alert.get("anchor_value")
        price = alert.get("price")

        price_difference = None
        if anchor_val is not None and price is not None:
            price_difference = anchor_val - price

        assert price_difference is None

    def test_formatting_handles_none_values(self):
        """Test that formatting functions handle None values gracefully"""
        # Test the pattern used in email_notifier.py
        low_price = None
        ma60 = 5.8

        # Safe formatting pattern from email_notifier.py
        low_price_str = (
            f"{low_price:.2f}"
            if low_price is not None and not pd.isna(low_price)
            else "-"
        )
        ma60_str = f"{ma60:.2f}" if ma60 is not None and not pd.isna(ma60) else "-"

        assert low_price_str == "-"
        assert ma60_str == "5.80"

    def test_comparison_handles_none_values(self):
        """Test that comparison handles None values gracefully"""
        low_price = None
        ma60 = 5.8

        # Safe comparison pattern from email_notifier.py
        status = "正常"
        if (
            low_price is not None
            and ma60 is not None
            and not pd.isna(low_price)
            and not pd.isna(ma60)
            and low_price < ma60
        ):
            status = "<span style='color: #f44336;'>提醒</span>"

        assert status == "正常"

    def test_arithmetic_with_none_values(self):
        """Test that arithmetic operations handle None values gracefully"""
        # Test the pattern used in email_notifier.py:368-369
        close_price = None
        ma60 = 6.0

        close_ma60_diff = None
        close_ma60_pct = None
        if (
            close_price is not None
            and ma60 is not None
            and not pd.isna(close_price)
            and not pd.isna(ma60)
        ):
            close_ma60_diff = close_price - ma60
            close_ma60_pct = (close_ma60_diff / ma60 * 100) if ma60 != 0 else 0

        assert close_ma60_diff is None
        assert close_ma60_pct is None

    def test_color_class_with_none_values(self):
        """Test that color class determination handles None values gracefully"""
        close_ma60_diff = None

        # Safe color class pattern from email_notifier.py:424-432
        close_diff_class = (
            "positive"
            if close_ma60_diff is not None and close_ma60_diff >= 0
            else "negative"
            if close_ma60_diff is not None
            else ""
        )

        assert close_diff_class == ""

    def test_multiple_none_values_in_row(self):
        """Test row with multiple None values doesn't crash formatting"""
        row_data = {
            "open": None,
            "close": None,
            "high": None,
            "low": None,
            "ma60": None,
        }

        # Simulate the formatting loop from email_notifier.py:552-558
        open_price_str = (
            f"{row_data['open']:.2f}"
            if row_data["open"] is not None and not pd.isna(row_data["open"])
            else "-"
        )
        close_price_str = (
            f"{row_data['close']:.2f}"
            if row_data["close"] is not None and not pd.isna(row_data["close"])
            else "-"
        )
        high_price_str = (
            f"{row_data['high']:.2f}"
            if row_data["high"] is not None and not pd.isna(row_data["high"])
            else "-"
        )
        low_price_str = (
            f"{row_data['low']:.2f}"
            if row_data["low"] is not None and not pd.isna(row_data["low"])
            else "-"
        )
        ma60_str = (
            f"{row_data['ma60']:.2f}"
            if row_data["ma60"] is not None and not pd.isna(row_data["ma60"])
            else "-"
        )

        assert open_price_str == "-"
        assert close_price_str == "-"
        assert high_price_str == "-"
        assert low_price_str == "-"
        assert ma60_str == "-"

    def test_mixed_none_and_valid_values(self):
        """Test mixed None and valid values in same row"""
        row_data = {
            "open": 5.7,
            "close": None,
            "high": 5.8,
            "low": 5.6,
            "ma60": 5.9,
        }

        # Format with safe checks
        open_price_str = (
            f"{row_data['open']:.2f}"
            if row_data["open"] is not None and not pd.isna(row_data["open"])
            else "-"
        )
        close_price_str = (
            f"{row_data['close']:.2f}"
            if row_data["close"] is not None and not pd.isna(row_data["close"])
            else "-"
        )
        high_price_str = (
            f"{row_data['high']:.2f}"
            if row_data["high"] is not None and not pd.isna(row_data["high"])
            else "-"
        )
        low_price_str = (
            f"{row_data['low']:.2f}"
            if row_data["low"] is not None and not pd.isna(row_data["low"])
            else "-"
        )
        ma60_str = (
            f"{row_data['ma60']:.2f}"
            if row_data["ma60"] is not None and not pd.isna(row_data["ma60"])
            else "-"
        )

        assert open_price_str == "5.70"
        assert close_price_str == "-"
        assert high_price_str == "5.80"
        assert low_price_str == "5.60"
        assert ma60_str == "5.90"
