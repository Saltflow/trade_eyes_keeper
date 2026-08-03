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

import argparse
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
from time import monotonic
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
from src.strategy import get_strategy
from src.backtest import build_trade_plan, evaluate_all_groups
from src.search import get_constraints, get_execution_config
from src.markets import _detect_fine_group, get_skip_search, get_skip_signals
from src.core.ref_portfolio import (
    RefPortfolioManager,
    reference_execution_contract,
)
from src.core.process_lock import exclusive_process_lock
from src.search import run_optimizer
from src.strategy import Params
from src.search.artifacts import (
    activate_run,
    OptimizerGroupSummary,
    OptimizerRunSummary,
    load_latest_strategy_run,
    load_strategy_run,
    new_run_id,
    persist_group_summary,
    publish_complete_run,
)
from src.search.contracts import stable_hash
from src.instruments import InstrumentAuditService
from src.instruments.audit import load_latest_audit


OPTIMIZER_GROUPS = ("a_share", "hk", "us")
DEFAULT_OPTIMIZER_GROUPS = OPTIMIZER_GROUPS


def _stock_code(stock: object) -> str:
    """Normalize a configured stock entry to the code used by data sources."""
    if isinstance(stock, dict):
        return str(stock.get("code", "")).strip()
    return str(stock).strip()


def _configured_optimizer_groups(config: dict) -> tuple[str, ...]:
    """Return configured market groups that contain optimizer-eligible symbols."""
    skipped = get_skip_search(config)
    present = {
        _detect_fine_group(code)
        for stock in (config.get("stocks", []) or [])
        if (code := _stock_code(stock)) and code not in skipped
    }
    return tuple(group for group in OPTIMIZER_GROUPS if group in present)


def _get_configured_strategy(config: dict):
    """Return the registered strategy selected by the optimizer config."""
    name = (config.get("optimizer", {}) or {}).get("engine") or "percentile"
    return get_strategy(str(name))


def _optimizer_lookback_days(constraints) -> int:
    """Return enough calendar history for every configured walk-forward window."""
    wf = constraints.walk_forward
    # The final test window ends after train + test + all preceding steps.
    # Include a six-month calendar buffer for exchange holidays, date-set
    # intersections, and rolling indicators.  A 90-day buffer left the A-share
    # common calendar nine trading days short of the final full WF window.
    required_months = wf.total_months_needed
    configured_history_days = int(wf.data_years * 365.25) + 180
    return max(730, int(required_months * 30.4375) + 180, configured_history_days)


def _min_optimizer_history_rows(constraints) -> int:
    """Fast row-count preflight; window construction itself is calendar based."""
    return constraints.walk_forward.total_months_needed * 21


def _has_optimizer_history(data: pd.DataFrame, constraints) -> bool:
    """Return whether one symbol spans the full configured calendar horizon."""
    if data is None or data.empty:
        return False
    raw_dates = data["date"] if "date" in data.columns else data.index
    dates = pd.to_datetime(raw_dates, errors="coerce")
    dates = dates[~pd.isna(dates)]
    if len(dates) < 2:
        return False
    latest = pd.Timestamp(dates.max()).normalize() + pd.Timedelta(days=1)
    required_start = latest - pd.DateOffset(
        months=constraints.walk_forward.total_months_needed
    )
    return pd.Timestamp(dates.min()).normalize() <= required_start


def _optimizer_evaluation_budget(constraints) -> int:
    """Return the configured number of parameter evaluations per market."""
    solver_config = constraints.search.solver_config()
    if solver_config.get("budget") is not None:
        return max(1, int(solver_config["budget"]))
    ga = constraints.genetic_search
    return ga.phase1_random_samples + ga.num_generations * ga.offspring_size


def _optimizer_validation_start(constraints) -> str:
    """Legacy fallback when an artifact predates persisted WF boundaries."""
    months = max(1, int(constraints.walk_forward.test_months))
    return (pd.Timestamp.now().normalize() - pd.DateOffset(months=months)).strftime(
        "%Y-%m-%d"
    )


