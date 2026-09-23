"""
模块导入完整性验证

验证所有 src/ 下关键模块均可成功导入。
防止目录重组后 import 路径回归。
"""

from pathlib import Path


def test_extension_package_imports():
    """Strategy, search and backtest expose stable package APIs."""
    import src.backtest
    import src.search
    import src.strategy

    assert src.strategy.list_strategy_ids()
    assert src.search.list_solvers()
    assert src.backtest.Backtester


def test_service_components_import_without_http_server():
    """Bot SDKs stay lazy and the service imports without retired HTTP code."""
    from src.core.config_store import ConfigStore
    from src.core.runtime_service import RuntimeService
    from src.core.schedule_manager import ScheduleManager

    assert ConfigStore and RuntimeService and ScheduleManager


def test_all_key_modules():
    """关键模块批量导入"""
    modules = [
        # analysis
        "src.search.config",
        "src.strategy",
        "src.backtest.engine",
        "src.search.workflow",
        "src.strategy",
        "src.markets",
        # core
        "src.core.config_store",
        "src.core.runtime_service",
        "src.core.condition_checker",
        "src.core.data_fetcher",
        "src.core.schedule_manager",
        # Bot transports (SDK imports must be lazy)
        "src.interactive.feishu_bot",
        "src.interactive.telegram_bot",
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
        "src/strategy",
        "src/search",
        "src/backtest",
        "src/experiments",
        "src/core",
        "src/data",
        "src/models",
        "src/notification",
        "src/utils",
        "src/templates",
    ]
    missing = [d for d in required_dirs if not (root / d).is_dir()]
    assert not missing, f"Missing directories: {missing}"
