#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票量化系统主程序
功能：
1. 获取自选股票天级别交易数据
2. 检查当天最低价 < MA60（前复权）条件
3. 满足条件时发送邮件提醒
4. 获取股票报告并分析基本面（LLM API）
"""

import os
import sys
import yaml
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "config", ".env"))
# 添加src目录到Python路径
sys.path.insert(0, str(Path(__file__).parent / "src"))
from src.data_fetcher import StockDataFetcher
from src.condition_checker import ConditionChecker
from src.email_notifier import EmailNotifier
from src.llm_analyzer import LLMAnalyzer
from src.scheduler_manager import SchedulerManager
from src.announcement_fetcher import AnnouncementFetcher
from src.financial_report_manager import FinancialReportManager
from src.session_manager import SessionManager
from backtest_framework import BacktestFramework


# 设置日志
def setup_logging(config):
    """配置日志系统"""
    log_config = config.get("logging", {})
    logging_level = getattr(logging, log_config.get("level", "INFO"))
    log_file = log_config.get("file", "./logs/quant_system.log")
    # 创建日志目录
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    # 配置日志格式
    log_format = log_config.get(
        "format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    # 配置文件处理器
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging_level)
    file_formatter = logging.Formatter(log_format)
    file_handler.setFormatter(file_formatter)
    # 配置控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging_level)
    console_formatter = logging.Formatter(log_format)
    console_handler.setFormatter(console_formatter)
    # 获取根日志器并配置
    logger = logging.getLogger()
    logger.setLevel(logging_level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def load_config(config_path=None):
    """加载配置文件"""
    try:
        if config_path is None:
            # 默认配置文件路径，基于当前文件位置
            current_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(current_dir, "config", "config.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        # 用环境变量覆盖配置
        if os.getenv("EMAIL_SENDER"):
            config.setdefault("email", {})["sender_email"] = os.getenv("EMAIL_SENDER")
        if os.getenv("EMAIL_PASSWORD"):
            config.setdefault("email", {})["sender_password"] = os.getenv(
                "EMAIL_PASSWORD"
            )
        if os.getenv("EMAIL_RECEIVER"):
            config.setdefault("email", {})["receiver_email"] = os.getenv(
                "EMAIL_RECEIVER"
            )
        deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        if deepseek_key and deepseek_key.strip():
            config.setdefault("llm", {})["api_key"] = deepseek_key.strip()
        if os.getenv("TUSHARE_TOKEN"):
            config.setdefault("data_source", {})["tushare_token"] = os.getenv(
                "TUSHARE_TOKEN"
            )
        return config
    except Exception as e:
        print(f"加载配置文件失败: {e}")
        sys.exit(1)


def run_daily_task():
    """每日运行的任务"""
    logger = logging.getLogger(__name__)
    logger.info("开始执行每日任务")
    # 加载配置
    config = load_config()
    try:
        # 创建Session（新数据流）
        session_manager = SessionManager(config)
        session = session_manager.create_session(config)
        logger.info(f"Session创建成功: {session.session_id}")

        # 1. 获取公告信息（可选） - 提前获取以填充股息数据缓存
        announcement_config = config.get("announcements", {})
        # 创建公告抓取器用于公告信息获取
        announcement_fetcher = AnnouncementFetcher(config)
        if announcement_config.get("enable", False):
            try:
                logger.info("开始获取股票公告信息")
                days = announcement_config.get("days", 7)
                dividend_days = announcement_config.get("dividend_days", 420)
                announcements = announcement_fetcher.get_recent_important_announcements(
                    config["stocks"], days, dividend_days
                )
                # 存入session
                session.announcements = announcements
                logger.info(
                    f"公告获取完成，共获取{sum(len(v) for v in announcements.values())}条重要公告"
                )
            except Exception as e:
                logger.error(f"获取公告信息失败: {e}")
                session.errors.append(f"获取公告信息失败: {e}")

        # 2. 获取股票数据并存入Session
        logger.info("开始获取股票数据")
        fetcher = StockDataFetcher(config)
        fetcher.fetch_to_session(session, session_manager)
        if not session.stocks_data:
            logger.warning("Session中无股票数据")
            return
        logger.info(f"股票数据获取完成: {len(session.stocks_data)}只股票")

        # 3. 检查条件并存入Session
        logger.info("开始检查交易条件")
        checker = ConditionChecker(config)
        checker.check_from_session(session, session_manager)
        logger.info(f"条件检查完成: {len(session.alerts)}个警报")

        # 4. 创建邮件通知器
        notifier = EmailNotifier(config)
        # 5. LLM分析基本面（可选）
        llm_config = config.get("llm", {})
        api_key = llm_config.get("api_key")
        analyzer = None  # 初始化分析器变量
        if api_key and api_key.strip():
            # 总是创建LLM分析器实例（供财报分析使用）
            analyzer = LLMAnalyzer(config)

            # 检查是否启用基本面分析
            enable_fundamental_analysis = llm_config.get(
                "enable_fundamental_analysis", True
            )
            if enable_fundamental_analysis:
                logger.info("开始LLM基本面分析")
                # 从Session获取数据并转换为字典格式
                stock_data_df = session.get_all_dataframe()
                stock_data_dict = {}
                if not stock_data_df.empty and "stock_code" in stock_data_df.columns:
                    for _, row in stock_data_df.iterrows():
                        stock_code = str(row["stock_code"])
                        stock_data_dict[stock_code] = row.to_dict()
                analysis_results = analyzer.analyze_stocks(
                    config["stocks"], stock_data_dict
                )
                session.analysis_results = analysis_results
                logger.info(f"LLM基本面分析完成，共分析{len(analysis_results)}只股票")
            else:
                logger.info("LLM基本面分析已禁用，跳过基本面分析")
        else:
            logger.info("LLM API未配置，跳过LLM相关功能")

        # 6. 财报分析（可选）
        financial_config = config.get("financial_reports", {})
        if financial_config.get("enable", True):
            try:
                logger.info("开始财报分析检查")

                # 确定要传递给财报管理器的LLM分析器
                financial_llm_analyzer = None
                if api_key and api_key.strip():
                    financial_llm_analyzer = analyzer  # 使用已创建的LLM分析器实例
                    logger.info("使用LLM分析器进行财报分析")
                else:
                    logger.warning("LLM API未配置，跳过财报分析")

                # 获取内容抓取器（如果可用）
                content_fetcher = getattr(announcement_fetcher, "content_fetcher", None)

                financial_manager = FinancialReportManager(
                    config,
                    announcement_fetcher,
                    financial_llm_analyzer,
                    financial_report_fetcher=None,  # 让管理器自动创建
                    content_fetcher=content_fetcher,
                )
                # 从Session获取警报列表并提取股票代码（确保类型为List[str]）
                alert_stocks_dicts = session.get_alerts_as_dicts()
                alert_stock_codes = (
                    [
                        str(alert.get("stock_code"))
                        for alert in alert_stocks_dicts
                        if alert.get("stock_code") is not None
                    ]
                    if alert_stocks_dicts
                    else []
                )
                should_analyze, stocks_to_analyze = (
                    financial_manager.should_analyze_financial_reports(
                        alert_stock_codes
                    )
                )
                if should_analyze:
                    logger.info(f"需要财报分析: {stocks_to_analyze}")
                    financial_analysis_results = (
                        financial_manager.analyze_financial_reports(stocks_to_analyze)
                    )
                    session.financial_analysis_results = financial_analysis_results
                    logger.info(
                        f"财报分析完成: {len(financial_analysis_results)}只股票有结果"
                    )
                else:
                    logger.info("无需财报分析")
            except Exception as e:
                logger.error(f"财报分析失败: {e}", exc_info=True)
                session.errors.append(f"财报分析失败: {e}")

        # 7. 运行回测分析（可选）
        backtest_results = None
        # 读取回测配置
        backtest_config = config.get("backtest", {})
        backtest_enable = backtest_config.get("enable", True)  # 默认启用

        if backtest_enable:
            logger.info("回测功能已启用，开始回测分析")
            try:
                backtest_framework = BacktestFramework()
                backtest_results = backtest_framework.get_backtest_results()
                if backtest_results:
                    logger.info(f"回测分析完成，共{len(backtest_results)}只股票")
                else:
                    logger.warning("回测分析未返回结果")
            except Exception as e:
                logger.error(f"回测分析失败: {e}", exc_info=True)
                # 回测失败不影响主流程，继续发送邮件
        else:
            logger.info("回测功能已禁用，跳过回测分析")

        # 8. 发送邮件（无论是否有满足条件的股票都发送日报）
        if session.alerts:
            logger.info(f"发现{len(session.alerts)}个满足条件的警报")
            notifier.send_from_session(session)
        else:
            logger.info("没有满足条件的股票，发送每日报告")
            notifier.send_daily_report_from_session(session)
        logger.info("每日任务执行完成")
    except Exception as e:
        logger.error(f"执行任务时发生错误: {e}", exc_info=True)


def main():
    """主函数"""
    # 加载配置
    config = load_config()
    # 设置日志
    logger = setup_logging(config)
    # 解析命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == "--once":
            # 单次运行模式
            logger.info("单次运行模式")
            run_daily_task()
        elif sys.argv[1] == "--health-server":
            # 仅启动健康服务器模式
            logger.info("启动健康服务器模式")
            from src.health_server import start_health_server

            start_health_server()
        elif sys.argv[1] == "--help":
            # 显示帮助信息
            print("股票量化系统使用说明:")
            print("  python main.py              # 启动定时任务调度器（默认）")
            print("  python main.py --once       # 单次运行任务")
            print("  python main.py --health-server # 仅启动健康服务器")
            print("  python main.py --help       # 显示此帮助信息")
            print("\n健康服务器运行在端口1933，提供系统状态监控和测试邮件功能")
            return
        else:
            logger.error(f"未知参数: {sys.argv[1]}")
            print(f"未知参数: {sys.argv[1]}")
            print("使用 python main.py --help 查看可用参数")
            return
    else:
        # 定时运行模式
        logger.info("启动定时任务调度器")
        scheduler = SchedulerManager(config, task_function=run_daily_task)
        scheduler.start()


if __name__ == "__main__":
    main()