def _optimizer_validation_snapshot(report) -> dict[str, object]:
    """Serialize the portable parts of a validation report for notifications."""
    return {
        "total_return": float(report.total_return),
        "excess_return": float(report.excess_return),
        "max_drawdown": float(report.max_drawdown),
        "sharpe_ratio": float(report.sharpe_ratio),
        "trade_count": int(report.trade_count),
        "pending_order_count": int(
            getattr(report, "pending_order_count", 0)
        ),
        "avg_cash_pct": float(report.avg_cash_pct),
        "initial_asset": float(report.initial_asset),
        "final_asset": float(report.final_asset),
        "final_cash": float(report.final_cash),
        "final_holdings_value": float(report.final_holdings_value),
        "final_position_pct": float(report.final_position_pct),
        "final_holdings": list(report.final_holdings),
        "benchmark_returns": dict(report.benchmark_returns),
        "benchmark_win_rates": dict(report.benchmark_win_rates),
        "benchmark_excess_returns": dict(report.benchmark_excess_returns),
        "benchmark_details": dict(report.benchmark_details),
        "primary_benchmark": report.primary_benchmark,
        "composition": list(report.composition),
        "eligible_codes": list(getattr(report, "eligible_codes", []) or []),
        "warming_codes": list(getattr(report, "warming_codes", []) or []),
        "eligible_from": dict(getattr(report, "eligible_from", {}) or {}),
        "latest_holdings": list(report.final_holdings),
        "quarterly_holdings": list(report.quarterly_holdings),
        "weekly_ohlc": dict(report.weekly_nav_ohlc),
    }


def _load_optimizer_benchmarks(data_source, constraints, group: str, days: int) -> dict:
    """Fetch only configured tradable baselines for the optimizer validation."""
    result = {}
    codes = list(dict.fromkeys(constraints.benchmark_codes_for(group)))
    for code in codes:
        if code == "risk_free":
            continue
        try:
            data = data_source.fetch_stock_data(code, days=days)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Unable to load %s baseline for %s validation: %s", code, group, exc
            )
            continue
        if data is not None and not data.empty:
            result[code] = data
    return result


def rebuild_active_optimizer_summary(config: dict) -> OptimizerRunSummary | None:
    """Recreate a complete three-market optimizer report from the active run.

    This is used to repair a failed or legacy notification without launching a
    second expensive parameter search.  It evaluates the immutable active
    parameters over the configured validation horizon and persists the report
    metadata next to the run artifact for future resends.
    """
    active = load_latest_strategy_run(groups=_configured_optimizer_groups(config))
    if active is None or active.strategy is None:
        return None

    constraints = get_constraints()
    lookback_days = _optimizer_lookback_days(constraints)
    evaluation_budget = _optimizer_evaluation_budget(constraints)
    from src.data.data_source import DataSource

    data_source = DataSource(config)
    summaries: dict[str, OptimizerGroupSummary] = {}
    configured_codes = [_stock_code(stock) for stock in config.get("stocks", [])]

    for group in OPTIMIZER_GROUPS:
        artifact_path = (
            Path("data/optimizer")
            / "runs"
            / active.run_id
            / f"{group}_best_params.yaml"
        )
        try:
            artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            artifact = {}
        search = artifact.get("search", {}) if isinstance(artifact, dict) else {}
        period = artifact.get("validation_period", {}) if isinstance(artifact, dict) else {}
        validation_start = (
            str(period.get("start"))
            if isinstance(period, dict) and period.get("start")
            else _optimizer_validation_start(constraints)
        )
        validation_end = (
            str(period.get("end"))
            if isinstance(period, dict) and period.get("end")
            else None
        )
        survivors = int(
            search.get("survivor_count", constraints.genetic_search.population_size)
        )
        summary = OptimizerGroupSummary(
            group=group,
            candidate_count=survivors,
            evaluated_count=int(search.get("evaluated_count", evaluation_budget)),
            survivor_count=survivors,
            wf_score=(
                float(artifact["wf_score"])
                if isinstance(artifact, dict) and artifact.get("wf_score") is not None
                else None
            ),
            params=dict(active.params_by_group[group].values),
            execution=dict(active.strategy.execution_params(active.params_by_group[group])),
            ranking_window_count=int(search.get("ranking_window_count", 0)),
            validation_window_count=int(search.get("validation_window_count", 0)),
            purged_window_count=int(search.get("purged_overlap_window_count", 0)),
            ranking_diagnostics=(
                dict(search.get("ranking_diagnostics", {}))
                if isinstance(search.get("ranking_diagnostics"), dict)
                else {}
            ),
            sensitivity=(
                dict(artifact.get("sensitivity", {}))
                if isinstance(artifact, dict)
                and isinstance(artifact.get("sensitivity"), dict)
                else {}
            ),
            status="completed",
            artifact=f"{group}_best_params.yaml",
        )

        stocks_data = {}
        for code in configured_codes:
            if _detect_fine_group(code) != group:
                continue
            try:
                data = data_source.fetch_stock_data(code, days=lookback_days)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Unable to load %s for %s summary rebuild: %s", code, group, exc
                )
                continue
            if data is not None and len(data) >= 60:
                stocks_data[code] = data

        if stocks_data:
            reports = evaluate_all_groups(
                stocks_data,
                list(stocks_data),
                active.strategy,
                active.params_by_group[group],
                constraints.execution,
                benchmark_data=_load_optimizer_benchmarks(
                    data_source, constraints, group, lookback_days
                ),
                target_groups=[group],
                start_date=validation_start,
                end_date=validation_end,
            )
            validation = reports.get(group)
            if validation is not None:
                summary.validation = _optimizer_validation_snapshot(validation)

        if active.run_id:
            persist_group_summary(active.run_id, summary)
        summaries[group] = summary

    return OptimizerRunSummary(
        active.strategy_name,
        active.strategy.label,
        active.timestamp,
        0.0,
        summaries,
        activated=True,
    )


