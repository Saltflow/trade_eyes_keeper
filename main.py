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
import threading
import traceback
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

        # 1b. 获取 A 股定增(定向增发)数据 — 展示未解禁定增
        try:
            from src.data.web_crawler import StockWebCrawler
            crawler = StockWebCrawler(config)
            placements = {}
            for code in config["stocks"]:
                code_str = str(code)
                if not (code_str.isdigit() and len(code_str) == 6):
                    continue  # 仅 A 股
                try:
                    p = crawler.fetch_placement_data(code_str)
                    if p and p.get("is_locked"):
                        placements[code_str] = p
                except Exception as e:
                    logger.debug(f"定增数据获取失败 {code_str}: {e}")
            session.placements = placements
            logger.info(f"定增数据获取完成，{len(placements)} 只标的有未解禁定增")
        except Exception as e:
            logger.warning(f"定增数据获取失败 (非致命): {e}")

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

            # 港股 + 美股各自扫描（独立 YAML 策略）
            logger.info("开始港股/美股策略信号扫描")
            hk_result = scanner.scan(session, "hk", top_n=5)
            us_result = scanner.scan(session, "us", top_n=5)
            nona_alerts = list(hk_result.alerts) + list(us_result.alerts)
            for sa in nona_alerts:
                session.alerts.append({
                    "type": "strategy",
                    "stock_code": sa.stock_code,
                    "rule_id": sa.rule_id,
                    "rule_label": sa.rule_label,
                    "condition": sa.condition_str,
                    "current_value": sa.current_value,
                    "strategy_rank": sa.strategy_rank,
                })
            # 合并 indicator_snapshot: A 股 + 港股 + 美股
            merged_snapshot = dict(scan_result.indicator_snapshot or {})
            merged_snapshot.update(hk_result.indicator_snapshot or {})
            merged_snapshot.update(us_result.indicator_snapshot or {})
            scan_result.indicator_snapshot = merged_snapshot
            # 合并共识报告
            merged_consensus = _merge_consensus(
                _merge_consensus(scan_result.consensus, hk_result.consensus),
                us_result.consensus,
            )
            scan_result.consensus = merged_consensus
            # 合并告警：A股 + 港股 + 美股（否则邮件今日信号只显示A股）
            n_a_alerts = len(scan_result.alerts)
            n_nona_alerts = len(nona_alerts)
            scan_result.alerts = list(scan_result.alerts) + nona_alerts

            # 存入共识数据供邮件使用
            session.signal_scan = scan_result
            logger.info(
                f"策略信号扫描完成: A股={n_a_alerts} + "
                f"境外={n_nona_alerts} 个策略告警"
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
            # 如果 510300 不在股票列表, 单独抓取（730天以匹配净值图2年曲线）
            for bc in bench_codes:
                if bc not in bench_data:
                    try:
                        df = StockDataFetcher(config).data_source.fetch_stock_data(bc, days=730)
                        if df is not None and not df.empty:
                            bench_data[bc] = df
                            historical[bc] = df
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

        # 5. 投资组合策略分析（标的池=config，策略规则=YAML，不选股不搜参）
        try:
            from src.analysis.portfolio_strategy import PortfolioOptimizer
            from src.analysis.rule_engine import Rule

            logger.info("开始投资组合策略分析（config标的 + YAML规则）")

            def _load_opt_yaml(group: str):
                """读取指定分组最新 YAML，返回 (opt_data, custom_rules, signal_fn, engine_params)。

                只取策略规则，不再读 _stocks — 标的池由 config 决定。
                """
                opt_dir = Path("data/optimizer")
                files = sorted(
                    [f for f in opt_dir.glob(f"*_{group}_strategies.yaml")
                     if (group == "non_a_share") == ("non_a_share" in f.name)],
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if not files:
                    return None, None, None, None
                with open(files[0], "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                top = (data.get("strategies") or [{}])[0]
                rules_raw = top.get("rules", [])
                params = top.get("params", {})
                mode = params.get("_mode", "?")
                engine_name = params.get("_engine", "global")

                # 分位/评分引擎：用 SignalFn 评分流水线回测（rules 保持 __signal_fn__）
                if engine_name in ("percentile", "pct", "new"):
                    from src.analysis.percentile_engine import PercentileSignalFn
                    from src.analysis.signal_scanner import _params_from_yaml
                    sig_fn = PercentileSignalFn()
                    eng_params = _params_from_yaml(params)
                    rules = [Rule.from_dict(r) for r in rules_raw] if rules_raw else None
                    return data, rules, sig_fn, eng_params

                # position_target → cash*frac 转换（RuleEngine 无法求值 position_target）
                if mode == "position_target":
                    for r in rules_raw:
                        rid = r.get("id", "")
                        idx = rid.split("_")[-1] if "_" in rid else "1"
                        if r.get("type") == "buy" and r.get("action_amount") == "position_target":
                            r["action_amount"] = f"cash * {params.get(f'buy_{idx}_frac', 0.1)}"
                        elif r.get("type") == "sell" and r.get("action_fraction", 0.25) == 0.0:
                            r["action_fraction"] = params.get(f"sell_{idx}_frac", 0.25)
                            r["action_min"] = 2500.0
                            r["action_max"] = 10000.0
                rules = [Rule.from_dict(r) for r in rules_raw] if rules_raw else None
                return data, rules, None, None

            a_data, a_rules, a_sfn, a_ep = _load_opt_yaml("a_share")
            hk_data, hk_rules, hk_sfn, hk_ep = _load_opt_yaml("hk")
            us_data, us_rules, us_sfn, us_ep = _load_opt_yaml("us")
            # 回退：hk/us YAML 尚未生成时用 non_a_share（旧格式兼容）
            if hk_rules is None or us_rules is None:
                n_data, n_rules, n_sfn, n_ep = _load_opt_yaml("non_a_share")
                if hk_rules is None:
                    hk_data, hk_rules, hk_sfn, hk_ep = n_data, n_rules, n_sfn, n_ep
                if us_rules is None:
                    us_data, us_rules, us_sfn, us_ep = n_data, n_rules, n_sfn, n_ep

            # 存 opt_data 供邮件展示 YAML 预估收益（Top1 test_return）
            session.opt_data_a = a_data
            session.opt_data_hk = hk_data
            session.opt_data_us = us_data
            # 向后兼容：opt_data_non_a 指向 us（邮件旧字段）
            session.opt_data_non_a = us_data or hk_data

            # 标的池全部来自 config：各组用各自 rules
            portfolio_results = {}
            opt_a = PortfolioOptimizer(config, custom_rules=a_rules,
                                       signal_fn=a_sfn, engine_params=a_ep)
            res_a = opt_a.run_fixed(groups=["a_share"])
            if res_a.get("a_share"):
                portfolio_results["a_share"] = res_a["a_share"]
            opt_hk = PortfolioOptimizer(config, custom_rules=hk_rules,
                                        signal_fn=hk_sfn, engine_params=hk_ep)
            res_hk = opt_hk.run_fixed(groups=["hk"])
            if res_hk.get("hk"):
                portfolio_results["hk"] = res_hk["hk"]
            opt_us = PortfolioOptimizer(config, custom_rules=us_rules,
                                        signal_fn=us_sfn, engine_params=us_ep)
            res_us = opt_us.run_fixed(groups=["us"])
            if res_us.get("us"):
                portfolio_results["us"] = res_us["us"]

            if portfolio_results:
                session.portfolio_results = portfolio_results
                logger.info("投资组合策略分析完成（config标的 / A股/港股/美股独立资金池）")
            else:
                logger.warning("无可用标的，跳过投资组合分析")
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

        # 策略信号扫描（和日报同一套 SignalScanner，保证信号一致）
        try:
            from src.analysis.signal_scanner import SignalScanner
            scanner = SignalScanner()
            scan_result = scanner.scan(session, "a_share", top_n=5)
            nona_result = scanner.scan(session, "non_a_share", top_n=5)
            merged_snapshot = dict(scan_result.indicator_snapshot or {})
            merged_snapshot.update(nona_result.indicator_snapshot or {})
            scan_result.indicator_snapshot = merged_snapshot
            scan_result.alerts = list(scan_result.alerts) + list(nona_result.alerts)
            session.signal_scan = scan_result
            logger.info(f"简报策略信号扫描完成: {len(scan_result.alerts)} 个策略告警")
        except Exception as e:
            logger.warning(f"简报策略信号扫描失败 (非致命): {e}")

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
            except Exception as _e:
                _hb_logger.warning(f"heartbeat send failed: {_e}")
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
    from src.analysis.portfolio_strategy import _detect_fine_group, get_skip_search
    from src.analysis.indicator_library import compute_all

    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("策略搜索优化器 V2 启动")
    logger.info("=" * 60)

    try:
        _run_optimize_v2_impl(config)
    except Exception as _fatal:
        logger.exception(f"V2 优化器致命错误: {_fatal}")
        import traceback
        traceback.print_exc()
        # 确保 traceback 写入 stderr（已重定向到日志文件）
        import sys
        sys.stderr.flush()


def _run_optimize_v2_impl(config):
    logger = logging.getLogger(__name__)
    import time
    from src.data.data_source import DataSource
    from src.analysis.strategy_optimizer_v2 import StrategyOptimizerV2
    from src.analysis.portfolio_strategy import _detect_fine_group, get_skip_search
    from src.analysis.indicator_library import compute_all

    stocks = config.get("stocks", [])
    if not stocks:
        logger.error("配置中没有股票列表")
        return

    # 跳过 skip_search 标的（仅盯盘，不参与搜参）
    skip = get_skip_search(config)
    if skip:
        logger.info(f"跳过搜参标的: {sorted(skip)}")

    # 三组分开搜参：A股/港股/美股（港美股走势差异大，混搜会互相干扰）
    a_codes: list[str] = []
    hk_codes: list[str] = []
    us_codes: list[str] = []
    for s in stocks:
        if isinstance(s, str):
            code = s
        elif isinstance(s, dict):
            code = s.get("code", "")
        else:
            code = str(s)
        if str(code) in skip:
            continue
        g = _detect_fine_group(code)
        if g == "a_share":
            a_codes.append(code)
        elif g == "hk":
            hk_codes.append(code)
        else:
            us_codes.append(code)

    logger.info(f"A股: {len(a_codes)} 只 | 港股: {len(hk_codes)} 只 | 美股: {len(us_codes)} 只")

    # 策略引擎选择（config.yaml optimizer.engine → 默认 global）
    engine_type = (config.get("optimizer", {}) or {}).get("engine", "global")
    engine = None
    signal_fn = None
    if engine_type in ("percentile", "pct", "new"):
        from src.analysis.percentile_engine import PercentileSignalFn
        from src.analysis.signal_fn_engine import SignalFnSearchEngine
        signal_fn = PercentileSignalFn()
        engine = SignalFnSearchEngine(signal_fn)
        logger.info("使用分位评分引擎 (PercentileSignalFn, 真实接入遗传搜索)")
    else:
        from src.analysis.global_threshold_signal import GlobalThresholdSignalFn
        signal_fn = GlobalThresholdSignalFn()
        logger.info("使用全局阈值引擎 (GlobalThresholdSignalFn [deprecated])")

    # 数据源
    logger.info("data_source init...")
    data_source = DataSource(config)
    lookback = config.get("portfolio_strategy", {}).get("lookback_days") or _eval_opt_lookback()
    logger.info(f"data_source OK, lookback={lookback}")

    heartbeat_stop = threading.Event()
    heartbeat_state = {
        "group": "", "group_n": 0, "total_groups": 3,
        "phase": "starting", "elapsed": 0, "active": True,
    }
    logger.info("starting heartbeat...")
    _start_heartbeat(config, heartbeat_stop, heartbeat_state)
    logger.info("heartbeat started")

    group_labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
    group_index = 0
    for group_name, codes in [("a_share", a_codes), ("hk", hk_codes), ("us", us_codes)]:
        if not codes:
            logger.info("跳过 %s: 无标的", group_name)
            continue

        logger.info("-" * 40)
        logger.info("开始 %s 策略优化 V2 (%d 只标的)", group_name, len(codes))
        heartbeat_state.update(group=group_labels.get(group_name, group_name),
                               group_n=group_index + 1, phase="Phase1")

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
            engine=engine,
            signal_fn=signal_fn,
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

        # ── 搜参完自动跑完整回测报告（含敏感性和波动率）──
        full_report = None
        try:
            full_report = _build_optimizer_report(config, group_name, codes, signal_fn)
        except Exception as e:
            logger.warning(f"回测报告生成失败 (非致命): {e}")

        notifier.send_optimizer_notification(report, group_name, full_report)
        group_index += 1
        heartbeat_state.update(phase="done", group_n=group_index)

    heartbeat_stop.set()
    logger.info("策略搜索 V2 完成")


def _build_optimizer_report(config, group_name, codes, signal_fn):
    """搜参完成后构建完整回测报告 dict。

    包含：日回报测核心指标 | 季末持仓 | 参数敏感性(10版最差) | 跨天波动率。

    Returns:
        {
            "group": "a_share", "label": "A股",
            "yaml_name": "...", "engine": "percentile",
            "total_return": 12.3, "excess_return": 4.6, "dd": -6.0,
            "sharpe": 0.73, "trades": 68, "position": 33,
            "sensitivity": { "worst_ret": 2.1, "drop_pct":", "ret_range": [1.2, 12.3] },
            "volatility": { "range": 8.7, "ret_min": -3.5, "ret_max": 5.2 },
            "quarterly": [...],
            "nav_series": [...], "nav_dates": [...],
            "params": {"tau_buy": 0.9, ...},
        }
        失败返回 None。
    """
    import numpy as np
    import yaml
    from pathlib import Path
    from src.analysis.portfolio_strategy import (
        PortfolioEvaluator, _detect_fine_group,
    )
    from src.analysis.signal_scanner import _params_from_yaml
    from src.analysis.rule_engine import Rule
    from src.analysis.execution_config import get_execution_config

    # 读最新 YAML
    opt_dir = Path("data/optimizer")
    files = sorted(
        [f for f in opt_dir.glob(f"*_{group_name}_strategies.yaml")],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not files:
        return None
    with open(files[0], "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    top = (data.get("strategies") or [{}])[0]
    params_dict = top.get("params", {})
    engine_name = params_dict.get("_engine", "global")
    if engine_name not in ("percentile", "pct", "new"):
        logger.info(f"[{group_name}] 非分位引擎，跳过完整报告")
        return None

    # 获取数据（用 441 天 = 252 预热 + 189 交易 ≈ 9 个月）
    from src.data.data_source import DataSource
    ds = DataSource(config)
    stocks_data = {}
    for code in codes:
        df = ds.fetch_stock_data(code, days=_eval_opt_lookback())
        if df is not None and not df.empty:
            stocks_data[str(code)] = df
    if not stocks_data:
        return None

    from src.analysis.percentile_engine import PercentileSignalFn
    sfn = PercentileSignalFn()
    ep = _params_from_yaml(params_dict)
    rules_raw = top.get("rules", [])
    rules = [Rule.from_dict(r) for r in rules_raw] if rules_raw else None
    fine_group = _detect_fine_group(str(codes[0]))
    eval_group = {"a_share": "a_share", "hk": "non_a_share",
                  "us": "non_a_share"}.get(fine_group, "non_a_share")
    exec_cfg = get_execution_config()
    ev = PortfolioEvaluator(stocks_data, eval_group, rules=rules,
                            signal_fn=sfn)
    ev._engine_params = ep
    res = ev.evaluate(list(stocks_data.keys()))

    # 日回报测核心指标
    label_map = {"a_share": "A股", "hk": "港股", "us": "美股"}
    report = {
        "group": group_name,
        "label": label_map.get(group_name, group_name),
        "yaml_name": files[0].name,
        "engine": engine_name,
        "total_return": round(res.total_return, 2),
        "dd": round(res.max_drawdown, 2),
        "sharpe": round(res.sharpe_ratio, 2),
        "trades": res.trade_count,
        "position": round(res.expected_position, 0),
        "quarterly": getattr(res, "quarterly_holdings", []) or [],
        "nav_series": getattr(res, "nav_series", []) or [],
        "nav_dates": getattr(res, "nav_dates", []) or [],
        "params": {},
    }
    # 解码参数为人读值
    from src.analysis.percentile_engine import _decode_tau, _decode_w, _decode_pos_frac
    vals = getattr(ep, "values", ep)
    for lbl in ("adx_pct", "rsi_pct", "deviation_pct", "vol_ratio_pct", "ma200_dev_pct"):
        report["params"][f"{lbl}_tau"] = round(_decode_tau(vals.get(f"{lbl}_tau", 5)), 2)
        report["params"][f"{lbl}_w"] = round(_decode_w(vals.get(f"{lbl}_w", 2)), 2)
    report["params"]["tau_buy"] = round(_decode_tau(vals.get("buy_score_thresh", 5)), 2)
    report["params"]["tau_sell"] = round(_decode_tau(vals.get("sell_score_thresh", 5)), 2)
    report["params"]["pos_frac"] = round(_decode_pos_frac(vals.get("position_frac", 2)), 2)

    # 超额：用 YAML test_return 作为搜参评估值
    report["excess_return"] = top.get("test_return")

    # ── 参数敏感性：随机扰动 10 版，限测试期数据 ──
    try:
        per_code_bs, per_code_ss, per_code_pr = {}, {}, {}
        from src.analysis.indicator_library import compute_all
        computed = compute_all(stocks_data)
        all_dates = set()
        for c in stocks_data:
            df = computed.get(c, stocks_data[c]).copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").reset_index(drop=True)
            nb, ns = sfn.score_timeseries(ep, df)
            close = df["close"].astype(float).values
            per_code_bs[c] = nb
            per_code_ss[c] = ns
            per_code_pr[c] = close
            all_dates.update(df["date"])
        dates_sorted = sorted(all_dates)
        didx = {d: i for i, d in enumerate(dates_sorted)}
        T = len(dates_sorted)
        N = len(stocks_data)
        buy_scores = np.zeros((T, N), dtype=np.float64)
        sell_scores = np.zeros((T, N), dtype=np.float64)
        price = np.full((T, N), np.nan)
        for j, c in enumerate(stocks_data):
            b_arr = per_code_bs.get(c, np.zeros(0))
            s_arr = per_code_ss.get(c, np.zeros(0))
            p_arr = per_code_pr.get(c, np.zeros(0))
            for k, d in enumerate(dates_sorted[:len(b_arr)]):
                if k < len(b_arr):
                    ti = didx.get(d, -1)
                    if ti >= 0 and ti < T:
                        buy_scores[ti, j] = b_arr[k]
                        sell_scores[ti, j] = s_arr[k]
                        price[ti, j] = p_arr[k]
        # 前向填充
        for j in range(N):
            last = np.nan
            for ti in range(T):
                if np.isnan(price[ti, j]):
                    price[ti, j] = last
                else:
                    last = price[ti, j]
        price = np.nan_to_num(price, nan=0.0)
        copies = sfn.random_perturbations(ep, n=10)
        pert_rets = []
        from src.analysis.signal_functions import simulate_portfolio
        from src.analysis.signal_functions import Params as _Params
        base_ex = sfn.execution_params(ep)
        base_tr = simulate_portfolio(
            buy_scores, sell_scores, price, exec_cfg.initial_capital,
            base_ex["buy_threshold"], base_ex["sell_threshold"],
            base_ex["position_frac"], 100, exec_cfg.monthly_buy_limit,
            exec_cfg.commission_rate,
            [""] * buy_scores.shape[0], [f"S{i}" for i in range(N)],
        )
        base_ret = base_tr.total_return_pct
        for cp in copies:
            cp_params = _Params(values=cp, _engine="percentile")
            cp_ex = sfn.execution_params(cp_params)
            tr = simulate_portfolio(
                buy_scores, sell_scores, price, exec_cfg.initial_capital,
                cp_ex["buy_threshold"], cp_ex["sell_threshold"],
                cp_ex["position_frac"], 100, exec_cfg.monthly_buy_limit,
                exec_cfg.commission_rate,
                [""] * buy_scores.shape[0], [f"S{i}" for i in range(N)],
            )
            pert_rets.append(round(tr.total_return_pct, 2))
        worst_ret = min(pert_rets) if pert_rets else base_ret
        report["sensitivity"] = {
            "worst_ret": worst_ret,
            "drop_pct": round(base_ret - worst_ret, 2),
            "base_ret": round(base_ret, 2),
            "ret_range": [round(min(pert_rets), 2), round(max(pert_rets), 2)],
        }
    except Exception as e:
        logger.warning(f"[{group_name}] 敏感性评估失败: {e}")
        report["sensitivity"] = None

    # ── 跨天波动率 ──
    try:
        vol_result = sfn.cross_day_volatility(
            ep, buy_scores, sell_scores, price,
            lookback_days=5,
            initial_cash=exec_cfg.initial_capital,
            monthly_limit=exec_cfg.monthly_buy_limit,
        )
        report["volatility"] = vol_result
    except Exception as e:
        logger.warning(f"[{group_name}] 跨天波动率失败: {e}")
        report["volatility"] = None

    # ── 周K蜡烛图 ──
    try:
        from src.notification.chart_generator import _build_weekly_ohlc
        ohlc = _build_weekly_ohlc(report["nav_series"], report["nav_dates"])
        report["weekly_ohlc"] = ohlc
    except Exception as e:
        logger.warning(f"[{group_name}] 周K图生成失败: {e}")
        report["weekly_ohlc"] = None

    return report


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


def _eval_opt_lookback() -> int:
    """读 optimizer_constraints.yaml 的 walk_forward.test_months × 21。"""
    try:
        import yaml
        with open("config/optimizer_constraints.yaml", "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        months = int((raw.get("walk_forward", {}) or {}).get("test_months", 9))
        return max(months * 21, 60)
    except Exception:
        return 9 * 21


def _start_heartbeat(config, stop_event, state: dict):
    """搜参过程每5分钟飞书心跳通知，含相位/组合/耗时的实时进度。"""
    import logging as _logging
    _hb_logger = _logging.getLogger(__name__)

    def _beat():
        import time as _time
        start = _time.time()
        count = 0
        phase_emoji = {"starting": "⏳", "Phase1": "🔍", "Phase2": "🧬", "done": "✅"}
        while not stop_event.is_set():
            _time.sleep(300)
            if stop_event.is_set():
                break
            count += 1
            elapsed = int(_time.time() - start)
            mins = elapsed // 60
            sec = elapsed % 60
            group = state.get("group", "—")
            phase = state.get("phase", "—")
            g_n = state.get("group_n", 0)
            g_tot = state.get("total_groups", 3)
            emoji = phase_emoji.get(phase, "⚙️")

            progress = f"第 {g_n}/{g_tot} 组 · {emoji} {phase}"
            title = f"Trade Eyes · 搜参运行中 ({mins}m{sec}s)"
            body_lines = [
                f"**{title}**",
                f"当前: **{group}** · {progress}",
                f"已连续运行 {mins} 分 {sec} 秒",
                "完成后自动推送完整报告",
            ]
            card = {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
                "body": {"elements": [
                    {"tag": "markdown", "content": "\n".join(body_lines)},
                ]},
            }
            try:
                import os as _os
                import requests as _requests
                webhook = _os.getenv("FEISHU_WEBHOOK_URL", "")
                if not webhook:
                    webhook = config.get("notification", {}).get("feishu", {}).get("webhook_url", "")
                if webhook:
                    _requests.post(webhook, json={
                        "msg_type": "interactive", "card": card,
                    }, timeout=10)
            except Exception as _e:
                _hb_logger.warning(f"heartbeat send failed: {_e}")

    threading.Thread(target=_beat, daemon=True, name="heartbeat").start()


def _send_restart_notification(config: dict):
    """服务重启后飞书通知。"""
    try:
        import os
        from datetime import datetime
        from pathlib import Path
        import json
        import requests

        webhook = os.getenv("FEISHU_WEBHOOK_URL", "")
        if not webhook:
            fc = config.get("notification", {}).get("feishu", {})
            webhook = fc.get("webhook_url", "")
        if not webhook:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        title = "Trade Eyes · 已上线"
        lines = [
            f"**{title}**",
            f"重启时间: {now}",
            f"状态: 定时任务已注册 (日报 19:00 / 简报 09:50 14:30 / 搜参 02:00)",
        ]
        try:
            root = Path(__file__).parent
            import subprocess
            commits = subprocess.run(
                ["git", "-C", str(root), "log", "-1", "--pretty=format:%h %s"],
                capture_output=True, text=True, timeout=5,
            )
            if commits.returncode == 0 and commits.stdout.strip():
                lines.append(f"版本: `{commits.stdout.strip()[:80]}`")
        except Exception:
            pass

        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "green"},
            "body": {"elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
            ]},
        }
        payload = {"msg_type": "interactive", "card": card}
        requests.post(webhook, json=payload, timeout=10)
    except Exception:
        pass


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
        # ── 服务重启通知 ──
        _send_restart_notification(config)


if __name__ == "__main__":
    main()
