#!/usr/bin/env python3
"""
股票量化系统主程序
功能：
1. 获取自选股票天级别交易数据
2. 生成完整日报与简报，通过配置渠道投递
3. 查询公司行为并更新参考持仓估值
4. 按显式请求执行数据维护
"""

from __future__ import annotations

import argparse
import os
import sys

if __name__ == "__main__" and any(
    flag in sys.argv[1:] for flag in ("--once", "--brief")
) and "--report-worker" not in sys.argv[1:]:
    from src.core.report_cli import main as report_cli_main

    raise SystemExit(report_cli_main(sys.argv[1:]))

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "config", ".env"))
# 添加src目录到Python路径
logger = logging.getLogger(__name__)
sys.path.insert(0, str(Path(__file__).parent / "src"))
from src.core.config_store import load_config as load_runtime_config
from src.core.data_fetcher import StockDataFetcher
from src.core.ref_portfolio import (
    RefPortfolio,
    RefPortfolioManager,
    reference_valuation_price,
)
from src.core.report_quotes import prepare_report_quotes
from src.data.announcement_fetcher import AnnouncementFetcher
from src.instruments import InstrumentAuditService
from src.instruments.audit import load_latest_audit
from src.notification.manager import NotifierManager
from src.search.config import get_market_optimizer_configs
from src.session.session_manager import SessionManager
from src.strategy.return_curves import load_return_curves

MARKET_GROUPS = ("a_share", "hk", "us")


def _attach_return_curves(session, config):
    object.__setattr__(session, "strategy_return_curves", load_return_curves(config))


def _stock_code(stock: object) -> str:
    """Normalize a configured stock entry to the code used by data sources."""
    if isinstance(stock, dict):
        return str(stock.get("code", "")).strip()
    return str(stock).strip()


def _backtest_lookback_days(constraints) -> int:
    """Return enough calendar history for every configured walk-forward window."""
    wf = constraints.walk_forward
    # The final test window ends after train + test + all preceding steps.
    # Include a six-month calendar buffer for exchange holidays, date-set
    # intersections, and rolling indicators.  A 90-day buffer left the A-share
    # common calendar nine trading days short of the final full WF window.
    required_months = wf.total_months_needed
    configured_history_days = int(wf.data_years * 365.25) + 180
    return max(730, int(required_months * 30.4375) + 180, configured_history_days)


def _load_backtest_market_bundles(
    config: dict,
    codes: list[str],
    days: int,
    *,
    label: str,
) -> dict[str, object]:
    """Load the strict raw/qfq/action contract for an explicit backtest universe.

    Backtest input is deliberately not allowed to fall back to the legacy
    adjusted-only cache.  A missing bundle or insufficient calendar coverage
    makes this group fail closed so incomplete data cannot enter a backtest.
    """
    bundles, errors = _load_backtest_market_bundles_with_errors(
        config, codes, days
    )
    if errors:
        raise RuntimeError(
            f"{label} point-in-time raw/qfq coverage validation failed: "
            + "; ".join(errors)
        )
    return bundles