def run_optimization(
    config: dict,
    target_groups: tuple[str, ...] = DEFAULT_OPTIMIZER_GROUPS,
) -> dict[str, int]:
    """Run the configured strategy through the unified optimizer."""
    logger = logging.getLogger(__name__)
    strategy = _get_configured_strategy(config)
    if strategy is None:
        logger.error("No registered strategy is configured for optimization")
        return {}

    target_groups = tuple(group for group in target_groups if group in OPTIMIZER_GROUPS)
    if not target_groups:
        logger.error("No valid optimizer market group was requested")
        return {}

    started_at = datetime.now()
    started = monotonic()
    run_id = new_run_id(strategy.name, started_at)
    run_dir = Path("data/optimizer") / "runs" / run_id
    summaries = {
        group: OptimizerGroupSummary(group=group) for group in OPTIMIZER_GROUPS
    }

    configured_stocks = config.get("stocks", []) or []
    if not configured_stocks:
        logger.error("No stocks are configured for optimization")
        _notify_optimizer_run(
            config,
            OptimizerRunSummary(
                strategy.name,
                strategy.label,
                started_at.isoformat(),
                monotonic() - started,
                summaries,
            ),
        )
        return {}

    skipped = get_skip_search(config)
    groups: dict[str, list[str]] = {group: [] for group in target_groups}
    for stock in configured_stocks:
        code = _stock_code(stock)
        if not code or code in skipped:
            continue
        group = _detect_fine_group(code)
        if group in groups:
            groups[group].append(code)

    if skipped:
        logger.info("Optimization skips configured symbols: %s", sorted(skipped))

    required_groups = tuple(group for group, codes in groups.items() if codes)
    if not required_groups:
        logger.error("No optimizer-eligible market group has configured symbols")
        return {}

    constraints = get_constraints()
    lookback_days = _optimizer_lookback_days(constraints)
    evaluation_budget = _optimizer_evaluation_budget(constraints)
    logger.info(
        "Starting %s optimization: A=%d HK=%d US=%d, lookback=%d days",
        strategy.name,
        len(groups.get("a_share", [])),
        len(groups.get("hk", [])),
        len(groups.get("us", [])),
        lookback_days,
    )

    from src.data.data_source import DataSource

    data_source = DataSource(config)
    completed: dict[str, int] = {}
    for group, codes in groups.items():
        if not codes:
            logger.info("Skipping %s optimization: no eligible symbols", group)
            summaries[group].status = "no_symbols"
            continue

        stocks_data: dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                data = data_source.fetch_stock_data(code, days=lookback_days)
            except Exception as exc:
                logger.warning("Unable to load %s for %s optimization: %s", code, group, exc)
                continue
            if data is None or data.empty:
                continue
            if not _has_optimizer_history(data, constraints):
                logger.warning(
                    "Skipping %s from %s optimization: history does not cover "
                    "the configured %d-month walk-forward horizon",
                    code,
                    group,
                    constraints.walk_forward.total_months_needed,
                )
                continue
            stocks_data[code] = data

        if not stocks_data:
            logger.warning("Skipping %s optimization: no usable market data", group)
            summaries[group].status = "no_data"
            continue

        constraints.set_group(group)
        try:
            optimizer_benchmarks = _load_optimizer_benchmarks(
                data_source, constraints, group, lookback_days
            )
            results, _ = run_optimizer(
                strategy,
                stocks_data,
                list(stocks_data),
                group,
                _constraints=constraints,
                output_dir=run_dir,
                benchmark_data=optimizer_benchmarks,
            )
        except Exception:
            logger.exception("%s optimization failed", group)
            summaries[group].status = "failed"
            continue

        completed[group] = len(results)
        if results:
            params = Params(
                values=dict(results[0].parameters),
                _engine=strategy.name,
            )
            summaries[group] = OptimizerGroupSummary(
                group=group,
                candidate_count=len(results),
                evaluated_count=evaluation_budget,
                survivor_count=len(results),
                wf_score=float(results[0].objective_score),
                params=dict(params.values),
                execution=dict(strategy.execution_params(params)),
                ranking_window_count=len(getattr(results[0], "ranking_stats", [])),
                validation_window_count=len(
                    getattr(results[0], "validation_stats", [])
                ),
                purged_window_count=int(
                    getattr(results[0], "purged_window_count", 0)
                ),
                ranking_diagnostics=dict(
                    getattr(results[0], "ranking_metrics", {}) or {}
                ),
                sensitivity=dict(getattr(results[0], "sensitivity", {}) or {}),
                status="completed",
                artifact=f"{group}_best_params.yaml",
            )
            try:
                artifact_path = run_dir / f"{group}_best_params.yaml"
                artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8")) or {}
                activation = artifact.get("activation", {})
                if isinstance(activation, dict):
                    summaries[group].activation = dict(activation)
                period = artifact.get("validation_period", {})
                validation_start = (
                    str(period.get("start"))
                    if isinstance(period, dict) and period.get("start")
                    else _optimizer_validation_start(constraints)
                )
                validation_end = (
                    str(period.get("end"))
                    if isinstance(period, dict) and period.get("end")
                    else None
                )
                validation_reports = evaluate_all_groups(
                    stocks_data,
                    list(stocks_data),
                    strategy,
                    params,
                    constraints.execution,
                    benchmark_data=optimizer_benchmarks,
                    target_groups=[group],
                    start_date=validation_start,
                    end_date=validation_end,
                )
                validation = validation_reports.get(group)
                if validation is not None:
                    summaries[group].validation = _optimizer_validation_snapshot(
                        validation
                    )
            except Exception:
                logger.exception("%s optimizer validation report failed", group)
            persist_group_summary(run_id, summaries[group])
            logger.info(
                "%s optimization completed: %d configured evaluations, %d survivors, "
                "top WF score %.2f",
                group,
                evaluation_budget,
                len(results),
                results[0].objective_score,
            )
        else:
            logger.warning("%s optimization completed without valid candidates", group)
            summaries[group].status = "no_candidates"

    manual_activation = bool(getattr(strategy, "manual_activation", False))
    publication_groups = (
        OPTIMIZER_GROUPS if manual_activation else required_groups
    )
    published = publish_complete_run(
        run_id,
        strategy.name,
        started_at.isoformat(),
        summaries,
        required_groups=publication_groups,
        all_groups=publication_groups,
        activate=not manual_activation,
    )
    activated = bool(published and not manual_activation)
    if activated:
        logger.info("Published active optimizer run %s (%s)", run_id, strategy.name)
    elif published and manual_activation:
        logger.info(
            "Saved candidate optimizer run %s (%s); activate explicitly with "
            "python main.py --activate-run %s",
            run_id,
            strategy.name,
            run_id,
        )
    else:
        logger.warning(
            "Optimizer run %s is incomplete or ineligible; active alert "
            "strategy was not changed",
            run_id,
        )
    _notify_optimizer_run(
        config,
        OptimizerRunSummary(
            strategy.name,
            strategy.label,
            started_at.isoformat(),
            monotonic() - started,
            summaries,
            activated=activated,
            run_id=run_id,
            candidate=bool(published and manual_activation),
        ),
    )

    return completed


