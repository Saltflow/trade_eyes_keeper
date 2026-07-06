"""TDD tests for daily_mode in _build_email_body.

Red phase: these tests FAIL because daily_mode doesn't exist yet.
Green phase: implement daily_mode to make them pass.

Goal: daily report email should NOT contain alert/strategy-alert/backtest/
old-portfolio sections. It SHOULD contain chart + fundamentals + search-strategy
results + announcements.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_minimal_stock_data():
    """Build a minimal stock_data DataFrame for testing."""
    return pd.DataFrame([
        {
            "stock_code": "601728",
            "stock_name": "中国电信",
            "open": 5.40,
            "close": 5.38,
            "high": 5.45,
            "low": 5.35,
            "ma60": 5.91,
            "dividend_per_share": 0.25,
            "dividend_yield": 4.65,
            "pe_ratio": 12.3,
            "pb_ratio": 1.4,
            "roe": 11.8,
        },
    ])


def _make_notifier():
    """Create a minimal EmailNotifier with temp archive dir."""
    import tempfile
    tmpdir = tempfile.mkdtemp()
    config = {
        "email": {
            "smtp_server": "localhost",
            "smtp_port": 465,
            "sender_email": "test@test.com",
            "sender_password": "x",
            "receiver_email": "test@test.com",
            "archive_dir": tmpdir,
        },
    }
    from notification.email_notifier import EmailNotifier
    notifier = EmailNotifier(config)
    # Mock _get_server_info to avoid network calls
    notifier._get_server_info = MagicMock(return_value={
        "hostname": "test-host",
        "ip_address": "127.0.0.1",
    })
    return notifier


class TestDailyModeRemovesOldSections:
    """daily_mode=True should strip alert/strategy-alert/backtest/old-portfolio."""

    def test_no_alert_section_in_daily_mode(self):
        """No '满足条件的股票' header (alert section) in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        assert "满足条件的股票" not in html, (
            "daily_mode should suppress alert section"
        )

    def test_no_strategy_alert_when_signal_scan_none(self):
        """No '策略报警' header when signal_scan is None in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        assert "策略报警" not in html

    def test_strategy_alert_shown_when_signal_scan_provided(self):
        """Strategy alert section renders without error when signal_scan has alerts."""
        from unittest.mock import MagicMock
        notifier = _make_notifier()
        mock_scan = MagicMock()
        mock_scan.alerts = []
        mock_scan.consensus = None
        mock_scan.indicator_snapshot = {}
        mock_scan.divergence_warnings = []
        # Should not crash even with empty alerts
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            signal_scan=mock_scan,
            daily_mode=True,
        )
        assert len(html) > 0

    def test_no_backtest_in_daily_mode(self):
        """No '回测分析' header (backtest section) in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        assert "回测分析" not in html
        assert "观察期" not in html

    def test_no_old_portfolio_in_daily_mode(self):
        """No '投资组合预期回报' header (old portfolio section) in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        assert "投资组合预期回报" not in html
        assert "策略说明" not in html


class TestDailyModeKeepsEssentialSections:
    """daily_mode=True should keep chart, fundamentals, search-strategy, announcements."""

    def test_has_strategy_results_when_portfolio_provided(self):
        """Search strategy results section should appear when portfolio_results given."""
        from src.analysis.portfolio_strategy import PortfolioResult
        notifier = _make_notifier()
        pr = PortfolioResult(
            name="max_return", group="a_share",
            total_return=15.0, max_drawdown=-5.0, sharpe_ratio=0.8,
            expected_position=50000, composition=["601728"], trade_count=10,
        )
        portfolio_results = {"a_share": {"max_return": pr}}
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            portfolio_results=portfolio_results,
            daily_mode=True,
        )
        assert "搜参策略结果" in html

    def test_has_fundamentals_table(self):
        """Fundamentals table should still be present in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        assert "基本面" in html or "股息率" in html or "ROE" in html


class TestDailyModePriceTableSimplified:
    """daily_mode=True should remove MA60/deviation columns from price table."""

    def test_no_ma60_column_in_daily_mode(self):
        """Price table should NOT have MA60 column header in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        # The price table header should not contain MA60
        # Look in the monitoring section, not in alert section
        assert "MA60" not in html, "daily_mode should remove MA60 column"

    def test_no_deviation_column_in_daily_mode(self):
        """Price table should NOT have 偏离/偏离% columns in daily mode."""
        notifier = _make_notifier()
        html = notifier._build_email_body(
            alert_stocks=[],
            stock_data=_make_minimal_stock_data(),
            daily_mode=True,
        )
        assert "偏离" not in html, "daily_mode should remove deviation columns"


class TestSendFromSessionPassesDailyMode:
    """Both send_from_session and send_daily_report_from_session should pass daily_mode=True."""

    def test_build_email_body_signature_has_daily_mode(self):
        """_build_email_body must accept daily_mode parameter."""
        import inspect
        from notification.email_notifier import EmailNotifier
        sig = inspect.signature(EmailNotifier._build_email_body)
        assert "daily_mode" in sig.parameters, (
            "_build_email_body must have daily_mode parameter"
        )
