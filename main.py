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
from src.analysis.strategies import get_strategy
from src.analysis.backtester import evaluate_all_groups
from src.analysis.config import get_execution_config
from src.analysis.helpers import _detect_fine_group
from src.core.ref_portfolio import RefPortfolioManager, REF_MONTHLY_LIMIT


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

        # 3b. 策略信号扫描（基于最新优化结果，直接调 PercentileSearchStrategy）
        try:
            logger.info("开始策略信号扫描")
            a_alerts = _scan_group(session, "a_share")
            hk_alerts = _scan_group(session, "hk")
            us_alerts = _scan_group(session, "us")

            for sa in a_alerts + hk_alerts + us_alerts:
                session.alerts.append(sa)

            # 存入信号扫描结果供邮件使用
            session.signal_scan = type("ScanResult", (), {
                "alerts": a_alerts + hk_alerts + us_alerts,
                "consensus": None,
                "indicator_snapshot": {},
                "divergence_warnings": [],
            })()

            logger.info(
                f"策略信号扫描完成: A股={len(a_alerts)} + "
                f"境外={len(hk_alerts) + len(us_alerts)} 个策略告警"
            )
        except Exception as e:
            logger.warning(f"策略信号扫描失败 (非致命): {e}")
            session.signal_scan = None

        # 4. 创建通知管理器（统一入口）
        notifier = NotifierManager(config)

        # 5. 投资组合策略分析（config.dashboard.strategy → evaluate_all_groups）
        try:
            logger.info("开始投资组合策略分析")

            strategy_name = config.get("dashboard", {}).get("strategy", "percentile")
            strategy = get_strategy(strategy_name)
            if strategy is None:
                strategy = get_strategy("percentile")
            params = strategy.random_params()
            exec_cfg = get_execution_config()

            # 全量历史数据（session._historical 已缓存数据拉取结果）
            stocks_data = {}
            for code in config["stocks"]:
                code_str = str(code)
                df = getattr(session, "_historical", {}).get(code_str)
                if df is not None and len(df) >= 60:
                    stocks_data[code_str] = df

            benchmark_data = _fetch_benchmarks(config, session)

            reports = evaluate_all_groups(
                stocks_data,
                [str(c) for c in config["stocks"]],
                strategy, params, exec_cfg,
                benchmark_data=benchmark_data,
            )

            if reports:
                object.__setattr__(session, "evaluation_reports", reports)
                object.__setattr__(session, "_yaml_eval_cache", {
                    gk: r.to_cache_dict() for gk, r in reports.items()
                })
                logger.info(
                    f"投资组合策略分析完成 ({len(reports)}组: "
                    + ", ".join(f"{k}:{v.total_return:+.1f}%"
                                for k, v in reports.items())
                    + ")"
                )
            else:
                logger.warning("无可用标的，跳过投资组合分析")
        except Exception as e:
            logger.error(f"投资组合策略分析失败: {e}", exc_info=True)

        # ── 参考持仓状态（只读，日报不做调仓，三分仓）──

        stock_data_df = session.get_all_dataframe()
        all_statuses = {}
        for group_key, label, file_name in [
            ("a_share", "A股", "data/ref_portfolio_a.yaml"),
            ("hk", "港股", "data/ref_portfolio_hk.yaml"),
            ("us", "美股", "data/ref_portfolio_us.yaml"),
        ]:
            mgr = RefPortfolioManager(file_path=file_name)
            pf = mgr.load()
            if not mgr.is_initialized(pf):
                continue
            fx = {"a_share": 1.0, "hk": 0.9, "us": 7.0}.get(group_key, 1.0)
            prices = {}
            for _, row in stock_data_df.iterrows():
                code = str(row.get("stock_code", ""))
                if _detect_fine_group(code) != group_key:
                    continue
                close = row.get("close")
                if code and close is not None and not pd.isna(close):
                    prices[code] = float(close) * fx
            status = mgr.get_status(pf, prices)
            status["_group"] = group_key
            status["_label"] = label
            all_statuses[group_key] = status
            logger.debug(
                f"参考持仓{label}已加载: 净值 {status['nav']:,.0f}, "
                f"回报 {status['nav_return_pct']:+.2f}%"
            )
        object.__setattr__(session, "ref_portfolio_status", all_statuses)

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

        # 策略信号扫描（直接调 PercentileSearchStrategy）
        try:
            a_alerts = _scan_group(session, "a_share")
            hk_alerts = _scan_group(session, "hk")
            us_alerts = _scan_group(session, "us")

            session.signal_scan = type("ScanResult", (), {
                "alerts": a_alerts + hk_alerts + us_alerts,
                "consensus": None,
                "indicator_snapshot": {},
                "divergence_warnings": [],
            })()
            logger.info(f"简报策略信号扫描完成: {len(session.signal_scan.alerts)} 个策略告警")
        except Exception as e:
            logger.warning(f"简报策略信号扫描失败 (非致命): {e}")

        # ── 参考持仓三分仓调仓（A股/港股/美股各自独立资金池）──

        exec_cfg = get_execution_config()
        alerts_all = session.signal_scan.alerts if session.signal_scan else []
        stock_data_df = session.get_all_dataframe()

        # 按分组拆分信号和现价
        POOLS = {
            "a_share": {
                "file": "data/ref_portfolio_a.yaml",
                "lot": exec_cfg.lot_sizes.get("a_share", 100),
                "fx": exec_cfg.fx_rates.get("a_share", 1.0),
                "label": "A股",
                "initial_capital": exec_cfg.initial_capital,
                "monthly_limit": REF_MONTHLY_LIMIT,
            },
            "hk": {
                "file": "data/ref_portfolio_hk.yaml",
                "lot": exec_cfg.lot_sizes.get("hk", 100),
                "fx": exec_cfg.fx_rates.get("hk", 0.9),
                "label": "港股",
                "initial_capital": exec_cfg.initial_capital,
                "monthly_limit": REF_MONTHLY_LIMIT,
            },
            "us": {
                "file": "data/ref_portfolio_us.yaml",
                "lot": exec_cfg.lot_sizes.get("us", 1),
                "fx": exec_cfg.fx_rates.get("us", 7.0),
                "label": "美股",
                "initial_capital": exec_cfg.initial_capital,
                "monthly_limit": REF_MONTHLY_LIMIT,
            },
        }

        all_statuses: dict[str, dict] = {}
        for group_key, cfg in POOLS.items():
            mgr = RefPortfolioManager(file_path=cfg["file"])
            pf = mgr.load()
            if not mgr.is_initialized(pf):
                logger.debug(f"参考持仓{cfg['label']} 未初始化，跳过")
                continue

            # 筛选本组信号
            group_alerts = [
                a for a in alerts_all
                if _detect_fine_group(str(getattr(a, "stock_code", ""))) == group_key
            ]

            # 构建本组现价表
            prices = {}
            for _, row in stock_data_df.iterrows():
                code = str(row.get("stock_code", ""))
                if _detect_fine_group(code) != group_key:
                    continue
                close = row.get("close")
                if code and close is not None and not pd.isna(close):
                    prices[code] = float(close)

            new_pf, trades = mgr.rebalance(
                pf, group_alerts, prices,
                today.strftime("%Y-%m-%d"),
                lot_size=cfg["lot"],
                commission_rate=exec_cfg.commission_rate,
                monthly_buy_limit=cfg["monthly_limit"],
                fx_rate=cfg["fx"],
                label=cfg["label"],
                force=force,
            )
            mgr.save(new_pf)

            # 本组 status（现价 × FX → CNY，与成本基准一致）
            cny_prices = {code: p * cfg["fx"] for code, p in prices.items()}
            status = mgr.get_status(new_pf, cny_prices)
            status["_group"] = group_key
            status["_label"] = cfg["label"]
            all_statuses[group_key] = status

        # 合并附到 session
        object.__setattr__(session, "ref_portfolio_status", all_statuses)

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