def _notify_optimizer_run(config: dict, report: OptimizerRunSummary) -> None:
    """Send one three-market optimizer summary through every notification channel."""
    try:
        NotifierManager(config).send_optimizer_notification(
            report, group_name=report.strategy_label
        )
    except Exception:
        logging.getLogger(__name__).exception("Unable to send optimizer summary")


def _get_active_strategy_and_params(config: dict):
    """Resolve alerts from the newest complete optimizer run, not config state."""
    active = load_latest_strategy_run(groups=_configured_optimizer_groups(config))
    if active and active.strategy is not None:
        return active.strategy, active.params_by_group, active.timestamp
    return _get_configured_strategy(config), {}, None

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
    # ``requests`` includes the full Telegram request URL in urllib3 DEBUG logs,
    # which would expose the bot token when application logging is DEBUG.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
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

        # Current instrument profiles are reporting-only. Loading the latest
        # completed audit avoids injecting current statements or current ETF
        # constituents into historical optimization.
        audit_config = config.get("instrument_audit", {}) or {}
        if audit_config.get("show_in_daily_report", True):
            audit = load_latest_audit(
                audit_config.get("output_dir", "data/instrument_audit")
            )
            if audit is not None:
                object.__setattr__(session, "instrument_audit", audit)
                logger.info(
                    "标的画像审计已加载: %s只",
                    len(audit.profiles),
                )

        # 3. 检查条件并存入Session
        logger.info("开始检查交易条件")
        checker = ConditionChecker(config)
        checker.check_from_session(session, session_manager)
        logger.info(f"条件检查完成: {len(session.alerts)}个警报")

        # 3b. 策略信号扫描
        try:
            logger.info("开始策略信号扫描")
            scan_strategy, scan_params, scan_timestamp = _get_active_strategy_and_params(config)
            if scan_timestamp:
                logger.info("Signal scan uses active optimizer run from %s", scan_timestamp)
            a_alerts = _scan_group(session, scan_strategy, "a_share", scan_params.get("a_share"))
            hk_alerts = _scan_group(session, scan_strategy, "hk", scan_params.get("hk"))
            us_alerts = _scan_group(session, scan_strategy, "us", scan_params.get("us"))

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

        # 5. 投资组合策略分析（参数唯一来源: data/optimizer/{group}_best_params.yaml）
        try:
            logger.info("开始投资组合策略分析")

            strategy, active_params, active_timestamp = _get_active_strategy_and_params(config)

            if strategy is None:
                logger.warning("未找到有效策略，跳过策略评估")
            else:
                exec_cfg = get_execution_config()
                stocks_data = {}
                for configured_stock in config.get("stocks", []):
                    code = _stock_code(configured_stock)
                    df = getattr(session, "_historical", {}).get(code)
                    if df is not None and len(df) >= 60:
                        stocks_data[code] = df

                benchmark_data = _fetch_benchmarks(config, session)
                reports = {}
                if active_timestamp:
                    logger.info("Portfolio evaluation uses active optimizer run from %s", active_timestamp)
                active_run = load_latest_strategy_run(
                    groups=_configured_optimizer_groups(config)
                )
                for group in OPTIMIZER_GROUPS:
                    params = active_params.get(group)
                    if params is None:
                        logger.warning(
                            "No %s parameters for %s; skip this group. "
                            "Run python main.py --optimize to generate them.",
                            strategy.name,
                            group,
                        )
                        continue
                    group_last_dates = []
                    for code, frame in stocks_data.items():
                        if _detect_fine_group(code) != group or frame.empty:
                            continue
                        raw_dates = frame["date"] if "date" in frame.columns else frame.index
                        parsed_dates = pd.to_datetime(raw_dates, errors="coerce")
                        if len(parsed_dates):
                            group_last_dates.append(pd.Timestamp(parsed_dates.max()))
                    validation_end = max(group_last_dates) if group_last_dates else None
                    validation_start = (
                        validation_end - pd.Timedelta(days=272)
                        if validation_end is not None
                        else None
                    )
                    group_reports = evaluate_all_groups(
                        stocks_data,
                        list(stocks_data),
                        strategy,
                        params,
                        exec_cfg,
                        benchmark_data=benchmark_data,
                        target_groups=[group],
                        start_date=(
                            validation_start.strftime("%Y-%m-%d")
                            if validation_start is not None
                            else None
                        ),
                        end_date=(
                            validation_end.strftime("%Y-%m-%d")
                            if validation_end is not None
                            else None
                        ),
                    )
                    for report in group_reports.values():
                        if active_run is not None:
                            report.selection_diagnostics = dict(
                                active_run.selection_by_group.get(group, {})
                            )
                    reports.update(group_reports)

                if reports:
                    object.__setattr__(session, "evaluation_reports", reports)
                    object.__setattr__(session, "_yaml_eval_cache", {
                        gk: report.to_cache_dict() for gk, report in reports.items()
                    })
                    logger.info(
                        f"投资组合策略分析完成 ({len(reports)}组: "
                        + ", ".join(
                            f"{group}:{report.total_return:+.1f}%"
                            for group, report in reports.items()
                        )
                        + ")"
                    )
                else:
                    logger.warning("无可用回测结果，跳过投资组合分析")
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

        # 策略信号扫描
        try:
            (
                brief_strategy,
                brief_params,
                brief_timestamp,
            ) = _get_active_strategy_and_params(config)
            if brief_timestamp:
                logger.info(
                    "Brief scan uses active optimizer run from %s",
                    brief_timestamp,
                )
            brief_skipped_signals = get_skip_signals(config)
            a_alerts = _scan_group(
                session,
                brief_strategy,
                "a_share",
                brief_params.get("a_share"),
                skip_codes=brief_skipped_signals,
            )
            hk_alerts = _scan_group(
                session,
                brief_strategy,
                "hk",
                brief_params.get("hk"),
                skip_codes=brief_skipped_signals,
            )
            us_alerts = _scan_group(
                session,
                brief_strategy,
                "us",
                brief_params.get("us"),
                skip_codes=brief_skipped_signals,
            )

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
        stock_data_df = session.get_all_dataframe()
        historical = getattr(session, "_historical", {}) or {}
        skipped_signals = get_skip_signals(config)

        POOLS = {
            "a_share": {
                "file": "data/ref_portfolio_a.yaml",
                "lot": exec_cfg.lot_sizes.get("a_share", 100),
                "fx": exec_cfg.fx_rates.get("a_share", 1.0),
                "label": "A股",
            },
            "hk": {
                "file": "data/ref_portfolio_hk.yaml",
                "lot": exec_cfg.lot_sizes.get("hk", 100),
                "fx": exec_cfg.fx_rates.get("hk", 0.9),
                "label": "港股",
            },
            "us": {
                "file": "data/ref_portfolio_us.yaml",
                "lot": exec_cfg.lot_sizes.get("us", 1),
                "fx": exec_cfg.fx_rates.get("us", 7.0),
                "label": "美股",
            },
        }

        all_statuses: dict[str, dict] = {}
        for group_key, pool in POOLS.items():
            mgr = RefPortfolioManager(file_path=pool["file"])
            pf = mgr.load()
            if not mgr.is_initialized(pf):
                logger.debug(f"参考持仓{pool['label']} 未初始化，跳过")
                continue

            prices = {}
            for _, row in stock_data_df.iterrows():
                code = str(row.get("stock_code", ""))
                if _detect_fine_group(code) != group_key:
                    continue
                close = row.get("close")
                if code and close is not None and not pd.isna(close):
                    prices[code] = float(close)

            new_pf = pf
            trading_blocked_reason = ""
            if not pf.is_bound:
                trading_blocked_reason = "未绑定运行，需手动重置"
                logger.warning(
                    "参考持仓%s 是旧格式且未绑定运行；跳过交易，等待手动 /ref_date 重置",
                    pool["label"],
                )
            else:
                pinned = load_strategy_run(pf.strategy_run_id, groups=(group_key,))
                params = (
                    pinned.params_by_group.get(group_key)
                    if pinned is not None
                    else None
                )
                binding_valid = bool(
                    pinned is not None
                    and pinned.strategy is not None
                    and pinned.strategy_name == pf.strategy_id
                    and params is not None
                    and stable_hash(
                        {
                            "strategy_id": pinned.strategy_name,
                            "values": params.values,
                        }
                    )
                    == pf.params_hash
                    and stable_hash(
                        reference_execution_contract(
                            params.execution_snapshot,
                            exec_cfg,
                            group_key,
                        )
                    )
                    == pf.execution_hash
                )
                if not binding_valid:
                    trading_blocked_reason = "固定运行或执行合同不可恢复"
                    logger.error(
                        "参考持仓%s 固定运行或合同不可恢复；跳过交易，"
                        "需手动 /ref_date 重置",
                        pool["label"],
                    )
                else:
                    active_codes = sorted(
                        {
                            str(row.get("stock_code", ""))
                            for _, row in stock_data_df.iterrows()
                            if _detect_fine_group(
                                str(row.get("stock_code", ""))
                            )
                            == group_key
                            and str(row.get("stock_code", "")) not in skipped_signals
                            and str(row.get("stock_code", "")) in historical
                        }
                    )
                    if active_codes:
                        try:
                            trade_plan, market_data, _, _ = build_trade_plan(
                                historical,
                                active_codes,
                                pinned.strategy,
                                params,
                                start_date=pf.inception_date,
                                end_date=today.strftime("%Y-%m-%d"),
                            )
                            if trade_plan is not None and market_data is not None:
                                new_pf, _ = mgr.rebalance_plan(
                                    pf,
                                    trade_plan,
                                    market_data,
                                    today.strftime("%Y-%m-%d"),
                                    run_id=pinned.run_id,
                                    strategy_id=pinned.strategy_name,
                                    lot_size=pool["lot"],
                                    commission_rate=exec_cfg.commission_rate,
                                    min_holding_days=exec_cfg.min_holding_days,
                                    fx_rate=pool["fx"],
                                    label=pool["label"],
                                    force=force,
                                )
                                mgr.save(new_pf)
                        except Exception:
                            trading_blocked_reason = "统一计划构建或执行失败"
                            logger.exception(
                                "参考持仓%s 统一计划构建或执行失败；保留原状态",
                                pool["label"],
                            )
                    else:
                        trading_blocked_reason = "没有完整历史数据"
                        logger.warning(
                            "参考持仓%s 没有可构建统一计划的完整历史数据",
                            pool["label"],
                        )

            # 本组 status（现价 × FX → CNY，与成本基准一致）
            cny_prices = {code: p * pool["fx"] for code, p in prices.items()}
            status = mgr.get_status(new_pf, cny_prices)
            status["_group"] = group_key
            status["_label"] = (
                f"{pool['label']}（停单）"
                if trading_blocked_reason
                else pool["label"]
            )
            status["_trading_blocked_reason"] = trading_blocked_reason
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


