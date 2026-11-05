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
                    "/report_frequency [daily|weekly|off]",
                    "设置固定日报频次；weekly 每周五",
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
                ("/ref_date [YYYY-MM-DD]", "设置参考持仓基期（默认今天）"),
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
                    "/schedule [任务 时间]",
                    "查看/修改调度 例 <code>/schedule daily 20:00</code>",
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


def handle_add(codes: list[str]) -> str:
    added = []
    skipped = []

    def mutate(config: dict) -> None:
        stocks: list[str] = config.get("stocks", [])
        upper_stocks = {str(s).upper() for s in stocks}
        for code in codes:
            if code.upper() in upper_stocks:
                skipped.append(code)
            else:
                stocks.append(code)
                upper_stocks.add(code.upper())
                added.append(code)
        if added:
            config["stocks"] = stocks

    config = _update_config(mutate)
    stocks = config.get("stocks", [])

    if not added and not skipped:
        return "没有可添加的标的。"

    lines = []
    if added:
        lines.append(
            f"✅ 已添加 {len(added)} 只：{' '.join(f'<code>{c}</code>' for c in added)}"
        )
    if skipped:
        lines.append(
            f"⏭ 已存在 {len(skipped)} 只：{' '.join(f'<code>{c}</code>' for c in skipped)}"
        )
    lines.append(f"当前共 {len(stocks)} 只")
    return "\n".join(lines)


def handle_remove(codes: list[str]) -> str:
    removed = []
    not_found = []

    def mutate(config: dict) -> None:
        stocks: list[str] = config.get("stocks", [])
        upper_stocks = {str(s).upper(): s for s in stocks}
        for code in codes:
            matched = upper_stocks.get(code.upper())
            if matched is not None:
                stocks.remove(matched)
                del upper_stocks[code.upper()]
                removed.append(code)
            else:
                not_found.append(code)
        if removed:
            config["stocks"] = stocks

    config = _update_config(mutate)
    stocks = config.get("stocks", [])

    if not removed and not not_found:
        return "没有可移除的标的。"

    lines = []
    if removed:
        lines.append(
            f"✅ 已移除 {len(removed)} 只：{' '.join(f'<code>{c}</code>' for c in removed)}"
        )
    if not_found:
        lines.append(
            f"❌ 未找到 {len(not_found)} 只：{' '.join(f'<code>{c}</code>' for c in not_found)}"
        )
    lines.append(f"当前共 {len(stocks)} 只")
    return "\n".join(lines)


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
    """把当前 config.yaml 提交到 git。"""
    import subprocess

    if config_path is None:
        config_path = CONFIG_PATH
    repo = config_path.parent.parent  # .../trade_eyes_keeper
    try:
        subprocess.run(
            ["git", "add", "config/config.yaml"],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", "bot: watchlist updated via /save", "--no-verify"],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=10,
        )
        logger.info("配置已提交到 git")
        return "✅ 监控列表已保存到 git。下次本地 <code>git pull</code> 即可同步。"
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode() if e.stderr else str(e)
        logger.error(f"git commit 失败: {msg}")
        return f"❌ git 保存失败: {msg[:200]}"
    except Exception as e:
        logger.exception("git 保存异常")
        return f"❌ 保存失败: {e}"


def _run_main(command_args: list[str], env_extra: dict | None = None) -> bool:
    """Submit only full daily/brief reports to the shared queue."""
    if command_args and command_args[0] in {"--once", "--brief"}:
        from ...core.report_execution import submit_report

        brief = command_args[0] == "--brief"
        try:
            submit_report(
                _load_config(),
                kind="brief" if brief else "daily",
                report_id=(
                    command_args[1]
                    if brief and len(command_args) > 1
                    else "morning_snapshot"
                ),
                force=bool(env_extra and env_extra.get("BOT_FORCE") == "1"),
                source="bot",
                config_path=CONFIG_PATH,
            )
            return True
        except Exception:
            logger.exception("报表入队失败")
            return False
    return False


def handle_brief(report_id: str = "morning_snapshot") -> str:
    label = "早盘简报" if report_id == "morning_snapshot" else "收盘简报"
    if _run_main(["--brief", report_id], env_extra={"BOT_FORCE": "1"}):
        return f"⏳ {label}已触发。稍后飞书会推送简报卡片。"
    return f"❌ {label}触发失败"


