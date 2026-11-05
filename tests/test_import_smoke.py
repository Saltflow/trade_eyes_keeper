"""
模块导入完整性验证

验证所有 src/ 下关键模块均可成功导入。
防止目录重组后 import 路径回归。
"""

from pathlib import Path


def test_models_keep_stock_session_contracts_without_retired_alert_api():
    from src import models
    from src.models import converters

    assert models.StockPriceData and models.SessionContext
    assert not hasattr(models, "AlertStock")
    assert not hasattr(converters, "alert_dict_to_alert_stock")


def test_reference_portfolio_keeps_manual_management_without_auto_rebalance():
    from src.core.ref_portfolio import RefPortfolioManager

    assert callable(RefPortfolioManager.adjust_position)
    assert callable(RefPortfolioManager.apply_corporate_actions)
    assert callable(RefPortfolioManager.get_status)
    assert not hasattr(RefPortfolioManager, "rebalance_plan")


def test_extension_package_imports():
    """Strategy and explicit backtest APIs remain after search retirement."""
    import src.backtest
    import src.search
    import src.strategy

    assert src.strategy.list_strategy_ids()
    assert not hasattr(src.search, "run_optimizer")
    assert not hasattr(src.search, "create_solver")
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
        "src.markets",
        # core
        "src.core.ref_portfolio",
        "src.core.config_store",
        "src.core.runtime_service",
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
        "src.notification.feishu_notifier",
        "src.notification.telegram_notifier",
        # utils
        "src.utils.font_setup",
        "src.utils.etf_detector",
    ]
    import importlib

    for mod_name in modules:
        importlib.import_module(mod_name)


def test_project_structure():
    """项目关键目录存在（防重组回归）"""
    root = Path(__file__).parent.parent
    # logs/cache are ignored runtime outputs, created lazily by their owners.
    # A clean checkout must contain source directories, not historic outputs.
    required_dirs = [
        "config",
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


def test_retired_optimizer_cli_flags_fail_before_config_or_runtime(monkeypatch):
    import main
    import pytest

    def unexpected_config(*args, **kwargs):
        raise AssertionError("retired CLI must stop before loading runtime config")

    monkeypatch.setattr(main, "load_config", unexpected_config)
    for arguments in (
        ["--optimize"], ["--activate-run", "old-run"],
        ["--group", "a_share"], ["--universe-robustness"],
    ):
        with pytest.raises(SystemExit) as error:
            main.main(arguments)
        assert error.value.code == 2
