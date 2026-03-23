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
        # 1. 获取公告信息（可选） - 提前获取以填充股息数据缓存
        announcements = {}
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
                logger.info(
                    f"公告获取完成，共获取{sum(len(v) for v in announcements.values())}条重要公告"
                )
            except Exception as e:
                logger.error(f"获取公告信息失败: {e}")
        # 2. 获取股票数据
        logger.info("开始获取股票数据")
        fetcher = StockDataFetcher(config)
        stock_data = fetcher.fetch_stock_data()
        if stock_data.empty:
            logger.warning("未获取到股票数据")
            return
        # 3. 检查条件
        logger.info("开始检查交易条件")
        checker = ConditionChecker(config)
        alert_stocks = checker.check_condition(stock_data)
        # 4. 创建邮件通知器
        notifier = EmailNotifier(config)
        # 5. LLM分析基本面（可选）
        analysis_results = {}
        llm_config = config.get("llm", {})
        api_key = llm_config.get("api_key")
        if api_key and api_key.strip():
            logger.info("开始LLM基本面分析")
            analyzer = LLMAnalyzer(config)
            # 将DataFrame转换为字典格式，键为股票代码，值为该股票的数据行
            stock_data_dict = {}
            if not stock_data.empty and "stock_code" in stock_data.columns:
                for _, row in stock_data.iterrows():
                    stock_code = str(row["stock_code"])
                    stock_data_dict[stock_code] = row.to_dict()
            analysis_results = analyzer.analyze_stocks(
                config["stocks"], stock_data_dict
            )
            logger.info(f"LLM分析完成，共分析{len(analysis_results)}只股票")
        else:
            logger.info("LLM API未配置，跳过基本面分析")
        # 7. 发送邮件（无论是否有满足条件的股票都发送日报）
        if alert_stocks:
            logger.info(f"发现{len(alert_stocks)}只满足条件的股票: {alert_stocks}")
            notifier.send_alert(
                alert_stocks, stock_data, analysis_results, announcements
            )
        else:
            logger.info("没有满足条件的股票，发送每日报告")
            notifier.send_daily_report(stock_data, analysis_results, announcements)
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