def handle_daily() -> str:
    if _run_main(["--once"], env_extra={"BOT_FORCE": "1"}):
        return "⏳ 完整日报已触发。稍后飞书+邮件会推送。"
    return "❌ 日报触发失败"


def handle_daily_report_frequency(frequency: str | None = None) -> str:
    """查看或修改定时普通日报的发送频次。

    频次控制固定定时日报；托管触发器按自己的条件执行，
    ``/daily`` 手动触发也始终发送。
    """
    labels = {
        "daily": "每天（交易日）",
        "weekly": "每周五",
        "off": "关闭定时普通日报",
    }
    config = _load_config()
    scheduler = config.get("scheduler", {}) or {}
    current = str(scheduler.get("daily_report_frequency", "daily")).lower()
    if current not in labels:
        current = "daily"

    if not frequency:
        return (
            f"<b>日报频次</b>: {labels[current]} (<code>{current}</code>)\n"
            "可选: <code>daily</code> 每个交易日、"
            "<code>weekly</code> 每周五、<code>off</code> 关闭普通日报。\n"
            "托管触发器与手动 <code>/daily</code> 不受影响。"
        )

    mode = str(frequency).strip().lower()
    if mode not in labels:
        return "❌ 频次只能是 daily、weekly 或 off。"

    def mutate(current_config: dict) -> None:
        current_config.setdefault("scheduler", {})["daily_report_frequency"] = mode

    _update_config(mutate)
    return f"✅ 日报频次已改为 {labels[mode]} (<code>{mode}</code>)。"


def handle_schedule(action: str, task_id: str, time_str: str) -> str:
    """查看或修改调度时间。"""
    from ...core.schedule_manager import get_schedule_manager

    mgr = get_schedule_manager()
    if mgr is None:
        return "❌ 调度管理器未启动"

    if action == "view" or not task_id:
        items = mgr.get_schedule()
        if not items:
            return "当前无调度任务"
        lines = ["<b>当前调度</b>\n"]
        for s in items:
            lines.append(f"<code>{s['name']}</code>: {s['time']} ({s['timezone']})")
        return "\n".join(lines)

    # set
    try:
        ok = mgr.reschedule(task_id, time_str)
    except RuntimeError as exc:
        logger.exception("刷新调度失败")
        return f"❌ {exc}"
    if ok:
        label = {
            "daily": "日报",
            "morning_snapshot": "早盘简报",
            "afternoon_snapshot": "收盘简报",
        }.get(task_id, task_id)
        return f"✅ {label}时间已改为 <code>{time_str}</code>（立即生效）"
    return f"❌ 修改失败。任务名: {task_id}，时间: {time_str}"


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


def handle_ref_position(
    action: str,
    group: str,
    code: str,
    shares: int,
    price: float,
) -> str:
    """Modify one bound reference position from an explicit bot instruction."""
    from ...core.ref_portfolio import RefPortfolioManager
    from ...markets import _detect_fine_group
    from ...search.config import get_market_optimizer_config

    labels = {"a_share": "A股", "hk": "港股", "us": "美股"}
    files = {
        "a_share": "data/ref_portfolio_a.yaml",
        "hk": "data/ref_portfolio_hk.yaml",
        "us": "data/ref_portfolio_us.yaml",
    }
    if group not in files:
        return "❌ 市场只能是 a_share、hk 或 us。"
    try:
        detected = _detect_fine_group(code)
    # Malformed bot symbols must not reach portfolio writes.
    except Exception:  # noqa: BLE001
        detected = ""
    if detected != group:
        return f"❌ {code} 不属于 {labels[group]}，未修改参考持仓。"

    mgr = RefPortfolioManager(file_path=files[group])
    pf = mgr.load()
    if not mgr.is_initialized(pf):
        return f"❌ {labels[group]}参考持仓尚未初始化；请先发送 /ref_date YYYY-MM-DD。"
    if not pf.is_bound:
        return (
            f"❌ {labels[group]}参考持仓未绑定已激活运行，未修改；"
            "请先重新设置 /ref_date。"
        )

    try:
        execution = get_market_optimizer_config(
            group, application_config=_load_config()
        ).execution
    except ValueError as exc:
        return f"❌ 市场优化配置无效，未修改参考持仓：{exc}"
    trade_date = datetime.now().astimezone().strftime("%Y-%m-%d")
    try:
        new_pf, trades = mgr.adjust_position(
            pf,
            code,
            action,
            shares,
            price,
            trade_date,
            commission_rate=execution.commission_rate,
            lot_size=execution.lot_sizes[group],
            fx_rate=execution.fx_rates[group],
            reason="manual_bot",
        )
    except ValueError as exc:
        return f"❌ 参考持仓未修改：{exc}"
    mgr.save(new_pf)

    holding = new_pf.holdings.get(code)
    target = holding.shares if holding is not None else 0
    if not trades:
        return (
            f"✅ {labels[group]} <code>{code}</code> 已是目标仓位 "
            f"{target} 股；未产生重复交易。"
        )
    trade = trades[-1]
    side = "买入" if trade.action == "buy" else "卖出"
    return (
        f"✅ {labels[group]}参考持仓已修改\n"
        f"{side} <code>{code}</code> {trade.shares} 股 @CNY {trade.price:.4f}\n"
        f"目标仓位: {target} 股 | 现金: {new_pf.cash:,.2f}\n"
        f"记录: <code>{trade.event_id}</code>"
    )