def _load_backtest_market_bundles_with_errors(
    config: dict,
    codes: list[str],
    days: int,
) -> tuple[dict[str, object], list[str]]:
    """Load bundles and retain per-symbol failures for explicit backtest readiness."""
    from src.backtest.execution import build_corporate_action_schedule
    from src.data.in_kind_estimation import validate_cached_in_kind_estimates
    from src.data.market_history import (
        PointInTimeMarketStore,
        corporate_action_issues,
    )

    settings = config.get("point_in_time_data", {}) or {}
    root = settings.get("output_dir", "data/point_in_time")
    store = PointInTimeMarketStore(root)
    today = pd.Timestamp.now().normalize()
    requested_start = today - pd.Timedelta(days=max(1, int(days)))
    bundles: dict[str, object] = {}
    errors: list[str] = []
    for code in dict.fromkeys(str(item) for item in codes):
        try:
            bundle = store.read(code, config=config)
            if bundle is None:
                raise ValueError("bundle is absent")
            validate_cached_in_kind_estimates(bundle, config)
            frame = bundle.prices
            dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
            if dates.empty:
                raise ValueError("bundle has no valid dates")
            if dates.min().normalize() > requested_start + pd.Timedelta(days=31):
                raise ValueError(
                    f"coverage starts at {dates.min().date()}, requested {requested_start.date()}"
                )
            if dates.max().normalize() < today - pd.Timedelta(days=14):
                raise ValueError(
                    f"coverage ends at {dates.max().date()}, current data is stale"
                )
            for column in (
                "raw_open", "raw_high", "raw_low", "raw_close",
                "qfq_open", "qfq_high", "qfq_low", "qfq_close",
                "qfq_factor",
            ):
                if pd.to_numeric(frame[column], errors="coerce").isna().any():
                    raise ValueError(f"column {column} contains missing values")
            action_issues = corporate_action_issues(bundle.actions)
            if action_issues:
                raise ValueError("; ".join(action_issues))
            build_corporate_action_schedule(
                bundle.actions,
                frame["date"],
                [code],
                external_prices=bundle.external_prices,
            )
            bundles[code] = bundle
        except Exception as exc:  # noqa: BLE001 - Preserve per-symbol readiness errors.
            errors.append(f"{code}: {exc}")
    return bundles, errors


def _load_backtest_benchmark_bundles(
    config: dict,
    constraints,
    group: str,
    days: int,
) -> dict[str, object]:
    codes = [
        code
        for code in dict.fromkeys(constraints.benchmark_codes_for(group))
        if code != "risk_free"
    ]
    return _load_backtest_market_bundles(
        config,
        codes,
        days,
        label=f"{group} benchmark",
    )


def _reference_portfolio_prices(
    portfolio: RefPortfolio,
    stock_data: pd.DataFrame,
    fx_rate: float,
    fetch_quote: Callable[[str], dict | None],
    *,
    external_fx_rates: dict[str, float] | None = None,
) -> dict[str, float]:
    """Value every held symbol, including those removed from the watchlist.

    Session prices and supplemental provider quotes are in native currency.
    Only complete, positive quotes are converted to the account's CNY basis.
    A failed lookup stays absent so the status reports incomplete valuation.
    This function never changes holdings or the configured signal universe.
    """
    fx = reference_valuation_price(fx_rate)
    if fx is None:
        raise ValueError("reference portfolio FX rate must be finite and positive")
    prices: dict[str, float] = {}
    external_codes = {
        position["asset_code"]
        for position in getattr(portfolio, "in_kind_positions", {}).values()
        if position.get("quantity", 0) + position.get("fraction", 0) > 0
    }

    def quote_fx(code):
        if code not in external_codes:
            return fx
        from src.instruments.classifier import detect_market

        market = detect_market(code)
        if external_fx_rates is not None:
            return reference_valuation_price(external_fx_rates.get(market))
        if market == portfolio.market_group:
            return fx
        return None

    valued_codes = set(portfolio.holdings)
    valued_codes.update(
        position["asset_code"]
        for position in getattr(portfolio, "in_kind_positions", {}).values()
        if position.get("quantity", 0) + position.get("fraction", 0) > 0
    )
    for _, row in stock_data.iterrows():
        code = str(row.get("stock_code", ""))
        if code not in valued_codes:
            continue
        price = reference_valuation_price(row.get("close"))
        conversion = quote_fx(code)
        if price is not None and conversion is not None:
            prices[code] = price * conversion
    for code in portfolio.missing_price_codes(prices):
        conversion = quote_fx(code)
        if conversion is None:
            logger.warning("参考持仓 %s 缺少外部标的币种换汇合同，估值不完整", code)
            continue
        try:
            quote = fetch_quote(code)
            price = reference_valuation_price((quote or {}).get("close"))
            if price is not None:
                prices[code] = price * conversion
            else:
                logger.warning("参考持仓 %s 无有效补充行情，估值不完整", code)
        # An unavailable quote leaves valuation partial.
        except Exception as exc:  # noqa: BLE001
            logger.warning("参考持仓 %s 补充行情失败，估值不完整: %s", code, exc)
    return prices


