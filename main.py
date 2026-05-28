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
from src.core.data_fetcher import StockDataFetcher
from src.core.condition_checker import ConditionChecker
from src.notification.email_notifier import EmailNotifier
from src.core.scheduler_manager import SchedulerManager
from src.data.announcement_fetcher import AnnouncementFetcher
from src.session.session_manager import SessionManager


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

        # 3b. 策略信号扫描（基于最新优化结果）
        try:
            from src.analysis.signal_scanner import SignalScanner

            def _merge_consensus(a, b):
                """合并两个 ConsensusReport，求并集"""
                from src.analysis.signal_scanner import ConsensusReport
                return ConsensusReport(
                    buy_signal_counts={**a.buy_signal_counts, **b.buy_signal_counts},
                    sell_signal_counts={**a.sell_signal_counts, **b.sell_signal_counts},
                    stock_inclusion_counts={**a.stock_inclusion_counts, **b.stock_inclusion_counts},
                    consensus_buy_signals=list(set(a.consensus_buy_signals + b.consensus_buy_signals)),
                    consensus_stocks=list(set(a.consensus_stocks + b.consensus_stocks)),
                    consensus_indicators=list(set(a.consensus_indicators + b.consensus_indicators)),
                )

            logger.info("开始策略信号扫描")
            scanner = SignalScanner()
            scan_result = scanner.scan(session, "a_share", top_n=5)
            # 合并策略告警到 session.alerts
            for sa in scan_result.alerts:
                session.alerts.append({
                    "type": "strategy",
                    "stock_code": sa.stock_code,
                    "rule_id": sa.rule_id,
                    "rule_label": sa.rule_label,
                    "condition": sa.condition_str,
                    "current_value": sa.current_value,
                    "strategy_rank": sa.strategy_rank,
                })

            # 境外标的也扫描
            logger.info("开始境外策略信号扫描")
            nona_result = scanner.scan(session, "non_a_share", top_n=5)
            for sa in nona_result.alerts:
                session.alerts.append({
                    "type": "strategy",
                    "stock_code": sa.stock_code,
                    "rule_id": sa.rule_id,
                    "rule_label": sa.rule_label,
                    "condition": sa.condition_str,
                    "current_value": sa.current_value,
                    "strategy_rank": sa.strategy_rank,
                })
            # 合并 indicator_snapshot: A 股 + 境外
            merged_snapshot = dict(scan_result.indicator_snapshot or {})
            merged_snapshot.update(nona_result.indicator_snapshot or {})
            scan_result.indicator_snapshot = merged_snapshot
            # 合并共识报告
            merged_consensus = _merge_consensus(scan_result.consensus, nona_result.consensus)
            scan_result.consensus = merged_consensus

            # 存入共识数据供邮件使用
            session.signal_scan = scan_result
            logger.info(
                f"策略信号扫描完成: A股={len(scan_result.alerts)} + "
                f"境外={len(nona_result.alerts)} 个策略告警"
            )
        except Exception as e:
            logger.warning(f"策略信号扫描失败 (非致命): {e}")
            session.signal_scan = None

        # 3c. 回测分析（基于最新优化策略）
        try:
            logger.info("开始回测分析")
            # 注入基准 ETF 数据（510300 沪深300, 510880 红利ETF）
            historical = getattr(session, "_historical", {}) or {}
            bench_codes = ["510300", "510880"]
            bench_data = {}
            for bc in bench_codes:
                if bc in historical:
                    bench_data[bc] = historical[bc]
            # 如果 510300 不在股票列表, 单独抓取
            for bc in bench_codes:
                if bc not in bench_data:
                    try:
                        df = StockDataFetcher(config).data_source.fetch_stock_data(bc, days=120)
                        if df is not None and not df.empty:
                            bench_data[bc] = df
                            logger.info(f"已单独获取基准 ETF {bc} 数据 ({len(df)} 行)")
                    except Exception:
                        logger.warning(f"无法获取基准 ETF {bc} 数据")
            if bench_data:
                scanner.benchmark_data = bench_data

            bt_a = scanner.run_backtest(session, "a_share")
            bt_nona = scanner.run_backtest(session, "non_a_share")
            session.backtest = {}
            if bt_a:
                session.backtest["a_share"] = bt_a
            if bt_nona:
                session.backtest["non_a_share"] = bt_nona
            logger.info(
                f"回测分析完成: A股={'OK' if bt_a else 'N/A'}, "
                f"非A={'OK' if bt_nona else 'N/A'}"
            )
        except Exception as e:
            logger.warning(f"回测分析失败 (非致命): {e}")
            session.backtest = None

        # 4. 创建邮件通知器
        notifier = EmailNotifier(config)

        # 5. 投资组合策略分析
        try:
            from src.analysis.portfolio_strategy import PortfolioOptimizer

            logger.info("开始投资组合策略分析")
            optimizer = PortfolioOptimizer(config)
            portfolio_results = optimizer.run()
            if portfolio_results:
                session.portfolio_results = portfolio_results
                logger.info("投资组合策略分析完成")
            else:
                logger.warning("投资组合策略分析未返回结果")
        except Exception as e:
            logger.error(f"投资组合策略分析失败: {e}", exc_info=True)

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


