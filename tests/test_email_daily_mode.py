"""
日报模式邮件测试（EvaluationReport 统一数据源）
"""

import sys
import os
import unittest.mock
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
                 benchmark_win_rates=None, weekly_nav_ohlc=None,
                 final_holdings=None):
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
        weekly_nav_ohlc=weekly_nav_ohlc or {},
        final_asset=108000.0,
        final_cash=68000.0,
        final_holdings_value=40000.0,
        final_position_pct=37.04,
        final_holdings=final_holdings or [],
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

    def test_monitoring_targets_precede_backtest_results(self):
        notifier = _make_notifier()
        html = notifier._build_email_body(
            [],
            pd.DataFrame(),
            daily_mode=True,
            evaluation_reports={"a_share": _make_report()},
        )

        assert html.index("监控标的") < html.index("搜参策略结果")

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

    def test_unified_weekly_quarterly_and_final_report_rendered(self, monkeypatch):
        notifier = _make_notifier()
        monkeypatch.setattr(
            "src.notification.chart_generator.generate_candlestick_chart",
            lambda weekly: ("data:image/png;base64,AAAA", b"fake"),
        )
        position = {
            "code": "601728", "shares": 1000, "cost": 10.0,
            "price": 12.0, "value": 12000, "weight": 11.11,
            "pnl": 2000, "pnl_pct": 20.0,
        }
        report = _make_report(
            weekly_nav_ohlc={
                "labels": ["2026-W01", "2026-W02", "2026-W03"],
                "open": [100000.0, 100500.0, 101000.0],
                "high": [101000.0, 101500.0, 102000.0],
                "low": [99000.0, 100000.0, 100500.0],
                "close": [100500.0, 101000.0, 101500.0],
            },
            quarterly_holdings=[{
                "quarter": "2026Q1", "date": "2026-03-31",
                "cash": 88000, "pos_pct": 12.0, "nav": 100000,
                "positions": [position],
            }],
            final_holdings=[position],
        )

        section = notifier._build_strategy_results_section({"a_share": report})

        assert "周 NAV K线" in section
        assert 'src="data:image/png;base64,AAAA"' in section
        assert "<th>自然周</th>" not in section
        assert "季末持仓（自然季度最后有效交易日）" in section
        assert "2026-03-31" in section
        assert "期末持仓" in section
        assert "期末资产" in section
        assert "11.1%" in section

    def test_pdf_template_receives_unified_evaluation_appendix(self, monkeypatch):
        notifier = _make_notifier()
        position = {
            "code": "601728", "shares": 1000, "cost": 10.0,
            "price": 12.0, "value": 12000, "weight": 11.11,
            "pnl": 2000, "pnl_pct": 20.0,
        }
        report = _make_report(
            weekly_nav_ohlc={
                "labels": ["2026-W01"], "open": [100000.0],
                "high": [101000.0], "low": [99000.0], "close": [100500.0],
            },
            quarterly_holdings=[{
                "quarter": "2026Q1", "date": "2026-03-31",
                "cash": 88000, "pos_pct": 12.0, "nav": 100000,
                "positions": [position],
            }],
            final_holdings=[position],
        )
        session = SimpleNamespace(evaluation_reports={"a_share": report})
        captured = {}

        monkeypatch.setattr(notifier, "_chart_deviation_timeline", lambda *a, **k: None)

        def fake_run(args, **kwargs):
            tex_path = Path(args[-1])
            captured["tex"] = tex_path.read_text(encoding="utf-8")
            (tex_path.parent / "report.pdf").write_bytes(b"%PDF-FAKE")
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        pdf = notifier._generate_daily_pdf(session, [], None, {}, pd.DataFrame())

        assert pdf == b"%PDF-FAKE"
        assert "统一策略评估" in captured["tex"]
        assert "周 NAV K线（自然周 OHLC）" in captured["tex"]
        assert "季末持仓（自然季度最后有效交易日）" in captured["tex"]
        assert "期末持仓" in captured["tex"]
        assert "2026-W01" in captured["tex"]

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