def _reference_portfolio_actions(config: dict, portfolio) -> list[object]:
    """Load the exact stored corporate actions for every live holding.

    A reference account may not estimate a dividend from adjusted prices.  It
    either has a point-in-time action record for every holding or its next
    rebalance is blocked with an explicit reason.
    """
    from src.data.in_kind_estimation import validate_cached_in_kind_estimates
    from src.data.market_history import PointInTimeMarketStore

    settings = config.get("point_in_time_data", {}) or {}
    store = PointInTimeMarketStore(settings.get("output_dir", "data/point_in_time"))
    actions: list[object] = []
    missing: list[str] = []
    parent_codes = set(portfolio.holdings)
    parent_codes.update(
        identity.split(":")[1]
        for identity in getattr(portfolio, "in_kind_positions", {})
        if identity.startswith("in_kind:") and len(identity.split(":")) >= 4
    )
    for code in sorted(parent_codes):
        bundle = store.read(code, config=config)
        if bundle is None:
            missing.append(code)
            continue
        validate_cached_in_kind_estimates(bundle, config)
        actions.extend(bundle.actions)
    if missing:
        raise ValueError("missing point-in-time corporate-action bundle: " + ", ".join(missing))
    return actions


def _reference_portfolio_external_prices(config: dict, portfolio) -> dict:
    """Retain external quotes for pending rights after the parent is sold."""
    from src.backtest.in_kind import merge_in_kind_prices
    from src.data.market_history import PointInTimeMarketStore

    settings = config.get("point_in_time_data", {}) or {}
    store = PointInTimeMarketStore(settings.get("output_dir", "data/point_in_time"))
    codes = set(portfolio.holdings)
    codes.update(
        identity.split(":")[1]
        for identity in getattr(portfolio, "in_kind_positions", {})
        if identity.startswith("in_kind:") and len(identity.split(":")) >= 4
    )
    external = {}
    for code in sorted(codes):
        bundle = store.read(code, config=config)
        if bundle is not None:
            external = merge_in_kind_prices(external, bundle.external_prices)
    return external


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

    def suppress_pdfminer_debug(record: logging.LogRecord) -> bool:
        return not (
            record.levelno == logging.DEBUG
            and record.name.startswith("pdfminer")
        )

    # 配置文件处理器
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging_level)
    file_handler.addFilter(suppress_pdfminer_debug)
    file_formatter = logging.Formatter(log_format)
    file_handler.setFormatter(file_formatter)
    # 配置控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging_level)
    console_handler.addFilter(suppress_pdfminer_debug)
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
    """Load runtime configuration through the shared raw/environment boundary."""
    try:
        return load_runtime_config(config_path)
    except Exception as exc:
        logger.error("Unable to load configuration: %s", exc)
        raise SystemExit(1) from exc


def _report_result(status: str, reason: str, session=None, *, deliveries=None):
    """Keep the business entrypoints independent from the execution worker."""
    from src.core.execution import RunResult

    artifacts = (
        {"session_id": session.session_id, "data_readiness": _report_data_readiness(session)}
        if session is not None else {}
    )
    return RunResult(
        status=status, reason=reason, artifacts=artifacts, deliveries=deliveries or {}
    )


def _report_data_readiness(session):
    from src.notification.report_builders import report_data_readiness

    return report_data_readiness(session)


def _report_delivery_result(delivery, session, skip_reason: str = ""):
    if delivery is None and skip_reason:
        return _report_result("skipped", skip_reason, session)
    if not isinstance(delivery, dict):
        return _report_result("unknown", "notification_receipt_missing", session)
    status, reason = delivery.get("status", "unknown"), delivery.get("reason", "")
    readiness = _report_data_readiness(session)
    if readiness["status"] == "not_ready":
        if status == "succeeded":
            counts = readiness.get("quote_counts", {})
            usable = sum(counts.get(key, 0) for key in ("realtime", "delayed", "last_close"))
            status = "failed" if counts and not usable else "partial"
        reason = "data_not_ready" + (":" + reason if reason else "")
    return _report_result(
        status,
        reason,
        session,
        deliveries=delivery.get("deliveries", {}),
    )


