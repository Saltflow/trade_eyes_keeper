"""
日报模式邮件测试（EvaluationReport 统一数据源）
"""

import os
import sys
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
    from src.strategy import EvaluationReport
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
            [], pd.DataFrame(),
        )
        assert "策略报警" not in html
        assert "策略信号扫描" not in html




    def test_daily_decision_board_precedes_full_watchlist(self):
        notifier = _make_notifier()
        html = notifier._build_email_body(
            [],
            pd.DataFrame(),
            evaluation_reports={"a_share": _make_report()},
        )

        assert html.index("三市场决策板") < html.index("完整行情矩阵")




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

        def capture_compilation(tex_path):
            # This test checks template data; compiler protocol has its own tests.
            captured["tex"] = tex_path.read_text(encoding="utf-8")
            return b"%PDF-FAKE"

        monkeypatch.setattr(
            "src.notification.daily_pdf._compile_pdf", capture_compilation
        )
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
            [], pd.DataFrame(), evaluation_reports={},
        )
        assert "股票日报" in html or "股票提醒" in html or "<html" in html.lower()

    def test_daily_mode_uses_compact_valuation_matrix(self):
        notifier = _make_notifier()
        stock_data = pd.DataFrame(
            [{
                "stock_code": "601728",
                "stock_name": "中国电信",
                "date": pd.Timestamp.now().strftime("%Y-%m-%d"),
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

        html = notifier._build_email_body([], stock_data)

        assert "完整行情矩阵" in html
        assert "PE" in html
        assert "PB" in html
        assert "隐含 Ke" in html
        assert "1 ÷ PE + 2%" in html
        assert "601728" in html
        assert "6.20" in html
        assert "15.0" in html
        assert "1.10" in html
        assert "8.7%" in html
        assert "锚值" not in html
        assert "基本面" not in html
        assert "技术面" not in html
        assert "MACD柱" not in html
        assert "三市场决策板" in html
        assert "参考持仓" not in html

    def test_daily_email_omits_weekly_nav_and_hides_holdings(self):
        notifier = _make_notifier()
        position = {
            "code": "601728", "shares": 1000, "cost": 10.0,
            "price": 12.0, "value": 12000, "pnl": 2000,
            "pnl_pct": 20.0,
        }
        report = _make_report(
            weekly_nav_ohlc={
                "labels": ["2026-W01", "2026-W02"],
                "open": [100000.0, 100500.0],
                "high": [101000.0, 101500.0],
                "low": [99000.0, 100000.0],
                "close": [100500.0, 101000.0],
            },
            final_holdings=[position],
        )
        html = notifier._build_email_body(
            [], pd.DataFrame(), evaluation_reports={"a_share": report}
        )

        assert "周 NAV 箱线图" not in html
        assert "nav-boxplot" not in html
        assert "2026-W01" not in html
        assert "2026-W01 100500" not in html
        assert "期末持仓" not in html
        assert "1000股" not in html
