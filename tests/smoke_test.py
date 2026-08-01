"""Smoke test: 快速检测代码完整性和常见运行时错误。

在每次部署前运行，验证所有模块可导入、关键函数签名正确.
不依赖外部数据——纯语法和导入检查.
"""

import sys
import os
import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("LOG_LEVEL", "ERROR")

FAILED = 0
OK = "[OK]"
BAD = "[FAIL]"


def check(desc: str, fn):
    global FAILED
    try:
        fn()
        print(f"  {OK} {desc}")
    except Exception as e:
        FAILED += 1
        print(f"  {BAD} {desc}: {e}")


# ── 1. 核心模块可导入 ──
def test_imports():
    modules = [
        "src.core.ref_portfolio",
        "src.search.config",
        "src.strategy",
        "src.backtest.engine",
        "src.search.workflow",
        "src.markets",
        "src.notification.email_notifier",
        "src.notification.feishu_notifier",
        "src.notification.telegram_notifier",
    ]
    for m in modules:
        check(f"import {m}", lambda m=m: importlib.import_module(m))


# ── 2. ref_portfolio 基础操作 ──
def test_ref_portfolio():
    from src.core.ref_portfolio import (
        RefPortfolioManager,
        RefPortfolio,
    )

    pf = RefPortfolio()
    check("RefPortfolio defaults", lambda: pf.cash == 100000.0 and pf.holdings == {})
    d = pf.to_dict()
    pf2 = RefPortfolio.from_dict(d)
    check("RefPortfolio round-trip", lambda: pf2.cash == pf.cash)

    mgr = RefPortfolioManager(file_path="/tmp/_smoke_test_pf.yaml")
    pf3 = mgr.load()
    check("RefPortfolioManager.load", lambda: isinstance(pf3, RefPortfolio))
    pf4 = mgr.reset(inception_date="2026-01-01")
    check("RefPortfolioManager.reset", lambda: pf4.inception_date == "2026-01-01")
    # cleanup
    try:
        os.remove("/tmp/_smoke_test_pf.yaml")
    except Exception:
        pass


# ── 3. 关键函数签名检查 ──
def test_function_signatures():
    import inspect

    # rebalance should accept fx_rate, force
    from src.core.ref_portfolio import RefPortfolioManager

    sig2 = inspect.signature(RefPortfolioManager.rebalance)
    check("rebalance has 'fx_rate'", lambda: "fx_rate" in sig2.parameters)
    check("rebalance has 'force'", lambda: "force" in sig2.parameters)

    # build_strategy_text_summary should exist
    from src.notification.email_notifier import build_strategy_text_summary

    check(
        "build_strategy_text_summary exists",
        lambda: callable(build_strategy_text_summary),
    )

    # EmailNotifier should have _build_ref_portfolio_html
    from src.notification.email_notifier import EmailNotifier

    check(
        "EmailNotifier._build_ref_portfolio_html exists",
        lambda: hasattr(EmailNotifier, "_build_ref_portfolio_html"),
    )

    # FeishuNotifier should have send_brief_report
    from src.notification.feishu_notifier import FeishuNotifier

    check(
        "FeishuNotifier.send_brief_report exists",
        lambda: hasattr(FeishuNotifier, "send_brief_report"),
    )


# ── 5. 命令处理 ──
def test_commands():
    from src.interactive.command_parser import parse_command, RefDateCommand

    cmd = parse_command("/ref_date 2026-07-14")
    check("/ref_date parser", lambda: isinstance(cmd, RefDateCommand))
    check("/ref_date date_str", lambda: cmd.date_str == "2026-07-14")


# ── 6. main.py 关键符号存在 ──
def test_main_symbols():
    import main

    check("main has run_daily_task", lambda: hasattr(main, "run_daily_task"))
    check("main has run_brief_report", lambda: hasattr(main, "run_brief_report"))
    # _eval_opt_lookback should return int > 0
    val = main._eval_opt_lookback()
    check("_eval_opt_lookback > 0", lambda: val > 0)


# ── Run ──
if __name__ == "__main__":
    print("=" * 50)
    print("SMOKE TEST")
    print("=" * 50)

    test_imports()
    test_ref_portfolio()
    test_function_signatures()
    test_commands()
    test_main_symbols()

    print("=" * 50)
    if FAILED:
        print(f"❌ {FAILED} FAILURES")
        sys.exit(1)
    else:
        print("✅ ALL PASSED")