def run_brief_report(report_id: str = "morning_snapshot"):
    """
    运行简报任务（轻量级：仅价格 + 锚点偏离率）。

    由 scheduler 通过 functools.partial 调用，或通过 CLI --brief 手动触发。

    Args:
        report_id: 简报 ID，对应 config scheduler.brief_reports[].id
    """
    logger = logging.getLogger(__name__)
    logger.info(f"开始执行简报任务: {report_id}")

    config = load_config()
    today = datetime.now().date()

    # 周末跳过
    if today.weekday() >= 5:
        logger.info(f"今天是周末 ({today})，跳过简报")
        return

    try:
        # 查找简报配置
        brief_configs = config.get("scheduler", {}).get("brief_reports", [])
        report_config = next(
            (b for b in brief_configs if b.get("id") == report_id), {}
        )
        if not report_config:
            logger.warning(f"未找到简报配置: {report_id}，使用默认标签")
            report_config = {"id": report_id, "label": "简报"}

        # 创建轻量 Session（仅获取价格数据）
        session_manager = SessionManager(config)
        session = session_manager.create_session(config)
        logger.info(f"简报Session创建: {session.session_id}")

        # 只获取股票数据，跳过 LLM/财报/回测/投资组合
        fetcher = StockDataFetcher(config)
        fetcher.fetch_to_session(session, session_manager)

        if not session.stocks_data:
            logger.warning("简报：Session 中无股票数据")
            return

        logger.info(f"简报：获取到 {len(session.stocks_data)} 只股票数据")

        # 发送简报邮件
        notifier = EmailNotifier(config)
        notifier.send_brief_report(session, report_config)

        logger.info(f"简报任务完成: {report_id}")

    except Exception as e:
        logger.error(f"简报任务失败 ({report_id}): {e}", exc_info=True)


def run_optimization(config):
    """
    运行策略搜索优化器。
    
    使用贝叶斯优化搜索最优策略参数，分 A 股和非 A 股两组分别优化。
    """
    import logging
    import time
    from src.data.data_source import DataSource
    from src.analysis.strategy_optimizer import StrategyOptimizer
    from src.analysis.portfolio_strategy import _detect_stock_group

    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("策略搜索优化器启动")
    logger.info("=" * 60)

    stocks = config.get("stocks", [])
    if not stocks:
        logger.error("配置中没有股票列表")
        return

    # 分组
    a_codes = []
    non_a_codes = []
    for s in stocks:
        if isinstance(s, str):
            code = s
        elif isinstance(s, dict):
            code = s.get("code", "")
        else:
            code = str(s)
        group = _detect_stock_group(code)
        if group == "a_share":
            a_codes.append(code)
        else:
            non_a_codes.append(code)

    logger.info(f"A股: {len(a_codes)} 只 | 非A股: {len(non_a_codes)} 只")

    # 数据源
    data_source = DataSource(config)
    lookback = config.get("portfolio_strategy", {}).get("lookback_days", 730)

    for group_name, codes in [("a_share", a_codes), ("non_a_share", non_a_codes)]:
        if not codes:
            logger.info("跳过 %s: 无标的", group_name)
            continue

        logger.info("-" * 40)
        logger.info("开始 %s 策略优化 (%d 只标的)", group_name, len(codes))

        # 获取数据
        stocks_data: dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = data_source.fetch_stock_data(code, days=lookback)
                if df is not None and not df.empty:
                    stocks_data[code] = df
            except Exception as e:
                logger.warning("获取 %s 数据失败: %s", code, e)

        if not stocks_data:
            logger.warning("%s 无可用数据，跳过", group_name)
            continue

        # 获取基准 ETF 数据
        bench_codes = [("510300", "510300"), ("510880", "510880")]
        bench_data: dict[str, pd.DataFrame] = {}
        for bcode, bname in bench_codes:
            try:
                bdf = data_source.fetch_stock_data(bcode, days=lookback)
                if bdf is not None and not bdf.empty:
                    bench_data[bname] = bdf
            except Exception:
                pass
        if bench_data:
            logger.info("%s 基准 ETF: %d 只就绪", group_name, len(bench_data))

        # 运行优化
        t0 = time.time()
        optimizer = StrategyOptimizer(stocks_data, group_name)
        if bench_data:
            optimizer.set_benchmark_data(bench_data)
        report = optimizer.run(
            stock_codes=list(stocks_data.keys()),
        )

        # 打印报告
        print("\n" + optimizer.print_report(report))

        # 保存结果
        optimizer.save_results(report)

        logger.info(
            "%s 优化完成: 耗时 %.0fs",
            group_name,
            time.time() - t0,
        )

    logger.info("策略搜索完成")


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
        elif sys.argv[1] == "--brief":
            # 简报模式（默认 morning_snapshot）
            report_id = sys.argv[2] if len(sys.argv) > 2 else "morning_snapshot"
            logger.info(f"简报模式: {report_id}")
            run_brief_report(report_id)
        elif sys.argv[1] == "--optimize":
            # 策略优化模式
            run_optimization(config)
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
            print("  python main.py --brief      # 运行早盘简报（默认9:50触发）")
            print("  python main.py --brief <id> # 运行指定简报")
            print("  python main.py --optimize              # 策略参数贝叶斯优化搜索")
            print("  python main.py --health-server # 仅启动健康服务器")
            print("  python main.py --help       # 显示此帮助信息")
            print("\n健康服务器端口等配置见 config/config.yaml → health_server")
            return
        else:
            logger.error(f"未知参数: {sys.argv[1]}")
            print(f"未知参数: {sys.argv[1]}")
            print("使用 python main.py --help 查看可用参数")
            return
    else:
        # 定时运行模式
        logger.info("启动定时任务调度器")
        scheduler = SchedulerManager(
            config, task_function=run_daily_task, brief_function=run_brief_report
        )
        scheduler.start()


if __name__ == "__main__":
    main()
