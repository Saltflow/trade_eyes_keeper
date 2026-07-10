"""三端信息量对齐测试：build_strategy_text_summary 共享摘要。

验证 Telegram/飞书日报能拿到与邮件一致的信息：
搜参3组 + 验证期胜率 + 平均现金仓位 + 今日信号(可读名) + 未解禁定增。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_result(nav, dates, comp, qh=None):
    from analysis.portfolio_strategy import PortfolioResult
    return PortfolioResult(
        name="top1", group="a_share",
        total_return=10.0, max_drawdown=-5.0, sharpe_ratio=1.0,
        expected_position=50000, composition=comp, trade_count=8,
        nav_series=nav, nav_dates=dates,
        quarterly_holdings=qh or [],
    )


def _make_session(**kw):
    """构造一个鸭子类型 session（build_strategy_text_summary 只用 getattr）。"""
    df = kw.pop("df", pd.DataFrame([
        {"stock_code": "601088", "stock_name": "中国神华"},
    ]))
    s = SimpleNamespace(
        portfolio_results=kw.get("portfolio_results"),
        signal_scan=kw.get("signal_scan"),
        placements=kw.get("placements"),
        opt_data_a=kw.get("opt_data_a"),
        opt_data_non_a=kw.get("opt_data_non_a"),
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
        dates = [f"2026-01-{i+1:02d}" for i in range(25)]
        nav = [100 + i for i in range(25)]
        pr = {
            "a_share": {"top1": _make_result(nav, dates, ["601088"])},
            "hk": {"top1": _make_result(nav, dates, ["00883"])},
            "us": {"top1": _make_result(nav, dates, ["VOO"])},
        }
        opt = {"timestamp": "2026-07-09T02:00:00",
               "strategies": [{"test_return": 12.0, "test_drawdown": -6.0, "sharpe": 1.1}]}
        s = _make_session(portfolio_results=pr, opt_data_a=opt, opt_data_non_a=opt)
        out = build_strategy_text_summary(s)
        assert "A股组合" in out
        assert "港股组合" in out
        assert "美股组合" in out
        assert "+12.0%" in out  # YAML test_return
        assert "搜参时间 2026-07-09" in out

    def test_win_rate_shown(self):
        from notification.email_notifier import build_strategy_text_summary
        dates = [f"2026-01-{i+1:02d}" for i in range(25)]
        nav = [100 + i * 2 for i in range(25)]  # 策略持续涨
        bench = pd.DataFrame({"date": dates, "close": [100.0] * 25})  # 基准平
        pr = {"a_share": {"top1": _make_result(nav, dates, ["601088"])}}
        opt = {"strategies": [{"test_return": 12.0, "test_drawdown": -6.0, "sharpe": 1.1}]}
        s = _make_session(portfolio_results=pr, opt_data_a=opt,
                          _historical={"510880": bench})
        out = build_strategy_text_summary(s)
        assert "验证期胜率 100%" in out
        assert "跑赢510880红利ETF" in out

    def test_signals_readable_names(self):
        from notification.email_notifier import build_strategy_text_summary
        alerts = [
            SimpleNamespace(stock_code="600938", rule_label="趋势跟踪",
                            current_value="ADX=35"),
            SimpleNamespace(stock_code="00883", rule_label="放量异动",
                            current_value="VOL=2.1"),
        ]
        scan = SimpleNamespace(alerts=alerts)
        s = _make_session(signal_scan=scan)
        out = build_strategy_text_summary(s)
        assert "今日信号 (2条 / 2只)" in out
        assert "600938 趋势跟踪" in out
        assert "00883 放量异动" in out

    def test_no_signal_shows_none(self):
        from notification.email_notifier import build_strategy_text_summary
        scan = SimpleNamespace(alerts=[])
        s = _make_session(signal_scan=scan)
        out = build_strategy_text_summary(s)
        assert "今日信号: 无触发" in out

    def test_placements_shown(self):
        from notification.email_notifier import build_strategy_text_summary
        placements = {
            "601088": {"issue_num": 457665903.0, "issue_price": 43.7,
                       "pct_of_total": 2.11, "unlock_date": "2029-04-08",
                       "is_locked": True},
        }
        s = _make_session(placements=placements)
        out = build_strategy_text_summary(s)
        assert "未解禁定增" in out
        assert "601088 中国神华" in out
        assert "4.58亿股" in out
        assert "占2.11%" in out
        assert "解禁2029-04-08" in out

    def test_markdown_bold(self):
        from notification.email_notifier import build_strategy_text_summary
        scan = SimpleNamespace(alerts=[])
        s = _make_session(signal_scan=scan)
        md = build_strategy_text_summary(s, markdown=True)
        plain = build_strategy_text_summary(s, markdown=False)
        assert "**今日信号**" in md
        assert "**" not in plain


class TestReadableSignalByGroup:
    """Bug2: 非A标的信号名应按非A YAML 映射，不能用A股映射。"""

    def test_a_share_uses_a_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_1": "偏离穿越"}
        map_n = {"buy_1": "趋势跟踪"}
        # 601728 是A股 → 用 map_a
        assert _readable_signal("601728", "buy_1", map_a, map_n) == "偏离穿越"

    def test_hk_uses_non_a_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_1": "偏离穿越"}
        map_n = {"buy_1": "趋势跟踪"}
        # 00883 港股 → 用 map_n
        assert _readable_signal("00883", "buy_1", map_a, map_n) == "趋势跟踪"

    def test_us_uses_non_a_map(self):
        from notification.email_notifier import _readable_signal
        map_a = {"buy_4": "RSI超卖"}
        map_n = {"buy_4": "深度价值"}
        # VOO 美股 → 用 map_n
        assert _readable_signal("VOO", "buy_4", map_a, map_n) == "深度价值"

    def test_unknown_falls_back_to_raw(self):
        from notification.email_notifier import _readable_signal
        assert _readable_signal("601728", "buy_9", {}, {}) == "buy_9"
