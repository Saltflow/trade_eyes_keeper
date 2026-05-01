"""
模块导入完整性验证

验证所有 src/ 下关键模块均可成功导入。
防止目录重组后 import 路径回归。
"""

import sys
from pathlib import Path


def test_analysis_imports():
    """analysis 子包导入"""
    from src.analysis.backtest_config import BacktestConfig, elapsed_months
    from src.analysis.indicator_library import compute_all, add_rsi, add_macd
    from src.analysis.strategy_optimizer import (
        StrategyOptimizer, OptimizationReport, StrategyTrial,
    )
    from src.analysis.signal_scanner import SignalScanner, ScanResult
    from src.analysis.portfolio_strategy import PortfolioEvaluator, PortfolioResult
    from src.analysis.rule_engine import RuleEngine, Rule, ExpressionEngine
    assert True


def test_health_server_imports():
    """health_server 子包导入"""
    from src.health_server.core.global_instances import (
        register_report_token, get_report_path, set_report_token_timeout,
    )
    assert True


def test_all_key_modules():
    """关键模块批量导入"""
    modules = [
        # analysis
        "src.analysis.backtest_config",
        "src.analysis.indicator_library",
        "src.analysis.strategy_optimizer",
        "src.analysis.signal_scanner",
        "src.analysis.portfolio_strategy",
        "src.analysis.rule_engine",
        # health_server
        "src.health_server.core.global_instances",
        "src.health_server.core.health_server",
        "src.health_server.handlers.health_handler",
        "src.health_server.auth.rate_limiter",
        "src.health_server.auth.otp_manager",
        "src.health_server.auth.auth_session",
        # core
        "src.core.condition_checker",
        "src.core.data_fetcher",
        "src.core.scheduler_manager",
        # data
        "src.data.data_source",
        "src.data.web_crawler",
        "src.data.technical_indicators",
        # models
        "src.models.schemas",
        "src.models.converters",
        # notification
        "src.notification.email_notifier",
        # utils
        "src.utils.font_setup",
        "src.utils.etf_detector",
    ]
    import importlib

    failures = []
    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
        except Exception as e:
            failures.append(f"{mod_name}: {e}")

    assert not failures, f"Import failures ({len(failures)}): {failures}"


def test_project_structure():
    """项目关键目录存在（防重组回归）"""
    root = Path(__file__).parent.parent
    required_dirs = [
        "config",
        "logs",
        "cache",
        "src/analysis",
        "src/health_server/core",
        "src/health_server/handlers",
        "src/health_server/auth",
        "src/core",
        "src/data",
        "src/models",
        "src/notification",
        "src/utils",
        "src/templates",
    ]
    missing = [d for d in required_dirs if not (root / d).is_dir()]
    assert not missing, f"Missing directories: {missing}"