def _scan_group(
    session,
    strategy,
    group: str,
    params=None,
    top_n: int = 5,
    skip_codes: set[str] | None = None,
):
    """Derive today's alerts from the canonical full-market TradePlan."""
    if strategy is None:
        return []
    if params is None:
        active = load_latest_strategy_run(groups=(group,))
        params = (
            active.params_by_group.get(group)
            if active is not None and active.strategy_name == strategy.name
            else None
        )
    if params is None:
        return []
    df = session.get_all_dataframe()
    historical = getattr(session, "_historical", {}) or {}
    skipped = skip_codes or set()
    active_codes = sorted(
        {
            str(row.get("stock_code", ""))
            for _, row in df.iterrows()
            if _detect_fine_group(str(row.get("stock_code", ""))) == group
            and str(row.get("stock_code", "")) not in skipped
            and str(row.get("stock_code", "")) in historical
        }
    )
    if not active_codes:
        return []
    try:
        trade_plan, _, _, _ = build_trade_plan(
            historical,
            active_codes,
            strategy,
            params,
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Unable to build %s live TradePlan",
            group,
        )
        return []
    if trade_plan is None or not trade_plan.dates:
        return []
    row = len(trade_plan.dates) - 1
    buys = np.asarray(
        trade_plan.entry_events
        if trade_plan.entry_events is not None
        else trade_plan.buy_signals,
        dtype=bool,
    )
    sells = np.asarray(
        trade_plan.exit_events
        if trade_plan.exit_events is not None
        else trade_plan.sell_signals,
        dtype=bool,
    )
    if trade_plan.force_exit_signals is not None:
        sells |= np.asarray(trade_plan.force_exit_signals, dtype=bool)
    buy_priority = np.asarray(trade_plan.buy_priority, dtype=float)
    sell_priority = np.asarray(trade_plan.sell_priority, dtype=float)
    alerts = []
    signal_date = str(trade_plan.dates[row])[:10]
    for column, code in enumerate(trade_plan.symbols):
        for side, matrix, priorities in (
            ("buy", buys, buy_priority),
            ("sell", sells, sell_priority),
        ):
            if not matrix[row, column]:
                continue
            alerts.append(
                {
                    "stock_code": code,
                    "side": side,
                    "priority": float(priorities[row, column]),
                    "label": f"{strategy.label} {side.upper()}",
                    "detail": f"TradePlan event on {signal_date}",
                    "rule_id": f"{strategy.name}_{side}",
                    "type": f"strategy_{side}",
                    "signal_date": signal_date,
                }
            )
    # Live alerts use the same priority semantics as simulated orders; source
    # configuration order must never decide which candidate is surfaced first.
    alerts.sort(
        key=lambda item: (
            -float(item.get("priority", 0.0)),
            str(item.get("stock_code", "")),
            str(item.get("side", "")),
        )
    )
    return alerts[:top_n]


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


