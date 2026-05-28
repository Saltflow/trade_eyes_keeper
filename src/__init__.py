"""
股票量化系统 — 核心模块 (v3.5-beta)
延迟导入避免测试时加载 matplotlib 等重型依赖
"""


def get_StockDataFetcher():
    from .core.data_fetcher import StockDataFetcher
    return StockDataFetcher


def get_ConditionChecker():
    from .core.condition_checker import ConditionChecker
    return ConditionChecker


def get_EmailNotifier():
    from .notification.email_notifier import EmailNotifier
    return EmailNotifier


def get_NotifierManager():
    from .notification.manager import NotifierManager
    return NotifierManager


def get_SchedulerManager():
    from .core.scheduler_manager import SchedulerManager
    return SchedulerManager