def run_daily_task(
    force: bool = False, *, config: dict | None = None, progress: Callable | None = None
):
    """每日运行的任务

    Args:
        force: True = 手动触发，跳过周末/休市检查
    """
    if not force:
        force = os.getenv("BOT_FORCE") == "1"
    logger = logging.getLogger(__name__)
    logger.info("开始执行每日任务")
    # 加载配置
    config = config if config is not None else load_config()
    today = datetime.now().astimezone().date()

    # 周末跳过（仅定时）
    if not force and today.weekday() >= 5:
        logger.info(f"今天是周末 ({today})，跳过日报")
        return _report_result("skipped", "weekend")

    try:
        if progress:
            progress("daily_session")
        # 创建Session（新数据流）
        session_manager = SessionManager(config)
        session = session_manager.create_session(config)
        logger.info(f"Session创建成功: {session.session_id}")

        # 1. 获取公告信息（可选） - 提前获取以填充股息数据缓存
        announcement_config = config.get("announcements", {})
        from src.triggers.report_context import (
            is_managed_daily,
            populate_trigger_announcements,
        )

        managed_daily = is_managed_daily(config)
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
            # Announcements are optional report context.
            except Exception as e:  # noqa: BLE001
                logger.error(f"获取公告信息失败: {e}")
                session.errors.append(f"获取公告信息失败: {e}")

        # Official evidence captured by the triggering event stays visible even
        # when the optional recent-announcement feed does not return that row.
        if managed_daily:
            populate_trigger_announcements(session, config)

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
                # One placement provider may fail.
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"定增数据获取失败 {code_str}: {e}")
            session.placements = placements
            logger.info(f"定增数据获取完成，{len(placements)} 只标的有未解禁定增")
        # Optional placement context stays nonfatal.
        except Exception as e:  # noqa: BLE001
            logger.warning(f"定增数据获取失败 (非致命): {e}")

        # 2. 获取股票数据并存入Session
        if progress:
            progress("daily_stock_data")
        logger.info("开始获取股票数据")
        fetcher = StockDataFetcher(config)
        fetcher.fetch_to_session(session, session_manager)
        logger.info(f"股票数据获取完成: {len(session.stocks_data)}只股票")

        # 日报事件层只读取明确的除权/派息日期，不改变回测或交易数据合同。
        try:
            from src.data.dividend_events import DailyDividendEventResolver

            resolver = DailyDividendEventResolver(config)
            session.dividend_schedule = resolver.public_schedule(
                session.announcements,
                session.get_all_dataframe(),
            )
            session.dividend_events = resolver.pending_events(session.dividend_schedule)
            logger.info("日报待处理分红/分派: %s 条", len(session.dividend_events))
        # Optional event context stays nonfatal.
        except Exception as exc:  # noqa: BLE001
            logger.warning("日报分红/分派事件准备失败（非致命）: %s", exc)

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

        if progress:
            progress("daily_evaluation")
        _attach_return_curves(session, config)

        # Freeze raw source-timed report prices after slow history/event preparation.
        if progress:
            progress("daily_report_quotes")
        valuation_quote = prepare_report_quotes(
            session, fetcher.web_crawler.fetch_realtime_quote
        )

        # 4. 创建通知管理器（统一入口）
        notifier = NotifierManager(config)

        # ── 参考持仓状态（只读，日报不做调仓，三分仓）──

        stock_data_df = session.get_all_dataframe()
        try:
            reference_market_configs = get_market_optimizer_configs(
                config, groups=MARKET_GROUPS
            )
        except ValueError as exc:
            logger.error("参考持仓市场执行合同无效，跳过持仓估值: %s", exc)
            reference_market_configs = {}
        all_statuses = {}
        for group_key, label, file_name in [
            ("a_share", "A股", "data/ref_portfolio_a.yaml"),
            ("hk", "港股", "data/ref_portfolio_hk.yaml"),
            ("us", "美股", "data/ref_portfolio_us.yaml"),
        ]:
            market_config = reference_market_configs.get(group_key)
            if market_config is None:
                continue
            mgr = RefPortfolioManager(file_path=file_name)
            pf = mgr.load()
            if not mgr.is_initialized(pf):
                continue
            try:
                pf, _corporate_action_trades = mgr.apply_corporate_actions(
                    pf,
                    _reference_portfolio_actions(config, pf),
                    today.strftime("%Y-%m-%d"),
                    withholding_rate=market_config.execution.withholding_rates.get(
                        group_key, 0.0
                    ),
                    fx_rate=market_config.execution.fx_rates.get(group_key, 1.0),
                    external_prices=_reference_portfolio_external_prices(config, pf),
                )
                mgr.save(pf)
            # Failed corporate actions keep old holdings.
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "参考持仓%s 公司行为未应用，日报仅展示原状态: %s",
                    label,
                    exc,
                )
            fx = float(
                market_config.execution.fx_rates.get(group_key, 1.0)
            )
            prices = _reference_portfolio_prices(
                pf, stock_data_df, fx, valuation_quote,
                external_fx_rates=market_config.execution.fx_rates,
            )
            status = mgr.get_status(pf, prices)
            status["_group"] = group_key
            status["_label"] = label
            all_statuses[group_key] = status
            logger.debug(
                "参考持仓%s已加载: 净值 %s, 回报 %s, %s",
                label,
                status["nav"],
                status["nav_return_pct"],
                status["valuation_reason"],
            )
        object.__setattr__(session, "ref_portfolio_status", all_statuses)

        try:
            from src.triggers.snapshots import capture_report_snapshot

            capture_report_snapshot(config, session, "daily")
        except Exception:  # Optional trigger observations cannot interrupt reports.
            logger.exception("Unable to save public daily trigger snapshot")

        if progress:
            progress("daily_notifications")
        delivery = notifier.send_daily_report_from_session(session)
        logger.info("每日任务执行完成")
        return _report_delivery_result(delivery, session)
    except Exception as e:
        logger.exception("执行日报任务时发生错误")
        return _report_result("failed", f"daily_task:{type(e).__name__}")