def _build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI from the same registered strategy keys used by bots."""
    parser = argparse.ArgumentParser(description="Stock quantitative system")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run the daily report once")
    mode.add_argument(
        "--brief",
        nargs="?",
        const="morning_snapshot",
        metavar="REPORT_ID",
        help="run a brief report (default: morning_snapshot)",
    )
    mode.add_argument(
        "--optimize", action="store_true", help="run the configured strategy optimizer"
    )
    mode.add_argument(
        "--activate-run",
        metavar="RUN_ID",
        help="atomically activate a complete holdout-passed candidate run",
    )
    mode.add_argument(
        "--audit-instruments",
        action="store_true",
        help="audit typed company/fund/REIT profiles without running strategies",
    )
    mode.add_argument("--health-server", action="store_true", help="start health server")
    mode.add_argument("--interactive", action="store_true", help="start Telegram bot")
    return parser


def main(argv: list[str] | None = None):
    """Application entry point."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    if (
        argv is None
        and args.optimize
        and os.environ.get("OPTIMIZER_GUARD_CHILD") != "1"
    ):
        environment = dict(os.environ)
        os.execve(
            sys.executable,
            [sys.executable, "-m", "src.optimizer_guard"],
            environment,
        )
    config = load_config()
    logger = setup_logging(config)
    if args.once:
        logger.info("Single daily run")
        run_daily_task()
    elif args.brief is not None:
        logger.info("Brief run: %s", args.brief)
        run_brief_report(args.brief)
    elif args.optimize:
        lock_path = Path("data/optimizer/.optimize.lock")
        with exclusive_process_lock(lock_path) as acquired:
            if not acquired:
                logger.error(
                    "Another optimizer process already holds %s; refusing overlap",
                    lock_path,
                )
                return
            completed = run_optimization(config, target_groups=OPTIMIZER_GROUPS)
            logger.info("Optimization finished: %s", completed or "no group completed")
    elif args.activate_run:
        if activate_run(args.activate_run, groups=OPTIMIZER_GROUPS):
            logger.info("Activated optimizer candidate: %s", args.activate_run)
        else:
            logger.error(
                "Candidate activation rejected; current active strategy is unchanged"
            )
    elif args.audit_instruments:
        report = InstrumentAuditService(config).run()
        outputs = report.summary.get("output_files", {})
        logger.info(
            "Instrument audit complete: %s instruments, fill %.1f%%, HTML=%s",
            len(report.profiles),
            float(report.summary.get("fill_rate", 0.0)) * 100.0,
            outputs.get("html", ""),
        )
    elif args.health_server:
        from src.health_server import start_health_server

        start_health_server()
    elif args.interactive:
        from src.interactive.telegram_bot import TelegramBot

        TelegramBot(config).run()
    else:
        logger.info("Starting scheduler")
        scheduler = SchedulerManager(
            config, task_function=run_daily_task, brief_function=run_brief_report
        )
        scheduler.start()
        _send_restart_notification(config)


if __name__ == "__main__":
    main()