def _fetch_benchmarks(config, session) -> dict:
    """拉取基准 ETF 数据（510300/510880/VOO/BRK.B）。

    优先从 session._historical 读（已拉取过的历史数据）。
    """
    historical = getattr(session, "_historical", {}) or {}
    bench_codes_map = {
        "510300": 730,   # 沪深300
        "510880": 730,   # 红利ETF
        "VOO": 730,      # 标普500
        "BRK.B": 730,    # 伯克希尔
    }
    bench_data = {}

    for bc, days in bench_codes_map.items():
        if bc in historical:
            bench_data[bc] = historical[bc]
            continue
        try:
            df = StockDataFetcher(config).data_source.fetch_stock_data(
                bc, days=days
            )
            if df is not None and not df.empty:
                bench_data[bc] = df
                historical[bc] = df
        except Exception:
            pass
    return bench_data


def _scan_group(session, group: str, top_n: int = 5):
    """读最新搜参 YAML，调 PercentileSearchStrategy.scan_signals()，返回告警列表。"""
    from pathlib import Path
    from src.analysis.strategies import get_strategy
    strategy = get_strategy("percentile")


def _eval_opt_lookback() -> int:
    """读 optimizer_constraints.yaml 的 walk_forward.test_months，返回日历天数。

    fetch_stock_data(days=N) 的 N 是日历天，不是交易日。
    """
    try:
        import yaml
        with open("config/optimizer_constraints.yaml", "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        months = int((raw.get("walk_forward", {}) or {}).get("test_months", 9))
        return int(months * 30.4375)  # 9 个月 ≈ 274 日历天
    except Exception:
        return 274


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