def run_brief_report(
    report_id: str = "morning_snapshot", force: bool = False, *,
    config: dict | None = None, progress: Callable | None = None,
):
    """
    运行简报任务（轻量级：仅价格 + 锚点偏离率）。

    由公共执行器的内部 worker 调用，CLI/Bot/定时器只提交任务。

    Args:
        report_id: 简报 ID，对应 config scheduler.brief_reports[].id
        force: True = 跳过周末/休市检测（手动触发用）
    """
    if not force:
        force = os.getenv("BOT_FORCE") == "1"

    logger = logging.getLogger(__name__)
    logger.info(f"开始执行简报任务: {report_id}")

    config = config if config is not None else load_config()
    from datetime import timezone

    from zoneinfo import ZoneInfo

    from src.notification.brief_market import has_open_brief_market

    observed = datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    today = observed.astimezone(ZoneInfo(
        config.get("scheduler", {}).get("timezone", "Asia/Shanghai")
    )).date()
    if not force and not has_open_brief_market(config.get("stocks", []), observed):
        logger.info("配置市场尚未开盘或已休市，跳过定时简报")
        return _report_result("skipped", "market_closed")

    try:
        if progress:
            progress("brief_session")
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
        if progress:
            progress("brief_stock_data")
        fetcher = StockDataFetcher(config)
        fetcher.fetch_to_session(session, session_manager)

        logger.info(f"简报：获取到 {len(session.stocks_data)} 只股票数据")

        _attach_return_curves(session, config)

        if progress:
            progress("brief_report_quotes")
        valuation_quote = prepare_report_quotes(
            session, fetcher.web_crawler.fetch_realtime_quote
        )

        # ── 参考持仓估值与公司行为（不自动调仓）──
        if progress:
            progress("brief_reference_portfolio")

        market_configs = get_market_optimizer_configs(
            config, groups=MARKET_GROUPS
        )
        stock_data_df = session.get_all_dataframe()

        POOLS = {
            "a_share": {
                "file": "data/ref_portfolio_a.yaml",
                "lot": market_configs["a_share"].execution.lot_sizes["a_share"],
                "fx": market_configs["a_share"].execution.fx_rates["a_share"],
                "execution": market_configs["a_share"].execution,
                "label": "A股",
            },
            "hk": {
                "file": "data/ref_portfolio_hk.yaml",
                "lot": market_configs["hk"].execution.lot_sizes["hk"],
                "fx": market_configs["hk"].execution.fx_rates["hk"],
                "execution": market_configs["hk"].execution,
                "label": "港股",
            },
            "us": {
                "file": "data/ref_portfolio_us.yaml",
                "lot": market_configs["us"].execution.lot_sizes["us"],
                "fx": market_configs["us"].execution.fx_rates["us"],
                "execution": market_configs["us"].execution,
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

            new_pf = pf
            trading_blocked_reason = ""
            try:
                new_pf, _corporate_action_trades = mgr.apply_corporate_actions(
                    pf,
                    _reference_portfolio_actions(config, pf),
                    today.strftime("%Y-%m-%d"),
                    withholding_rate=pool["execution"].withholding_rates.get(
                        group_key, 0.0
                    ),
                    fx_rate=pool["fx"],
                    external_prices=_reference_portfolio_external_prices(config, pf),
                )
                mgr.save(new_pf)
            # Preserve the last holdings when corporate action data is unavailable.
            except Exception as exc:  # noqa: BLE001
                trading_blocked_reason = "公司行为数据不就绪"
                logger.error(
                    "参考持仓%s 无法应用公司行为；保留已有持仓: %s",
                    pool["label"],
                    exc,
                )
            # 本组 status（现价 × FX → CNY，与成本基准一致）
            cny_prices = _reference_portfolio_prices(
                new_pf,
                stock_data_df,
                pool["fx"],
                valuation_quote,
                external_fx_rates=pool["execution"].fx_rates,
            )
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

        try:
            from src.triggers.snapshots import capture_report_snapshot

            capture_report_snapshot(config, session, "brief")
        except Exception:  # Optional trigger observations cannot interrupt reports.
            logger.exception("Unable to save public brief trigger snapshot")

        # 定时简报按报价快照防重，开市门禁已在入口检查。
        if progress:
            progress("brief_notifications")
        if not force:
            from src.utils.market_status import is_unchanged_report, mark_pushed
            stock_data_df = session.get_all_dataframe()
            last_pushed_file = Path(f"cache/last_pushed_{report_id}.txt")
            if is_unchanged_report(stock_data_df, last_pushed_file):
                logger.info("报价快照未变化，跳过重复简报推送")
                return _report_result("skipped", "quote_fingerprint_unchanged", session)

            # 发送简报（统一入口）
            notifier = NotifierManager(config)
            delivery = notifier.send_brief_report(session, report_config)

            # Unknown/partial is an attempted side effect, not a retry license.
            # Keep the existing fingerprint protection against duplicate sends.
            if not isinstance(delivery, dict) or delivery.get("status") != "failed":
                mark_pushed(last_pushed_file, stock_data_df)
        else:
            # 手动触发：直接推送，不记录
            notifier = NotifierManager(config)
            delivery = notifier.send_brief_report(session, report_config)

        logger.info(f"简报任务完成: {report_id}")
        return _report_delivery_result(delivery, session)

    except Exception as e:
        logger.exception("简报任务失败 (%s)", report_id)
        return _report_result("failed", f"brief_task:{type(e).__name__}")


def _send_restart_notification(config: dict, service):
    """Announce a started service through the common notification policy."""
    from src.notification.settings import env_flag

    if env_flag("SKIP_STARTUP_NOTIFICATION"):
        logging.getLogger(__name__).info("Startup notification skipped by setting")
        return
    jobs = service.scheduler.scheduler.get_jobs() if service.scheduler else []
    summary = "服务已启动；" + (
        "任务：" + "; ".join(f"{job.name}: {job.trigger}" for job in jobs)
        if jobs else "交互 Bot 模式"
    )
    NotifierManager(config).send_deployment_notification("SUCCESS", summary=summary)


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
        "--audit-instruments",
        action="store_true",
        help="audit typed company/fund/REIT profiles without running strategies",
    )
    mode.add_argument(
        "--backfill-point-in-time-data",
        action="store_true",
        help=(
            "backfill raw/qfq prices, corporate actions and dated financial "
            "statements without changing the active strategy"
        ),
    )
    mode.add_argument("--service", action="store_true", help="run scheduler and outbound Bots")
    mode.add_argument("--status", action="store_true", help="read local service status")
    mode.add_argument("--interactive", action="store_true", help="run configured outbound Bots")
    mode.add_argument(
        "--report-worker", choices=("daily", "brief"), help=argparse.SUPPRESS
    )
    parser.add_argument("--report-payload", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--report-result", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help="use an isolated configuration file")
    return parser


def main(argv: list[str] | None = None):
    """Application entry point."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    if args.report_worker:
        if args.report_payload is None or args.report_result is None:
            parser.error("internal report worker requires payload and result paths")
        from src.core.report_execution import run_report_worker

        return run_report_worker(
            args.report_worker,
            args.report_payload,
            args.report_result,
            application=sys.modules[__name__],
        )
    config = load_config(args.config) if args.config else load_config()
    if args.status:
        import json

        from src.core.runtime_service import read_service_status

        status = read_service_status(config)
        print(json.dumps(status, ensure_ascii=False))
        return 0 if status["ready"] else 1
    logger = setup_logging(config)
    if args.once:
        logger.info("Single daily run")
        from src.core.config_store import DEFAULT_CONFIG_PATH
        from src.core.report_execution import submit_report

        record = submit_report(
            config, kind="daily", force=os.getenv("BOT_FORCE") == "1",
            source="cli", wait=True, config_path=args.config or DEFAULT_CONFIG_PATH,
        )
        return 0 if record.get("status") in {"succeeded", "skipped"} else 1
    elif args.brief is not None:
        logger.info("Brief run: %s", args.brief)
        from src.core.config_store import DEFAULT_CONFIG_PATH
        from src.core.report_execution import submit_report

        record = submit_report(
            config, kind="brief", report_id=args.brief,
            force=os.getenv("BOT_FORCE") == "1", source="cli", wait=True,
            config_path=args.config or DEFAULT_CONFIG_PATH,
        )
        return 0 if record.get("status") in {"succeeded", "skipped"} else 1
    elif args.audit_instruments:
        report = InstrumentAuditService(config).run()
        outputs = report.summary.get("output_files", {})
        logger.info(
            "Instrument audit complete: %s instruments, fill %.1f%%, HTML=%s",
            len(report.profiles),
            float(report.summary.get("fill_rate", 0.0)) * 100.0,
            outputs.get("html", ""),
        )
    elif args.backfill_point_in_time_data:
        from src.data.point_in_time_backfill import PointInTimeBackfillService

        report = PointInTimeBackfillService(config).run()
        logger.info(
            "Point-in-time backfill complete: market %s/%s, statements %s/%s, "
            "report=%s",
            report["market_history_success"],
            report["instrument_count"],
            report["statement_success"],
            report["statement_applicable"],
            report.get("output_file", ""),
        )
    else:
        from src.core.runtime_service import RuntimeService

        service = RuntimeService(config, scheduling=not args.interactive)
        logger.info("Starting scheduler/Bot service with no HTTP listener")
        service.run(on_started=lambda: _send_restart_notification(config, service))
    return 0


if __name__ == "__main__":
    sys.exit(main())
