"""
日报模式邮件测试（EvaluationReport 统一数据源）
"""

import sys
import os
import unittest.mock
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_notifier():
    from src.notification.email_notifier import EmailNotifier
    return EmailNotifier({
        "stocks": [],
        "email": {
            "sender_email": "t@t.com",
            "sender_password": "x",
            "receiver_email": "t@t.com",
        },
    })


def _make_report(group="a_share", total_return=15.0, excess_return=10.0,
                 max_drawdown=-5.0, sharpe_ratio=1.2, composition=None,
                 quarterly_holdings=None, benchmark_returns=None,
                 benchmark_win_rates=None):
    from src.analysis.search_interface import EvaluationReport
    return EvaluationReport(
        group=group, engine_name="percentile", strategy_label="分位评分",
        timestamp="2026-01-01T00:00:00",
        total_return=total_return, excess_return=excess_return,
        max_drawdown=max_drawdown, sharpe_ratio=sharpe_ratio,
        trade_count=8, avg_cash_pct=30.0,
        benchmark_returns=benchmark_returns or {"risk_free": 1.0, "510300": 5.0},
        benchmark_win_rates=benchmark_win_rates or {"risk_free": 70.0, "510300": 55.0},
        composition=composition or ["601088"],
        quarterly_holdings=quarterly_holdings or [],
    )


class TestDailyModeEmail:
    def test_daily_mode_hides_alert_section(self):
        notifier = _make_notifier()
        html = notifier._build_email_body(
            [], pd.DataFrame(), daily_mode=True,
        )
        assert "策略报警" not in html
        assert "策略信号扫描" not in html

    def test_today_signals_in_strategy_results(self):
        """信号展示在策略结果段内。"""
        from unittest.mock import MagicMock
        notifier = _make_notifier()

        mock_alert = MagicMock()
        mock_alert.stock_code = "601728"
        mock_alert.rule_label = "buy_1"
        mock_alert.current_value = "-9.1%"
        mock_scan = MagicMock()
        mock_scan.alerts = [mock_alert]
        mock_scan.consensus = None
        mock_scan.indicator_snapshot = {}
        mock_scan.divergence_warnings = []

        r = _make_report()
        reports = {"a_share": r}

        section = notifier._build_strategy_results_section(
            reports, signal_scan=mock_scan,
        )
        assert "601728" in section
        assert "今日信号" in section

    def test_no_signal_crash(self):
        notifier = _make_notifier()
        r = _make_report()
        reports = {"a_share": r}
        section = notifier._build_strategy_results_section(reports)
        assert "无触发" in section

    def test_strategy_results_header(self):
        notifier = _make_notifier()
        r = _make_report()
        reports = {"a_share": r}
        section = notifier._build_strategy_results_section(reports)
        assert "搜参策略结果" in section
        assert "分位评分" in section

    def test_daily_strategy_section_keeps_ranking_selection_diagnostics(self):
        notifier = _make_notifier()
        report = _make_report()
        report.selection_diagnostics = {
            "wf_score": 3.5,
            "ranking_diagnostics": {
                "weighted_strategy_return": 4.2,
                "positive_return_windows": 10,
                "ranking_window_count": 13,
            },
            "sensitivity": {
                "worst_score": -1.0,
                "drop": 4.5,
                "selection_score": -1.0,
            },
            "selection_score": -1.0,
        }

        section = notifier._build_strategy_results_section({"a_share": report})

        assert "排名筛选" in section
        assert "加权绝对收益 +4.20%" in section
        assert "正收益窗口 10/13" in section
        assert "最终选择分 -1.000" in section

    def test_quarterly_holdings_rendered(self):
        notifier = _make_notifier()
        qh = [{
            "quarter": 1, "cash": 30000, "pos_pct": 70,
            "nav": 100000,
            "positions": [{
                "code": "601728", "shares": 1000, "cost": 10.0,
                "price": 12.0, "value": 12000, "pnl": 2000, "pnl_pct": 20.0,
            }],
        }]
        r = _make_report(quarterly_holdings=qh)
        reports = {"a_share": r}
        section = notifier._build_strategy_results_section(reports)
        assert "601728" in section
        assert "1000股" in section

    def test_empty_body_no_crash(self):
        notifier = _make_notifier()
        html = notifier._build_email_body(
            [], pd.DataFrame(), daily_mode=True, evaluation_reports={},
        )
        assert "股票日报" in html or "股票提醒" in html or "<html" in html.lower()

    def test_daily_mode_keeps_price_anchor_fundamental_and_technical_data(self):
        notifier = _make_notifier()
        stock_data = pd.DataFrame(
            [{
                "stock_code": "601728",
                "stock_name": "中国电信",
                "open": 6.1,
                "close": 6.2,
                "high": 6.3,
                "low": 6.0,
                "ma60": 6.0,
                "wma20": 6.1,
                "dividend_per_share": 0.2,
                "dividend_yield": 3.2,
                "pe_ratio": 15.0,
                "pb_ratio": 1.1,
                "roe": 9.8,
                "rsi": 55.0,
                "macd_hist": 0.03,
                "vol_ratio": 1.2,
                "adx": 22.0,
                "boll_pct_b": 0.6,
            }]
        )

        html = notifier._build_email_body([], stock_data, daily_mode=True)

        assert "价格 / 锚点" in html
        assert "锚值" in html
        assert "基本面" in html
        assert "技术面" in html
        assert "MACD柱" in html
        assert "601728" in html
