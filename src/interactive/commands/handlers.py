"""命令处理器 — 每个命令接收解析后的对象，返回响应文本。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from ...core.config_store import ConfigStore

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "config.yaml"


def _load_config() -> dict:
    return ConfigStore(CONFIG_PATH).load_runtime()


def _update_config(mutate: Callable[[dict], None]) -> dict:
    saved = ConfigStore(CONFIG_PATH).update(mutate)
    logger.info("配置已保存: %s", CONFIG_PATH)
    return saved


def _git_info() -> str:
    """Show the verified running build, including preserved production overlays."""
    from html import escape

    from ...core.build_info import build_label

    return "\n\n<b>运行版本</b>\n<code>" + escape(build_label()) + "</code>"


def handle_help() -> str:
    sections = [
        (
            "📋 监控列表",
            [
                ("/list", "查看监控列表"),
                ("/add 代码,...", "批量添加 例 <code>/add 601728,GOOG,00883</code>"),
                ("/remove 代码,...", "批量移除"),
                ("/save", "保存监控列表到 git"),
            ],
        ),
        (
            "📊 报告触发",
            [
                ("/daily", "触发完整日报"),
                ("/brief [afternoon]", "触发简报（默认早盘）"),
                (
                    "/report_frequency",
                    "查看已停用的旧频次配置；报告调度使用触发器",
                ),
                (
                    "/backtest 代码 起 止",
                    "回测例 <code>/backtest 601919 2024-01-01 2024-12-31</code>",
                ),
            ],
        ),
        (
            "📈 参考持仓与执行合同",
            [
                ("/mode", "查看当前 TradePlan 执行合同"),
                ("/ref_date", "查看参考持仓基期；重置请通过飞书对话提案确认"),
                (
                    "/ref_position set|buy|sell 市场 代码 股数 价格",
                    "按显式成交价修改参考持仓并记录交易",
                ),
            ],
        ),
        (
            "🔔 调度",
            [
                (
                    "/schedule",
                    "只读查看调度；报告调度使用触发器",
                ),
            ],
        ),
        (
            "ℹ️ 其他",
            [
                ("/help", "显示此帮助"),
            ],
        ),
    ]
    parts = ["<b>📖 可用命令</b>"]
    for title, cmds in sections:
        parts.append(f"\n<b>{title}</b>")
        for cmd, desc in cmds:
            parts.append(f"<code>{cmd}</code> — {desc}")
    return "\n".join(parts) + _git_info()


def handle_list() -> str:
    config = _load_config()
    stocks = config.get("stocks", [])
    if not stocks:
        return "监控列表为空。使用 <code>/add 代码</code> 添加。"

    lines = [f"<b>监控列表</b>（共 {len(stocks)} 只）"]
    for code in stocks:
        lines.append(f"<code>{code}</code>")
    return "\n".join(lines)


def _change_watchlist(action: str, codes: list[str]) -> str:
    from ..assistant.config_tools import ConfigurationTools

    tools = ConfigurationTools(CONFIG_PATH.parent.parent)
    proposals = tools.propose_watchlist(action, codes)
    if not proposals:
        return "监控列表无变更（标的已存在或无需移除）。"
    proposal = proposals[0]
    tools.apply(proposal)
    change = next(x for x in proposal["diff"] if x["key"] == "stocks")
    before, after = change["old"], change["new"]
    changed = (
        [x for x in after if x not in before]
        if action == "add"
        else [x for x in before if x not in after]
    )
    label = "添加" if action == "add" else "移除"
    return (
        f"✅ 已{label} {len(changed)} 只：{' '.join(map(str, changed))}\n当前共 {len(after)} 只"
    )


def handle_add(codes: list[str]) -> str:
    return _change_watchlist("add", codes)


def handle_remove(codes: list[str]) -> str:
    return _change_watchlist("remove", codes)


def _get_backtest_strategy(config: dict, group: str | None = None):
    """Resolve only the explicitly activated strategy for one market."""
    from ...search.artifacts import load_latest_strategy_run

    if group not in {"a_share", "hk", "us"}:
        return None
    active = load_latest_strategy_run(groups=(group,))
    return active.strategy_for(group) if active is not None else None


def _get_backtest_params(group: str, strategy):
    """Load parameters from the explicitly activated group artifact only."""
    from ...search.artifacts import load_latest_strategy_run

    active = load_latest_strategy_run(groups=(group,))
    if (
        active is not None
        and active.strategy_for(group) is not None
        and active.strategy_for(group).name == strategy.name
    ):
        params = active.params_by_group.get(group)
        if params is not None:
            return params, False
    return None, False


def handle_backtest(code: str, start: str, end: str) -> str:
    """单票回测（使用统一评估引擎 evaluate_all_groups）。"""
    try:
        import pandas as pd

        from ...backtest.engine import evaluate_all_groups
        from ...data.backtest_data import prepare_backtest_data
        from ...markets import _detect_fine_group
        from ...search.config import get_market_optimizer_config

        config = _load_config()

        # Date-only exchange bounds must match the naive pandas window contract.
        s = datetime.strptime(start, "%Y-%m-%d")  # noqa: DTZ007
        e = datetime.strptime(end, "%Y-%m-%d")  # noqa: DTZ007
        group = _detect_fine_group(code)
        strategy = _get_backtest_strategy(config, group)
        if strategy is None:
            return "❌ 该市场没有可用的历史策略快照，无法回测。"
        params, using_fallback_params = _get_backtest_params(group, strategy)
        if params is None:
            return "❌ 该市场没有可用的历史参数快照，无法回测。"
        market_config = get_market_optimizer_config(group, application_config=config)

        market_bundles = None
        benchmark_bundles = None
        if "point_in_time_data" in config:
            constraints = market_config.constraints
            benchmark_codes = [
                item
                for item in constraints.benchmark_codes_for(group)
                if item != "risk_free"
            ]
            warmup_start = (
                pd.Timestamp(s)
                - pd.DateOffset(months=constraints.walk_forward.state_lookback_months)
            ).date()
            prepared = prepare_backtest_data(
                config,
                [code],
                warmup_start,
                e.date(),
                purpose="interactive_backtest",
                benchmark_codes=benchmark_codes,
                strategy=strategy,
                require_current=False,
            )
            if prepared.issues or code not in prepared.bundles:
                details = (
                    "; ".join(f"{item.code}: {item.reason}" for item in prepared.issues)
                    or "行情包未就绪"
                )
                return f"❌ 回测数据未就绪 <code>{code}</code>：{details}"
            market_bundles = {code: prepared.bundles[code]}
            benchmark_bundles = {
                item: bundle
                for item, bundle in prepared.bundles.items()
                if item != code
            }
            frame = prepared.bundles[code].prices
            history = pd.DataFrame(
                {
                    "date": pd.to_datetime(frame["date"], errors="coerce"),
                    "open": pd.to_numeric(frame["qfq_open"], errors="coerce"),
                    "high": pd.to_numeric(frame["qfq_high"], errors="coerce"),
                    "low": pd.to_numeric(frame["qfq_low"], errors="coerce"),
                    "close": pd.to_numeric(frame["qfq_close"], errors="coerce"),
                    "volume": pd.to_numeric(frame["volume"], errors="coerce"),
                    "tradable": frame["tradable"].astype(bool),
                }
            )
        else:
            from ...data.data_source import DataSource

            ds = DataSource(config)
            requested_days = (e - s).days
            days = max(requested_days + 365, 1000)
            history = ds.fetch_stock_data(code, days=days)
            if history is None or history.empty or "date" not in history.columns:
                return f"❌ 未获取到 <code>{code}</code> 的行情数据"

            history = history.copy()
            history["date"] = pd.to_datetime(history["date"])
        history = history.sort_values("date").reset_index(drop=True)
        actual_start = str(history["date"].min())[:10]
        actual_end = str(history["date"].max())[:10]
        data = history[
            (history["date"] >= pd.Timestamp(start))
            & (history["date"] <= pd.Timestamp(end))
        ]
        if data.empty:
            return (
                f"❌ <code>{code}</code> 在 {start} ~ {end} 无数据\n"
                f"缓存数据范围: {actual_start} ~ {actual_end}"
            )
        reports = evaluate_all_groups(
            {code: history[history["date"] <= pd.Timestamp(end)]},
            [code],
            strategy,
            params,
            market_config.execution,
            benchmark_bundles=benchmark_bundles,
            market_bundles=market_bundles,
            target_groups=[group],
            market_constraints=market_config.constraints,
            start_date=start,
            end_date=end,
        )
        report = reports.get(group)
        if report is None:
            return f"❌ <code>{code}</code> 评估失败，无可交易数据"

        if market_bundles:
            raw = market_bundles[code].prices.copy()
            raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
            raw = raw[
                (raw["date"] >= pd.Timestamp(start))
                & (raw["date"] <= pd.Timestamp(end))
            ]
            bh_start = float(raw["raw_close"].iloc[0])
            bh_end = float(raw["raw_close"].iloc[-1])
        else:
            bh_start = float(data["close"].iloc[0])
            bh_end = float(data["close"].iloc[-1])
        bh_return = (bh_end - bh_start) / bh_start * 100

        return (
            f"<b>回测报告</b> — <code>{code}</code>\n"
            f"区间: {start} ~ {end}（{len(data)} 天）\n"
            f"策略: {report.strategy_label} ({report.engine_name})\n\n"
            + (
                "⚠ 未找到该市场的优化参数，已使用中性参数。\n"
                if using_fallback_params
                else ""
            )
            + f"<b>策略收益</b>: {report.total_return:+.2f}%"
            f"  |  <b>买入持有</b>: {bh_return:+.2f}%\n"
            f"<b>超额收益</b>: {report.excess_return:+.2f}%"
            f"  |  <b>最大回撤</b>: {report.max_drawdown:.2f}%\n"
            f"<b>夏普比率</b>: {report.sharpe_ratio:.2f}"
            f"  |  <b>交易</b>: {report.trade_count} 笔\n"
            f"<b>成分</b>: {', '.join(report.composition) if report.composition else '—'}"
            f"  |  评估时间: {report.timestamp[:16].replace('T', ' ')}"
        )
    except Exception as exc:
        logger.exception(f"回测失败 {code} {start} {end}")
        return f"❌ 回测失败: {exc}"


def handle_save(config_path=None) -> str:
    from ...core.config_store import save_configuration

    return save_configuration(config_path or CONFIG_PATH)


def _submit_report(kind, report_id, *, sender, chat_id, request_id):
    """Keep the real report receipt; formatting belongs to the command adapter."""
    from ..assistant.report_tools import ReportTools

    return ReportTools(
        _load_config(), owner=sender, chat_id=chat_id, config_path=CONFIG_PATH
    ).submit(
        kind=kind, report_id=report_id, force=True, request_id=request_id, source="bot"
    )


def _report_response(label, receipt):
    return (
        f"⏳ {label}请求已登记。运行 ID：{receipt['run_id']}；"
        f"当前状态：{receipt['status']}。完整报告通过全部已启用渠道通知；"
        "入队不代表执行成功或已经送达。"
    )


def handle_brief(
    report_id: str = "morning_snapshot", *, sender=None, chat_id=None, request_id=None
) -> str:
    label = "早盘简报" if report_id == "morning_snapshot" else "收盘简报"
    try:
        receipt = _submit_report(
            "brief", report_id, sender=sender, chat_id=chat_id, request_id=request_id
        )
        return _report_response(label, receipt)
    except Exception:
        logger.exception("简报请求未获得可确认的执行回执")
        return f"❌ {label}请求未获得可确认的执行回执，请查看服务日志。"


def handle_daily(*, sender=None, chat_id=None, request_id=None) -> str:
    try:
        receipt = _submit_report(
            "daily", "morning_snapshot", sender=sender,
            chat_id=chat_id, request_id=request_id,
        )
        return _report_response("完整日报", receipt)
    except Exception:
        logger.exception("日报请求未获得可确认的执行回执")
        return "❌ 日报请求未获得可确认的执行回执，请查看服务日志。"


def handle_daily_report_frequency(frequency: str | None = None) -> str:
    """Retired fixed-report configuration remains readable, never writable."""
    if frequency:
        return "❌ 固定日报频次入口已停用；请使用 trigger_manage 管理报告触发器。"
    current = (_load_config().get("scheduler", {}) or {}).get("daily_report_frequency", "daily")
    return f"旧日报频次配置: {current}（已停用）；报告调度请使用 trigger_manage。"


def handle_schedule(action: str, task_id: str, time_str: str) -> str:
    """Read existing schedules; report creation belongs to managed triggers."""
    if action != "view" or task_id or time_str:
        return "❌ 固定报告调度入口已停用；请使用 trigger_manage 管理报告触发器。"
    from ...core.schedule_manager import get_schedule_manager

    mgr = get_schedule_manager()
    if mgr is None:
        return "❌ 调度管理器未启动"
    items = mgr.get_schedule()
    if not items:
        return "当前无调度任务"
    return "<b>当前调度</b>\n" + "\n".join(
        f"<code>{s['name']}</code>: {s['time']} ({s['timezone']})" for s in items
    )


# ── /mode ────────────────────


_MODE_LABELS = {
    "trade_plan": "TradePlan（策略声明、统一执行）",
}


def handle_mode(mode: str) -> str:
    """Show execution models from the active immutable strategy artifacts."""
    if not mode:
        from ...search.artifacts import load_latest_strategy_run

        lines = [
            f"当前模式: <b>{_MODE_LABELS['trade_plan']}</b>",
            "",
            "执行模式由每个已激活参数产物声明，核心回测和参考持仓统一解释。",
            "支持 <code>cash_cap</code> 与 <code>target_weight</code>，不可全局强制切换。",
        ]
        for group in ("a_share", "hk", "us"):
            active = load_latest_strategy_run(groups=(group,))
            if active is None:
                lines.append(f"  {group}: 未激活")
                continue
            params = active.params_by_group.get(group)
            execution = params.execution_snapshot if params is not None else {}
            lines.append(
                f"  {group}: <code>{active.strategy_by_group.get(group, '')}</code> / "
                f"运行 <code>{active.run_id_for(group)}</code> / "
                f"{execution.get('model', '未声明')}"
            )
        return "\n".join(lines)

    return "⚠️ 执行模式由 TradePlan 声明，不能通过 /mode 修改。"


def handle_ref_position(action: str, group: str, code: str, shares: int, price: float) -> str:
    from ...core.ref_portfolio import adjust_reference_position

    return adjust_reference_position(CONFIG_PATH, action, group, code, shares, price)


def handle_ref_date(date_str: str | None = None) -> str:
    from ...core.ref_portfolio import reference_status

    return reference_status(CONFIG_PATH, date_str)


def prepare_ref_date(date_str: str) -> dict:
    from ...core.ref_portfolio import prepare_reference_reset

    return prepare_reference_reset(CONFIG_PATH, date_str)


def apply_ref_date(plan: dict) -> str:
    from ...core.ref_portfolio import apply_reference_reset

    return apply_reference_reset(CONFIG_PATH, plan)
