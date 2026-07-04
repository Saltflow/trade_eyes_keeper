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
from src.notification.manager import NotifierManager
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


def run_daily_task(force: bool = False):
    """每日运行的任务

    Args:
        force: True = 手动触发，跳过周末/休市检查
    """
    if not force:
        force = os.getenv("BOT_FORCE") == "1"
    logger = logging.getLogger(__name__)
    logger.info("开始执行每日任务")
    # 加载配置
    config = load_config()

    # 周末跳过（仅定时）
    if not force:
        today = datetime.now().date()
        if today.weekday() >= 5:
            logger.info(f"今天是周末 ({today})，跳过日报")
            return

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

        # 4. 创建通知管理器（统一入口）
        notifier = NotifierManager(config)

        # 5. 投资组合策略分析
        try:
            from src.analysis.portfolio_strategy import PortfolioOptimizer
            from src.analysis.rule_engine import Rule

            logger.info("开始投资组合策略分析")

            # 尝试从最新优化器 YAML 加载规则
            custom_rules = None
            try:
                opt_dir = Path("data/optimizer")
                yaml_files = sorted(
                    opt_dir.glob("*_a_share_strategies.yaml"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if yaml_files:
                    with open(yaml_files[0], "r", encoding="utf-8") as f:
                        opt_data = yaml.safe_load(f)
                    top = (opt_data.get("strategies") or [{}])[0]
                    opt_rules = top.get("rules", [])
                    if opt_rules:
                        custom_rules = [Rule.from_dict(r) for r in opt_rules]
                        mode = top.get("params", {}).get("_mode", "?")
                        logger.info(
                            "每日报告使用优化器策略 (mode=%s, rules=%d 条)",
                            mode, len(custom_rules),
                        )
            except Exception as e:
                logger.warning(f"读取优化器策略失败，将使用config默认规则: {e}")

            optimizer = PortfolioOptimizer(config, custom_rules=custom_rules)
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


def run_brief_report(report_id: str = "morning_snapshot", force: bool = False):
    """
    运行简报任务（轻量级：仅价格 + 锚点偏离率）。

    由 scheduler 通过 functools.partial 调用，或通过 CLI --brief 手动触发。

    Args:
        report_id: 简报 ID，对应 config scheduler.brief_reports[].id
        force: True = 跳过周末/休市检测（手动触发用）
    """
    if not force:
        force = os.getenv("BOT_FORCE") == "1"

    logger = logging.getLogger(__name__)
    logger.info(f"开始执行简报任务: {report_id}")

    config = load_config()
    today = datetime.now().date()

    # 周末跳过（仅定时）
    if not force and today.weekday() >= 5:
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

        # 只获取股票数据，跳过 LLM/财报/回测/投资组合（使用实时行情模式）
        fetcher = StockDataFetcher(config)
        fetcher.fetch_to_session(session, session_manager, realtime_mode=True)

        if not session.stocks_data:
            logger.warning("简报：Session 中无股票数据")
            return

        logger.info(f"简报：获取到 {len(session.stocks_data)} 只股票数据")

        # 休市检测：数据指纹比较（仅定时，按简报类型区分文件）
        if not force:
            from src.utils.market_status import is_market_closed, mark_pushed
            stock_data_df = session.get_all_dataframe()
            last_pushed_file = Path(f"cache/last_pushed_{report_id}.txt")
            if is_market_closed(stock_data_df, last_pushed_file):
                logger.info("数据未更新（疑似休市），跳过简报推送")
                return

            # 发送简报（统一入口）
            notifier = NotifierManager(config)
            notifier.send_brief_report(session, report_config)

            # 记录推送日期
            mark_pushed(last_pushed_file, stock_data_df)
        else:
            # 手动触发：直接推送，不记录
            notifier = NotifierManager(config)
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

        # ── 发送优化结果通知 ──
        notifier = NotifierManager(config)
        notifier.send_optimizer_notification(report, group_name)

    logger.info("策略搜索完成")


def run_optimization_v2(config):
    """
    运行策略搜索优化器 V2。

    使用 Walk-Forward + 遗传搜索 + 向量化快速评估。
    深夜运行，3-4 小时完成 A股 + 非A股 两组搜索。
    """
    import logging
    import time
    from src.data.data_source import DataSource
    from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
    from src.analysis.portfolio_strategy import _detect_stock_group
    from src.analysis.indicator_library import compute_all

    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("策略搜索优化器 V2 启动")
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
    # V2 需要 3年数据 (36个月 Walk-Forward)
    lookback = config.get("portfolio_strategy", {}).get("lookback_days", 1095)

    for group_name, codes in [("a_share", a_codes), ("non_a_share", non_a_codes)]:
        if not codes:
            logger.info("跳过 %s: 无标的", group_name)
            continue

        logger.info("-" * 40)
        logger.info("开始 %s 策略优化 V2 (%d 只标的)", group_name, len(codes))

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

        # 预计算指标（供 WalkForwardManager 使用，加速）
        logger.info("[V2] 预计算技术指标...")
        t0 = time.time()
        try:
            indicators = compute_all(stocks_data)
            logger.info("[V2] 指标计算完成: %.0fs", time.time() - t0)
        except Exception as e:
            logger.warning("[V2] 指标预计算失败，将兜底计算: %s", e)
            indicators = None

        # 运行 V2 优化器
        t0 = time.time()
        n_samples = int(os.getenv("OPTIMIZER_SAMPLES") or "0")
        n_gens = int(os.getenv("OPTIMIZER_GENERATIONS") or "0")
        optimizer = StrategyOptimizerV2(
            stocks_data, group_name,
            indicators_data=indicators,
        )
        report = optimizer.run(
            stock_codes=list(stocks_data.keys()),
            iterations=n_samples or None,
            random_starts=n_gens or None,
        )

        # 打印报告
        print("\n" + "=" * 70)
        print(f"  V2 策略搜索结果 — {group_name} — {report.report_id}")
        print("=" * 70)
        for i, t in enumerate(report.top_strategies[:5], 1):
            stocks_str = t.params.get("_stocks", "?")
            print(
                f"  [{i}] WF得分: 测试超额 {t.test_return:+.1f}% | "
                f"回撤 {t.test_drawdown:+.1f}% | 夏普 {t.sharpe:.3f} | "
                f"{t.trade_count}笔交易 | {stocks_str}"
            )
            for j in range(5):
                sig = t.params.get(f"buy_{j+1}_signal", "?")
                th = t.params.get(f"buy_{j+1}_t", "?")
                fr = t.params.get(f"buy_{j+1}_frac", "?")
                print(f"       buy_{j+1}: {sig:<20} t={th:<6} frac={fr}")

        logger.info(
            "%s V2 优化完成: 耗时 %.0fs",
            group_name,
            time.time() - t0,
        )

        # ── 发送优化结果通知（飞书 + Telegram + 邮件）──
        notifier = NotifierManager(config)
        notifier.send_optimizer_notification(report, group_name)

    logger.info("策略搜索 V2 完成")


def _send_optimizer_report_telegram(config, report):
    """通过 Telegram 发送 V2 优化器报告摘要"""
    import logging
    import os
    import requests

    _logger = logging.getLogger(__name__)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        _logger.info("Telegram 未配置，跳过报告发送")
        return

    lines = []
    lines.append(f"<b>策略搜索 V2 报告 - {report.report_id}</b>")
    lines.append(f"组别: {report.group}")
    lines.append(f"迭代: {report.iterations} | 耗时: {report.elapsed_seconds:.0f}s")
    lines.append("")

    for i, t in enumerate(report.top_strategies[:5], 1):
        stocks = t.params.get("_stocks", "?")
        lines.append(
            f"#{i} 测试超额 <code>{t.test_return:+.1f}%</code> | "
            f"回撤 <code>{t.test_drawdown:.1f}%</code> | 夏普 {t.sharpe:.2f} | {t.trade_count}笔"
        )
        for j in range(5):
            sig = t.params.get(f"buy_{j+1}_signal", "?")
            if sig == "none":
                continue
            th = t.params.get(f"buy_{j+1}_t", "?")
            fr = t.params.get(f"buy_{j+1}_frac", "?")
            lines.append(f"  • buy_{j+1}: {sig} t={th} frac={fr}")
        for j in range(3):
            sig = t.params.get(f"sell_{j+1}_signal", "?")
            if sig == "none" or sig == "?":
                continue
            th = t.params.get(f"sell_{j+1}_t", "?")
            fr = t.params.get(f"sell_{j+1}_frac", "?")
            lines.append(f"  • sell_{j+1}: {sig} t={th} frac={fr}")
        lines.append("")

    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            _logger.info("Telegram 优化报告发送成功")
        else:
            _logger.warning("Telegram 报告发送失败: HTTP %d", resp.status_code)
    except Exception as e:
        _logger.warning("Telegram 报告发送异常: %s", e)


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
            # 策略优化模式 (V1: 贝叶斯优化)
            run_optimization(config)
        elif sys.argv[1] == "--optimize-v2":
            # 策略优化模式 V2 (遗传搜索 + Walk-Forward)
            run_optimization_v2(config)
        elif sys.argv[1] == "--health-server":
            # 仅启动健康服务器模式
            logger.info("启动健康服务器模式")
            from src.health_server import start_health_server

            start_health_server()
        elif sys.argv[1] == "--interactive":
            # Telegram 交互 Bot 模式
            logger.info("启动 Telegram 交互 Bot")
            from src.interactive.telegram_bot import TelegramBot

            bot = TelegramBot(config)
            bot.run()
        elif sys.argv[1] == "--help":
            # 显示帮助信息
            print("股票量化系统使用说明:")
            print("  python main.py              # 启动定时任务调度器（默认）")
            print("  python main.py --once       # 单次运行任务")
            print("  python main.py --brief      # 运行早盘简报（默认9:50触发）")
            print("  python main.py --brief <id> # 运行指定简报")
            print("  python main.py --optimize              # 策略参数贝叶斯优化搜索 (V1)")
            print("  python main.py --optimize-v2           # 策略参数遗传搜索 + Walk-Forward (V2)")
            print("  python main.py --health-server # 仅启动健康服务器")
            print("  python main.py --interactive   # 启动 Telegram 交互 Bot")
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
