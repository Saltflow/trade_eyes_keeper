"""三端信息量对齐测试：build_strategy_text_summary 共享摘要。

验证 Telegram/飞书日报能拿到与邮件一致的信息：
搜参3组 + 验证期胜率 + 平均现金仓位 + 今日信号(可读名) + 未解禁定增。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_report(group="a_share", total_return=10.0, excess_return=8.0,
                 max_drawdown=-5.0, sharpe_ratio=1.0, composition=None,
                 quarterly_holdings=None, benchmark_returns=None,
                 nav_series=None, nav_dates=None, engine_name="percentile",
                 strategy_label="分位评分"):
    from analysis.search_interface import EvaluationReport

    return EvaluationReport(
        group=group,
        engine_name=engine_name,
        strategy_label=strategy_label,
        timestamp="2026-07-09T02:00:00",
        total_return=total_return,
        excess_return=excess_return,
        max_drawdown=max_drawdown,
        sharpe_ratio=sharpe_ratio,
        trade_count=8,
        avg_cash_pct=30.0,
        benchmark_returns=benchmark_returns or {
            "risk_free": 1.0,
            "510300": 5.0,
            "510880": 3.0,
        },
        composition=composition or ["601088"],
        nav_series=nav_series or [],
        nav_dates=nav_dates or [],
        quarterly_holdings=quarterly_holdings or [],
    )


def _make_session(**kw):
    """构造一个鸭子类型 session（build_strategy_text_summary 只用 getattr）。"""
    df = kw.pop(
        "df",
        pd.DataFrame([
            {"stock_code": "601088", "stock_name": "中国神华"},
        ]),
    )
    s = SimpleNamespace(
        evaluation_reports=kw.get("evaluation_reports"),
        signal_scan=kw.get("signal_scan"),
        placements=kw.get("placements"),
        _historical=kw.get("_historical", {}),
    )
    s.get_all_dataframe = lambda: df
    return s


class TestStrategyTextSummary:
    def test_empty_session_blank(self):
        from notification.email_notifier import build_strategy_text_summary
        s = _make_session()
        assert build_strategy_text_summary(s) == ""

    def test_three_groups_shown(self):
        from notification.email_notifier import build_strategy_text_summary

        reports = {
            "a_share": _make_report(group="a_share", total_return=15.0,
                                     excess_return=12.0, composition=["601088"]),
            "hk": _make_report(group="hk", total_return=8.0,
                                excess_return=5.0, composition=["00883"]),
            "us": _make_report(group="us", total_return=20.0,
                                excess_return=17.0, composition=["VOO"]),
        }
        s = _make_session(evaluation_reports=reports)
        out = build_strategy_text_summary(s)
        assert "A股组合" in out
        assert "港股组合" in out
        assert "美股组合" in out
        assert "+15.0%" in out
        assert "+12.0%" in out  # 超额
        assert "评估时间 2026-07-09" in out
        assert "分位评分" in out

    def test_win_rate_shown(self):
        from notification.email_notifier import build_strategy_text_summary

        r = _make_report(
            group="a_share",
            benchmark_returns={"risk_free": 1.0, "510300": 5.0, "510880": 3.0},
        )
        s = _make_session(evaluation_reports={"a_share": r})
        out = build_strategy_text_summary(s)
        assert "验证期胜率" in out
        assert "510300" in out
        assert "510880" in out
        assert "无风险" in out

    def test_signals_readable_names(self):
        from notification.email_notifier import build_strategy_text_summary

        alerts = [
            SimpleNamespace(
                stock_code="600938", rule_label="趋势跟踪", current_value="ADX=35"
            ),
            SimpleNamespace(
                stock_code="00883", rule_label="放量异动", current_value="VOL=2.1"
            ),
        ]
        scan = SimpleNamespace(alerts=alerts)
        r = _make_report()
        s = _make_session(evaluation_reports={"a_share": r}, signal_scan=scan)
        out = build_strategy_text_summary(s)
        assert "600938 趋势跟踪" in out
        assert "00883 放量异动" in out

    def test_no_signal_shows_none(self):
        from notification.email_notifier import build_strategy_text_summary

        scan = SimpleNamespace(alerts=[])
        r = _make_report()
        s = _make_session(evaluation_reports={"a_share": r}, signal_scan=scan)
        out = build_strategy_text_summary(s)
        assert "今日信号: 无触发" in out

    def test_placements_shown(self):
        from notification.email_notifier import build_strategy_text_summary

        placements = {
            "601088": {
                "issue_num": 457665903.0,
                "issue_price": 43.7,
                "pct_of_total": 2.11,
                "unlock_date": "2029-04-08",
                "is_locked": True,
            },
        }
        r = _make_report()
        s = _make_session(evaluation_reports={"a_share": r}, placements=placements)
        out = build_strategy_text_summary(s)
        assert "未解禁定增" in out
        assert "601088" in out
        assert "4.58亿股" in out

    def test_markdown_bold(self):
        from notification.email_notifier import build_strategy_text_summary

        scan = SimpleNamespace(alerts=[])
        r = _make_report()
        s = _make_session(evaluation_reports={"a_share": r}, signal_scan=scan)
        md = build_strategy_text_summary(s, markdown=True)
        plain = build_strategy_text_summary(s, markdown=False)
        assert "**今日信号**" in md
        assert "**" not in plain


class TestReadableSignalByGroup:
    """Bug2 + 三组拆分: A股/港股/美股信号名各用自己的 YAML 映射。"""

    def test_a_share_uses_a_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_1": "偏离穿越"}
        map_hk = {"buy_1": "趋势跟踪"}
        map_us = {"buy_1": "放量异动"}
        assert _readable_signal("601728", "buy_1", map_a, map_hk, map_us) == "偏离穿越"

    def test_hk_uses_hk_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_1": "偏离穿越"}
        map_hk = {"buy_1": "趋势跟踪"}
        map_us = {"buy_1": "放量异动"}
        assert _readable_signal("00883", "buy_1", map_a, map_hk, map_us) == "趋势跟踪"

    def test_us_uses_us_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_4": "RSI超卖"}
        map_hk = {"buy_4": "深度价值"}
        map_us = {"buy_4": "布林低位"}
        assert _readable_signal("VOO", "buy_4", map_a, map_hk, map_us) == "布林低位"

    def test_us_falls_back_to_hk_when_no_us_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_1": "偏离穿越"}
        map_hk = {"buy_1": "趋势跟踪"}
        assert _readable_signal("VOO", "buy_1", map_a, map_hk) == "趋势跟踪"

    def test_unknown_falls_back_to_raw(self):
        from notification.email_notifier import _readable_signal
        assert _readable_signal("601728", "buy_9", {}, {}, {}) == "buy_9"