def handle_ref_date(date_str: str | None = None) -> str:
    """设置或查看参考持仓基期。

    无参数: 显示当前基期 + 参考持仓状态
    有参数: 设置新基期并重置参考持仓（⚠️ 清空所有标的）
    参数 "confirm": 确认上次设置操作（防呆）
    """
    from ...core.ref_portfolio import (
        RefPortfolioManager,
        reference_execution_contract,
    )

    config = _load_config()
    opt = config.get("optimizer", {}) or {}
    current = opt.get("reference_base_date", "")

    # ── 需要确认（防呆）──
    pending_date = opt.get("_ref_date_pending", "")
    if date_str and date_str.strip().lower() == "confirm":
        if not pending_date:
            return "⚠️ 没有待确认的基期设置操作。发送 <code>/ref_date YYYY-MM-DD</code> 开始。"
        date_str = pending_date
        # 清除 pending，继续执行设置逻辑
        opt.pop("_ref_date_pending", None)
        config["optimizer"] = opt

    if not date_str:
        # ── 查看状态（三组各自展示）──
        lines = []
        if current:
            lines.append(f"📅 参考持仓基期: <b>{current}</b>")
        else:
            lines.append("📅 参考持仓基期: <b>未设置</b>")

        any_init = False
        for label, fname in [
            ("A股", "data/ref_portfolio_a.yaml"),
            ("港股", "data/ref_portfolio_hk.yaml"),
            ("美股", "data/ref_portfolio_us.yaml"),
        ]:
            mgr = RefPortfolioManager(file_path=fname)
            pf = mgr.load()
            if not mgr.is_initialized(pf):
                continue
            any_init = True
            lines.append(
                f"\n<b>{label}</b>: 现金 {pf.cash:,.2f} | "
                f"持仓 {len(pf.holdings)} 只 | 交易日 {pf.trading_days}"
            )
            if pf.is_bound:
                lines.append(
                    f"  策略: <code>{pf.strategy_id}</code> | "
                    f"固定运行: <code>{pf.strategy_run_id}</code>"
                )
            else:
                lines.append("  ⚠️ 旧持仓未绑定运行；不会继续交易，须手动重置。")
            if pf.holdings:
                for code, h in sorted(pf.holdings.items()):
                    lines.append(
                        f"  • <code>{code}</code> {h.shares}股 成本 {h.avg_cost:.2f}"
                    )
        if not any_init:
            lines.append(
                "\n📭 参考持仓未初始化"
                "（发送 <code>/ref_date YYYY-MM-DD</code> 开始跟踪）"
            )
        else:
            lines.append(
                "\n⚠️ 发送 <code>/ref_date YYYY-MM-DD</code> 将<b>清空所有持仓</b>并重置。"
            )
        return "\n".join(lines)

    # ── 设置新基期 ──
    from datetime import datetime as _dt

    try:
        # This validates only a calendar date; no instant is constructed or compared.
        _dt.strptime(date_str, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return f"❌ 日期格式错误: {date_str}。请使用 YYYY-MM-DD。"

    from ...search.artifacts import load_latest_strategy_run
    from ...search.config import get_market_optimizer_configs
    from ...search.contracts import stable_hash

    active_run = load_latest_strategy_run()
    if active_run is None:
        return (
            "❌ 没有可绑定的三市场完整已激活运行；参考持仓未重置。"
            "需要已有完整策略与参数快照。"
        )
    active_strategies = {
        group: active_run.strategy_for(group) for group in ("a_share", "hk", "us")
    }
    if any(
        active_strategies[group] is None
        or active_run.params_by_group.get(group) is None
        for group in active_strategies
    ):
        return (
            "❌ 已激活运行缺少完整的三市场策略或参数；参考持仓未重置。"
            "需要已有完整策略与参数快照。"
        )
    try:
        market_configs = get_market_optimizer_configs(config)
    except ValueError as exc:
        return f"❌ 历史执行合同配置无效，参考持仓未重置：{exc}"
    initial_capital = float(market_configs["a_share"].execution.initial_capital)

    # 防呆：设置新基期前需要确认
    if not pending_date:
        total_holdings = 0
        total_cash = 0.0
        for fname in [
            "data/ref_portfolio_a.yaml",
            "data/ref_portfolio_hk.yaml",
            "data/ref_portfolio_us.yaml",
        ]:
            mgr = RefPortfolioManager(file_path=fname)
            pf = mgr.load()
            if mgr.is_initialized(pf):
                total_holdings += len(pf.holdings)
                total_cash += pf.cash
        if total_holdings > 0:

            def set_pending(current_config: dict) -> None:
                current_config.setdefault("optimizer", {})["_ref_date_pending"] = (
                    date_str
                )

            _update_config(set_pending)
            return (
                f"⚠️ <b>确认重置参考持仓？</b>\n\n"
                f"当前持仓: {total_holdings} 只标的，现金 {total_cash:,.2f}。\n\n"
                f"重置后将:\n"
                f"• 清空 A股/港股/美股 全部持仓\n"
                f"• 每组恢复初始现金 {initial_capital:,.0f}\n"
                f"• 期初日期设为 {date_str}\n\n"
                f"确认请发送: <code>/ref_date confirm</code>\n"
                f"取消请忽略本条消息。"
            )

    # 执行重置（三组各自独立）
    for group, label, fname in [
        ("a_share", "A股", "data/ref_portfolio_a.yaml"),
        ("hk", "港股", "data/ref_portfolio_hk.yaml"),
        ("us", "美股", "data/ref_portfolio_us.yaml"),
    ]:
        params = active_run.params_by_group.get(group)
        strategy = active_strategies[group]
        execution_config = market_configs[group].execution
        group_run_id = active_run.run_id_for(group)
        if params is None:
            return f"❌ 已激活运行缺少 {label} 参数；参考持仓未重置。"
        mgr = RefPortfolioManager(file_path=fname)
        mgr.reset(
            initial_capital=initial_capital,
            inception_date=date_str,
            market_group=group,
            strategy_run_id=group_run_id,
            strategy_id=strategy.name,
            strategy_timestamp=active_run.timestamp,
            params_hash=stable_hash(
                {
                    "strategy_id": strategy.name,
                    "values": params.values,
                }
            ),
            execution_hash=stable_hash(
                reference_execution_contract(
                    params.execution_snapshot,
                    execution_config,
                    group,
                )
            ),
        )

    def set_base_date(current_config: dict) -> None:
        current_optimizer = current_config.setdefault("optimizer", {})
        current_optimizer["reference_base_date"] = date_str
        if current_optimizer.get("_ref_date_pending") == pending_date:
            current_optimizer.pop("_ref_date_pending", None)

    _update_config(set_base_date)
    return (
        f"✅ 参考持仓已重置（A股/港股/美股 三分仓）\n"
        f"📅 基期: <b>{date_str}</b>\n"
        f"💰 每组初始资金: {initial_capital:,.0f}\n"
        f"📭 持仓: 已清空\n"
        f"🔒 固定运行: <code>{', '.join(f'{group}:{active_run.run_id_for(group)}' for group in active_strategies)}</code> "
        f"({', '.join(f'{group}:{active_strategies[group].name}' for group in active_strategies)})\n"
        f"\n参考持仓固定绑定已有运行；再次手动重置才会更换绑定。"
    )
